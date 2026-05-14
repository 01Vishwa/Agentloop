"""Stringent validation and sanitization filters.

Enforces explicit limits against Mime typing, byte limits, and unsafe text
to proactively harden the application. Extended with analysis-mode format
gating that restricts uploads to structured dataset formats only.
"""

import re
from fastapi import UploadFile
from core.config import (
    ALLOWED_MIME_TYPES,
    MAX_FILE_SIZE_BYTES,
    ANALYSIS_MODE_ALLOWED_FORMATS,
)

# ---------------------------------------------------------------------------
# Magic-byte MIME checker (MIN-04)
# ---------------------------------------------------------------------------

try:
    import magic as _magic
    _MAGIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MAGIC_AVAILABLE = False

# Bytes to read for magic sniffing (first 2 KB is sufficient for all common types)
_MAGIC_SNIFF_BYTES = 2048


async def validate_file_metadata(
    file: UploadFile,
    analysis_mode: bool = False,
) -> str:
    """Verifies file against extension, declared MIME, and magic bytes.

    Validation order:
    1. Extension present and whitelisted.
    2. Analysis-mode format gate (if enabled).
    3. Client-declared Content-Type matches extension.
    4. Magic-byte sniff (if python-magic is installed) to detect spoofed
       file types (e.g. a Python script uploaded as data.csv).

    When ``analysis_mode`` is True the allowed extension set is restricted
    to ``ANALYSIS_MODE_ALLOWED_FORMATS`` (csv, xlsx, json).

    Args:
        file (UploadFile): Streamed file payload.
        analysis_mode (bool): Whether the analysis_mode switch is ON.

    Returns:
        str: Empty string if valid, otherwise returns the rejection reason.
    """
    filename = file.filename or ""
    if '.' not in filename:
        return "Missing extension."

    ext = filename.rsplit('.', 1)[-1].lower()

    # Analysis-mode format gate (enforced before general MIME check)
    if analysis_mode and ext not in ANALYSIS_MODE_ALLOWED_FORMATS:
        allowed = ", ".join(sorted(ANALYSIS_MODE_ALLOWED_FORMATS))
        return (
            f"Analysis Mode only accepts: {allowed}. "
            f"Received: .{ext}"
        )

    # General extension validation
    if ext not in ALLOWED_MIME_TYPES:
        return f"Unsupported format: {ext}"

    # MIME alignment check against client-declared Content-Type
    expected_mime = ALLOWED_MIME_TYPES[ext]
    actual_mime = file.content_type

    # B3 fix: Normalise known browser MIME aliases before comparing.
    # Different OS/browser combinations report different MIME strings for the
    # same file format (e.g. Windows Chrome sends application/vnd.ms-excel for
    # .csv). Map these to their canonical equivalents first.
    _BROWSER_MIME_ALIASES: dict = {
        "application/vnd.ms-excel":    "text/csv",       # Windows Chrome/Edge for .csv
        "application/csv":             "text/csv",
        "text/comma-separated-values": "text/csv",
        "application/x-json":          "application/json",
        "text/x-json":                 "application/json",
    }
    actual_mime = _BROWSER_MIME_ALIASES.get(actual_mime, actual_mime)

    # If the browser reports a truly generic type (octet-stream, text/plain,
    # or nothing at all), skip the MIME check entirely and let magic-byte
    # sniffing be the authoritative gate — do not reject prematurely.
    _GENERIC_MIMES = {"application/octet-stream", "text/plain", None, ""}

    if actual_mime not in _GENERIC_MIMES and actual_mime != expected_mime:
        return f"MIME mismatch (Got {actual_mime}, expected {expected_mime})."

    # Magic-byte sniff — detect spoofed Content-Type / extension (MIN-04)
    if _MAGIC_AVAILABLE:
        try:
            header = await file.read(_MAGIC_SNIFF_BYTES)
            await file.seek(0)  # Reset stream position for downstream parsers
            detected_mime = _magic.from_buffer(header, mime=True)
            # Allow a small set of aliases for text-based formats
            _MIME_ALIASES: dict = {
                "text/x-csv": "text/csv",
                "application/x-json": "application/json",
                "text/x-json": "application/json",
            }
            normalised = _MIME_ALIASES.get(detected_mime, detected_mime)
            
            # Special fallback for text/plain because magic often detects CSV/JSON/MD simply as plain text
            if detected_mime == "text/plain" and expected_mime in ["text/csv", "application/json", "text/markdown"]:
                normalised = expected_mime
            if normalised != expected_mime:
                return (
                    f"File content does not match declared type. "
                    f"Expected {expected_mime!r}, detected {detected_mime!r}. "
                    f"Possible spoofed file extension."
                )
        except Exception:  # pylint: disable=broad-except
            pass  # Magic sniff failure is non-fatal; proceed with header checks

    return ""


def validate_analysis_mode_format(ext: str) -> str:
    """Standalone gate for analysis-mode format enforcement.

    Used independently of MIME checking when the extension has already
    been extracted upstream.

    Args:
        ext (str): Lowercase file extension string.

    Returns:
        str: Error message if the extension is disallowed, else empty string.
    """
    if ext not in ANALYSIS_MODE_ALLOWED_FORMATS:
        allowed = ", ".join(sorted(ANALYSIS_MODE_ALLOWED_FORMATS))
        return (
            f"Analysis Mode only accepts structured dataset formats: "
            f"{allowed}. Received: .{ext}"
        )
    return ""


def validate_file_size(file_size: int) -> str:
    """Validates the byte count against local configurations.

    Args:
        file_size (int): Analyzed memory overhead of an asset.

    Returns:
        str: Error payload string or empty if clean.
    """
    if file_size > MAX_FILE_SIZE_BYTES:
        mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
        return f"File exceeds {mb:.0f}MB limit."
    return ""


def sanitize_text(raw_text: str) -> str:
    """Safely cleans out anomalous characters or execution blocks.

    Args:
        raw_text (str): Extracted memory text from parser.

    Returns:
        str: Output devoid of dangerous execution characters or script tags.
    """
    # Exterminate raw null bytes
    clean_text = raw_text.replace('\x00', '')

    # Strip basic HTML tags (simplistic regex for script/html block stripping)
    clean_html = re.compile('<.*?>')
    clean_text = re.sub(clean_html, '', clean_text)

    return clean_text.strip()
