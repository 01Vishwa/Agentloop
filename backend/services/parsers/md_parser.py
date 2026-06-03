"""Markdown (.md) parser.

Extracts and parses standard Markdown text files into unified context, mapping
the logic via the `markdown` module for extended functionality later.

BUG 4 fix: Raises ParseWarning when the extracted text is near-empty (< 50
characters), allowing process_service to surface the issue as an SSE warning
before the agent loop starts rather than silently passing empty context.
"""

from typing import Dict, Any
import markdown

from core.validation import sanitize_text
from services.parsers import ParseWarning

# Minimum meaningful character count before we emit a ParseWarning
_MIN_CONTENT_CHARS: int = 50


def parse_md(file_name: str, file_content: bytes) -> Dict[str, Any]:
    """Ingests Markdown representations directly from memory buffers.

    Args:
        file_name (str): Original filename of the Markdown file.
        file_content (bytes): The raw Markdown text bytes.

    Returns:
        Dict[str, Any]: The extracted text formatted unified.

    Raises:
        ValueError: If the file cannot be decoded or the markdown library fails.
        ParseWarning: If the extracted content is shorter than _MIN_CONTENT_CHARS,
            indicating the file is likely empty or contains only whitespace/markup.
    """
    try:
        content = file_content.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f"Failed to decode Markdown as UTF-8: {exc}") from exc

    try:
        # Parse into HTML to strip raw structural blocks, then pass to sanitizer
        html_converted = markdown.markdown(content)
        sanitized = sanitize_text(html_converted)
    except Exception as exc:  # pylint: disable=broad-except
        raise ValueError(f"Failed to parse Markdown: {exc}") from exc

    if len(sanitized.strip()) < _MIN_CONTENT_CHARS:
        raise ParseWarning(
            f"'{file_name}' produced near-empty content after Markdown parsing "
            f"({len(sanitized.strip())} chars). The file may be blank or contain "
            "only structural markup. Agent context will be minimal.",
            filename=file_name,
            char_count=len(sanitized.strip()),
        )

    return {
        "file_name": file_name,
        "source_type": "md",
        "sanitized_content": sanitized[:5000],
        "metadata": {
            "char_count": len(sanitized),
            "content_preview": sanitized[:1000]
        }
    }

