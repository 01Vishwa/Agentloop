"""Document processing service.

Routes files to specific format parsers, retrieving in-memory bytes and
aggregating output into a normalised schema map matching UnifiedDocumentContext.

Added: Parquet (.parquet) format support.

BUG 4 fix: process_documents() now distinguishes between hard parse failures
(ValueError → stored as an error entry) and near-empty parse warnings
(ParseWarning → stored as a successful entry *plus* a warning message in the
returned ``parse_warnings`` list).  The API layer is responsible for emitting
each warning as an SSE "warning" event before the agent loop begins, ensuring
users know their file produced minimal context rather than silently receiving
hallucinated column names from the LLM.
"""


from typing import Any, Dict, List

from services.parsers import ParseWarning
from services.parsers.csv_parser import parse_csv
from services.parsers.txt_parser import parse_txt
from services.parsers.excel_parser import parse_excel
from services.parsers.pdf_parser import parse_pdf
from services.parsers.json_parser import parse_json
from services.parsers.md_parser import parse_md
from services.parsers.parquet_parser import parse_parquet
from services.upload_service import get_file_content
from utils.helpers import sanitize_floats


def _get_extension(filename: str) -> str:
    """Safely extracts the lowercase file extension.

    Args:
        filename: The filename string.

    Returns:
        Lowercase extension, or empty string if none found.
    """
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def process_documents(
    file_names: List[str],
    session_id: str = "__anon__",
) -> Dict[str, Any]:
    """Routes files from the session cache to parsing logic.

    BUG 4 fix: ParseWarning from individual parsers is captured per-file and
    stored in ``aggregated_context["parse_warnings"]``.  Hard failures
    (ValueError and unexpected exceptions) continue to be stored as error
    entries in ``combined_extractions``.  Callers (the agent controller /
    research controller) must iterate ``parse_warnings`` and emit each message
    as an SSE ``"warning"`` event before starting the orchestrator.

    Args:
        file_names: Filenames matching keys in the session's file cache.
        session_id: Session identifier used to look up files from the
            session-scoped cache.

    Returns:
        Dict[str, Any]: A unified memory map of normalised contextual documents,
        guaranteed to contain no nan/inf float values, plus a ``parse_warnings``
        list of human-readable warning strings (may be empty).
    """
    aggregated_context: Dict[str, Any] = {
        "files_processed": 0,
        "combined_extractions": {},
        # BUG 4: new key — list of (filename, message) tuples for near-empty results
        "parse_warnings": [],
    }

    parser_map = {
        "csv": parse_csv,
        "txt": parse_txt,
        "xlsx": parse_excel,
        "pdf": parse_pdf,
        "json": parse_json,
        "md": parse_md,
        "parquet": parse_parquet,    # NEW: Parquet support for benchmark datasets
    }

    for filename in file_names:
        ext = _get_extension(filename)

        parser_fn = parser_map.get(ext)
        file_content = get_file_content(filename, session_id=session_id)

        if file_content is None:
            aggregated_context["combined_extractions"][filename] = {
                "file_name": filename,
                "source_type": ext,
                "sanitized_content": "",
                "metadata": {"error": f"File '{filename}' missing from memory cache."},
            }
            continue

        if parser_fn:
            try:
                # BUG 4: Use warnings.catch_warnings to intercept ParseWarning
                # without breaking the normal parse path.  ParseWarning is
                # raised (not warned) by parsers, so we catch it explicitly.
                extraction = parser_fn(filename, file_content)
                aggregated_context["combined_extractions"][filename] = extraction
                aggregated_context["files_processed"] += 1

            except ParseWarning as pw:
                # Near-empty result: store the best-effort extraction we can
                # reconstruct (empty sanitized_content) AND log the warning so
                # the caller can surface it before the agent loop starts.
                aggregated_context["combined_extractions"][filename] = {
                    "file_name": filename,
                    "source_type": ext,
                    "sanitized_content": "",
                    "metadata": {
                        "warning": str(pw),
                        "char_count": getattr(pw, "char_count", 0),
                    },
                }
                aggregated_context["parse_warnings"].append({
                    "filename": filename,
                    "message": str(pw),
                })
                # Count as processed — the file *was* readable, just sparse
                aggregated_context["files_processed"] += 1

            except ValueError as exc:
                # Hard parse failure — file is unreadable or corrupt
                aggregated_context["combined_extractions"][filename] = {
                    "file_name": filename,
                    "source_type": ext,
                    "sanitized_content": "",
                    "metadata": {"error": f"Parse failure: {str(exc)}"},
                }

            except Exception as exc:  # pylint: disable=broad-except
                # Unexpected error — treat as hard failure
                aggregated_context["combined_extractions"][filename] = {
                    "file_name": filename,
                    "source_type": ext,
                    "sanitized_content": "",
                    "metadata": {"error": f"Unexpected parse error: {str(exc)}"},
                }
        else:
            aggregated_context["combined_extractions"][filename] = {
                "file_name": filename,
                "source_type": ext,
                "sanitized_content": "",
                "metadata": {"error": f"No parser available for .{ext} files."},
            }

    return sanitize_floats(aggregated_context)
