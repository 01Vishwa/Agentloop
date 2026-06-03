"""Upload service.

Encapsulates the file-write logic for in-memory processing,
performing chunked streaming reads with mid-stream size validation.

Session isolation fix: _FILE_CACHE is now a two-level dict keyed by
session_id → filename → bytes.  All public functions accept a
``session_id`` parameter (default ``"__anon__"``) so that concurrent
users with identically-named files never overwrite each other's data.
"""

import logging
import threading
from typing import Dict, Optional, Tuple

from fastapi import UploadFile

from core.validation import validate_file_size

logger = logging.getLogger("uvicorn.info")

_ANON_SESSION = "__anon__"

# ---------------------------------------------------------------------------
# Magic-byte MIME validation (P1-2 security fix)
# ---------------------------------------------------------------------------

try:
    import magic as _magic  # python-magic-bin (already in requirements.txt)
    _MAGIC_AVAILABLE = True
except ImportError:
    _magic = None  # type: ignore[assignment]
    _MAGIC_AVAILABLE = False
    logger.warning(
        "[UploadService] python-magic not available — MIME validation disabled. "
        "Install python-magic-bin to enable magic-byte file type checking."
    )

# Allowlist: extension → set of acceptable MIME types returned by libmagic.
# Only file types the platform actually needs to process are permitted.
_ALLOWED_MIME_BY_EXT: Dict[str, set] = {
    "csv":     {"text/plain", "text/csv", "application/csv"},
    "tsv":     {"text/plain", "text/tab-separated-values"},
    "txt":     {"text/plain"},
    "json":    {"application/json", "text/plain"},
    "xlsx":    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/zip",  # libmagic sees XLSX as zip
    },
    "xls":     {"application/vnd.ms-excel"},
    "parquet": {"application/octet-stream"},
    "pdf":     {"application/pdf"},
}


def _validate_mime(filename: str, content: bytes) -> None:
    """Raises ``ValueError`` when the file's magic bytes contradict its extension.

    Protects against extension-spoofing attacks where a malicious payload is
    disguised with a benign extension (e.g. an ELF binary named ``data.csv``).
    Only validates file types present in ``_ALLOWED_MIME_BY_EXT``; unknown
    extensions are passed through without magic-byte verification (and therefore
    cannot be spoofed into an accepted type).

    Args:
        filename: Original upload filename (used to derive the extension).
        content: Raw file bytes (only the first 2 KB are inspected).

    Raises:
        ValueError: If the MIME type detected by libmagic is not in the
            allowlist for the declared extension.
    """
    if not _MAGIC_AVAILABLE or not filename:
        return

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed_mimes = _ALLOWED_MIME_BY_EXT.get(ext)
    if allowed_mimes is None:
        # Unknown extension — reject outright (not in the allowlist at all)
        raise ValueError(
            f"Unsupported file extension '.{ext}'. "
            f"Accepted types: {sorted(_ALLOWED_MIME_BY_EXT.keys())}"
        )

    detected = _magic.from_buffer(content[:2048], mime=True)
    if detected not in allowed_mimes:
        raise ValueError(
            f"File '{filename}' rejected: extension claims '.{ext}' but magic "
            f"bytes indicate '{detected}'. This may be a spoofed upload."
        )

# Two-level in-memory cache: {session_id → {filename → bytes}}
# Never access this dict directly from outside this module.
_FILE_CACHE: Dict[str, Dict[str, bytes]] = {}
_CACHE_LOCK = threading.Lock()


async def save_upload_file(
    file: UploadFile,
    session_id: str = _ANON_SESSION,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Tuple[int, str]:
    """Streams a single UploadFile into the session-scoped cache.

    Args:
        file: The incoming multipart file payload.
        session_id: Caller's session identifier.  Defaults to the shared
            anonymous bucket when no session is provided.

    Returns:
        Tuple[int, str]: Bytes read and an in-memory status string.

    Raises:
        ValueError: If the file exceeds the configured size limit.
    """
    bytes_read = 0
    content_chunks = []

    while chunk := await file.read(1024 * 1024):  # 1 MB chunks
        bytes_read += len(chunk)

        size_issue = validate_file_size(bytes_read)
        if size_issue:
            raise ValueError(size_issue)

        content_chunks.append(chunk)

    # Store in the session bucket — never touching other sessions
    content = b"".join(content_chunks)

    # P1-2 MIME validation: verify magic bytes match the declared extension
    # before accepting the file into the session cache or Supabase.
    try:
        _validate_mime(file.filename, content)
    except ValueError as mime_err:
        raise ValueError(str(mime_err)) from mime_err

    with _CACHE_LOCK:
        _FILE_CACHE.setdefault(session_id, {})[file.filename] = content

    file_format = (
        file.filename.rsplit(".", 1)[-1].lower()
        if file.filename and "." in file.filename
        else "unknown"
    )

    if workspace_id:
        try:
            from services.supabase_service import upload_to_storage, insert_file_record
            public_url, storage_path = await upload_to_storage(
                filename=file.filename,
                content_bytes=content,
                extension=file_format
            )
            record = {
                "filename": file.filename,
                "file_path": storage_path,
                "file_url": public_url,
                "file_size": bytes_read,
                "extension": file_format,
                "workspace_id": workspace_id,
                "user_id": user_id,
            }
            await insert_file_record(record)
        except Exception as e:
            logger.debug("Failed to persist file %s to Supabase Storage (non-critical): %s", file.filename, e)

    logger.info(
        "Uploaded File Metadata - Name: %s, Format: %s, Session: %s",
        file.filename,
        file_format,
        session_id,
    )

    return bytes_read, "Stored in memory"


def get_file_content(
    filename: str,
    session_id: str = _ANON_SESSION,
) -> Optional[bytes]:
    """Retrieves cached file bytes for the given session.

    Args:
        filename: Name of the file to retrieve.
        session_id: Session that owns the file.

    Returns:
        Raw bytes if found, else None.
    """
    with _CACHE_LOCK:
        return _FILE_CACHE.get(session_id, {}).get(filename)


def get_session_files(session_id: str = _ANON_SESSION) -> Dict[str, bytes]:
    """Returns a snapshot of all file bytes belonging to a session.

    The returned dict is a shallow copy — safe to iterate without
    holding a lock even if another request uploads concurrently.

    Args:
        session_id: Session whose files to retrieve.

    Returns:
        Dict mapping filename → bytes for the session.
    """
    with _CACHE_LOCK:
        return dict(_FILE_CACHE.get(session_id, {}))


def clear_file_cache(session_id: str = _ANON_SESSION) -> None:
    """Removes all cached files for the given session.

    Args:
        session_id: Session to wipe.  Only that session's data is removed;
            other sessions are unaffected.
    """
    with _CACHE_LOCK:
        _FILE_CACHE.pop(session_id, None)
    logger.info("[UploadService] Cleared file cache for session: %s", session_id)
