"""PDF document parser (pypdf).

Extracts text from PDF files using pypdf, which is pure python and avoids C extension issues.

Fix: Renamed ``page_count`` → ``pages`` to match the key contract expected
by FileAnalyzerAgent.  Also changed from a hard 10-page cap to a character-
budget approach (all pages, up to 20 000 chars) so long technical documents
aren't arbitrarily truncated at page 10.

BUG 4 fix: Raises ParseWarning when the extracted text is near-empty, which
typically happens with scan-only PDFs or encrypted documents, preventing
silent empty-context hallucinations in the agent loop.
"""

import io
from typing import Any, Dict

import pypdf

from core.validation import sanitize_text
from services.parsers import ParseWarning

# Maximum characters to extract across all pages
_MAX_CHARS: int = 20_000
# Minimum meaningful characters before a ParseWarning is raised
_MIN_CONTENT_CHARS: int = 50


def parse_pdf(file_name: str, file_content: bytes) -> Dict[str, Any]:
    """Extracts text content from a PDF file in-memory.

    Args:
        file_name: Original filename of the PDF.
        file_content: Raw binary stream of the PDF file.

    Returns:
        Dict[str, Any]: Structured unified document holding text blocks.

    Raises:
        ValueError: If the file is unreadable (corrupt, wrong format).
        ParseWarning: If the extracted text is shorter than _MIN_CONTENT_CHARS,
            indicating a scan-only or encrypted PDF where text extraction failed.
    """
    try:
        content_accumulator = []
        page_count = 0
        total_chars = 0

        pdf_stream = io.BytesIO(file_content)
        reader = pypdf.PdfReader(pdf_stream)
        page_count = len(reader.pages)

        for page in reader.pages:
            text = page.extract_text()
            if text:
                content_accumulator.append(text)
                total_chars += len(text)
            # Stop extracting once we have enough context
            if total_chars >= _MAX_CHARS:
                break

        full_text = "\n".join(content_accumulator)
        sanitized = sanitize_text(full_text)

    except pypdf.errors.PdfReadError as exc:
        raise ValueError(f"PDF is corrupt or cannot be read: {exc}") from exc
    except Exception as exc:  # pylint: disable=broad-except
        raise ValueError(f"Failed to parse PDF via pypdf: {exc}") from exc

    if len(sanitized.strip()) < _MIN_CONTENT_CHARS:
        raise ParseWarning(
            f"'{file_name}' yielded near-empty text after PDF extraction "
            f"({len(sanitized.strip())} chars across {page_count} page(s)). "
            "The PDF may be scan-only (image-based) or password-protected. "
            "Agent context will be minimal.",
            filename=file_name,
            char_count=len(sanitized.strip()),
        )

    return {
        "file_name": file_name,
        "source_type": "pdf",
        "sanitized_content": sanitized[:8000],   # higher cap than original 5000
        "metadata": {
            "pages": page_count,              # matches analyzer L61 ("pages")
            "text_preview": sanitized[:1000],
        },
    }

