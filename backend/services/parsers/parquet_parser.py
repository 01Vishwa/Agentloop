"""Parquet file parser.

Handles ingestion of Apache Parquet files (the dominant format in data science
benchmarks such as DABStep and KramaBench) using pandas + pyarrow.

Emits the full FileAnalyzerAgent-compatible metadata schema.
"""

from typing import Any, Dict
import io

from core.validation import sanitize_text
from services.parsers import sanitize_records


def parse_parquet(file_name: str, file_content: bytes) -> Dict[str, Any]:
    """Parses a Parquet file in-memory into UnifiedDocumentContext format.

    Args:
        file_name: The filename string.
        file_content: Raw binary stream of the Parquet file.

    Returns:
        Dict[str, Any]: Unified data structure matching FileAnalyzerAgent schema.

    Raises:
        ValueError: If the file cannot be parsed.
    """
    try:
        import pandas as pd  # pylint: disable=import-outside-toplevel

        df = pd.read_parquet(io.BytesIO(file_content))

        raw_text = df.head(20).to_string()
        sanitized = sanitize_text(raw_text)

        dtypes_map = {col: str(dtype) for col, dtype in df.dtypes.items()}

        return {
            "file_name": file_name,
            "source_type": "parquet",
            "sanitized_content": sanitized[:5000],
            "metadata": {
                "columns": list(df.columns),
                "dtypes": dtypes_map,
                "shape": list(df.shape),
                "row_count": len(df),
                "sample_rows": sanitize_records(df, n=5),
            },
        }
    except ImportError as exc:
        raise ValueError(
            "pyarrow is required to parse Parquet files. "
            "Install it with: pip install pyarrow"
        ) from exc
    except Exception as exc:
        raise ValueError(f"Failed to parse Parquet file: {exc}") from exc
