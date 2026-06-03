"""TXT file parser.

Reads standard text files and returns raw blocks filtered through
the unified document context schemas and sanitizers.

BUG 4 fix: Raises ParseWarning when the extracted text is near-empty (< 50
characters), allowing process_service to surface the issue as an SSE warning
before the agent loop starts rather than silently passing empty context.
"""

from typing import Dict, Any

from core.validation import sanitize_text
from services.parsers import ParseWarning

_MIN_CONTENT_CHARS: int = 50


def parse_txt(file_name: str, file_content: bytes) -> Dict[str, Any]:
    """Parses a raw text buffer into a structured dictionary format.

    Args:
        file_name (str): Original filename structure.
        file_content (bytes): Content buffer to inject.

    Returns:
        Dict[str, Any]: Unified format holding character counts
        and a preview of the text.

    Raises:
        ValueError: If string decode format is misaligned.
        ParseWarning: If the content is shorter than _MIN_CONTENT_CHARS.
    """
    try:
        content = file_content.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f"Failed to decode TXT file as UTF-8: {exc}") from exc

    try:
        sanitized = sanitize_text(content)
    except Exception as exc:  # pylint: disable=broad-except
        raise ValueError(f"Failed to sanitize TXT content: {exc}") from exc

    if len(sanitized.strip()) < _MIN_CONTENT_CHARS:
        raise ParseWarning(
            f"'{file_name}' produced near-empty content after parsing "
            f"({len(sanitized.strip())} chars). The file may be blank. "
            "Agent context will be minimal.",
            filename=file_name,
            char_count=len(sanitized.strip()),
        )

    return {
        "file_name": file_name,
        "source_type": "txt",
        "sanitized_content": sanitized[:5000],
        "metadata": {
            "char_count": len(sanitized),
            "preview": sanitized[:100] + "..." if len(sanitized) > 100 else sanitized
        }
    }

