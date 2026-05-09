"""Upload service.

Encapsulates the file-write logic for in-memory processing,
performing chunked streaming reads with mid-stream size validation.

Session isolation fix: _FILE_CACHE is now a two-level dict keyed by
session_id → filename → bytes.  All public functions accept a
``session_id`` parameter (default ``"__anon__"``) so that concurrent
users with identically-named files never overwrite each other's data.
"""

import logging
from typing import Dict, Optional, Tuple

from fastapi import UploadFile

from core.validation import validate_file_size

logger = logging.getLogger("uvicorn.info")

_ANON_SESSION = "__anon__"

# Two-level in-memory cache: {session_id → {filename → bytes}}
# Never access this dict directly from outside this module.
_FILE_CACHE: Dict[str, Dict[str, bytes]] = {}


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
            logger.warning("Failed to persist file %s to Supabase: %s", file.filename, e)

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
    return dict(_FILE_CACHE.get(session_id, {}))


def clear_file_cache(session_id: str = _ANON_SESSION) -> None:
    """Removes all cached files for the given session.

    Args:
        session_id: Session to wipe.  Only that session's data is removed;
            other sessions are unaffected.
    """
    _FILE_CACHE.pop(session_id, None)
    logger.info("[UploadService] Cleared file cache for session: %s", session_id)
