"""Excel file parser (XLSX).

Handles ingestion and parsing of Excel spreadsheet files into standardised
structured dictionaries using pandas and openpyxl.

Fix: Added ``dtypes``, ``row_count``, ``shape``, and ``sample_rows`` metadata
keys, and capped sheet loading to the first 3 sheets to avoid OOM on huge
workbooks.
"""

from typing import Any, Dict
import io

import pandas as pd

from core.validation import sanitize_text
from services.parsers import sanitize_records


def parse_excel(file_name: str, file_content: bytes) -> Dict[str, Any]:
    """Parses an XLSX file in-memory into a standard dictionary format.

    Args:
        file_name: The filename of the Excel document.
        file_content: Raw binary stream of the Excel sheet.

    Returns:
        Dict[str, Any]: Unified structured format with metadata.

    Raises:
        ValueError: If file cannot be parsed or dependencies are missing.
    """
    try:
        excel_data = pd.read_excel(io.BytesIO(file_content), sheet_name=None)
        sheets = list(excel_data.keys())

        # Limit to first 3 sheets to avoid OOM on large workbooks
        sheets_to_process = sheets[:3]
        raw_text_blocks = []
        for s_name in sheets_to_process:
            _df = excel_data[s_name]
            raw_text_blocks.append(
                f"--- Sheet: {s_name} ---\n{_df.head(20).to_string()}"
            )

        full_text = "\n".join(raw_text_blocks)
        sanitized = sanitize_text(full_text)

        # Use the first sheet for primary metadata
        first_df = excel_data[sheets[0]] if sheets else pd.DataFrame()
        dtypes_map = {col: str(dtype) for col, dtype in first_df.dtypes.items()}

        return {
            "file_name": file_name,
            "source_type": "xlsx",
            "sanitized_content": sanitized[:5000],
            "metadata": {
                # Keys matched to FileAnalyzerAgent contract
                "columns": list(first_df.columns),
                "dtypes": dtypes_map,
                "shape": list(first_df.shape),        # [rows, cols]
                "row_count": len(first_df),
                "sample_rows": sanitize_records(first_df, n=5),
                # XLSX-specific extras
                "sheet_count": len(sheets),
                "sheet_names": sheets,
                # Legacy key kept for any downstream consumers
                "preview_first_sheet": sanitize_records(first_df, n=5),
            },
        }
    except Exception as exc:
        raise ValueError(f"Failed to parse Excel file: {exc}") from exc
