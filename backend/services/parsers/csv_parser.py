"""CSV file parser.

Handles ingestion and basic parsing of comma-separated value files into
standardised structured dictionaries aligning with UnifiedDocumentContext.

Fix: Added ``dtypes``, ``row_count``, ``shape``, and ``sample_rows`` metadata
keys to align with the key contract expected by FileAnalyzerAgent.
"""

from typing import Any, Dict
import io

import pandas as pd

from core.validation import sanitize_text
from services.parsers import sanitize_records


def parse_csv(file_name: str, file_content: bytes) -> Dict[str, Any]:
    """Parses a CSV file in-memory into UnifiedDocumentContext format.

    Args:
        file_name: The filename string.
        file_content: Raw binary stream of the CSV file.

    Returns:
        Dict[str, Any]: The parsed unified data structure.

    Raises:
        ValueError: If the file cannot be parsed.
    """
    try:
        df = pd.read_csv(io.BytesIO(file_content))

        # Use a compact head preview — not df.to_string() which can be MBs
        raw_text = df.head(20).to_string()
        sanitized = sanitize_text(raw_text)

        # Serialize dtypes as a readable dict (str keys, str values)
        dtypes_map = {col: str(dtype) for col, dtype in df.dtypes.items()}

        # sanitize_records replaces NaN/Inf with None so the result is
        # JSON-serializable (required for Supabase schema_json column).
        safe_sample = sanitize_records(df, n=5)

        return {
            "file_name": file_name,
            "source_type": "csv",
            "sanitized_content": sanitized[:5000],
            "metadata": {
                # Keys matched to FileAnalyzerAgent contract
                "columns": list(df.columns),
                "dtypes": dtypes_map,
                "shape": list(df.shape),           # [rows, cols]
                "row_count": len(df),
                "sample_rows": safe_sample,
                # Legacy key kept for any downstream consumers
                "sample_data": safe_sample,
            },
        }
    except Exception as exc:
        raise ValueError(f"Failed to parse CSV: {exc}") from exc

