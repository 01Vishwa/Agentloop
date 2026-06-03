"""Parser package — exposes ParseWarning for callers to handle near-empty results.

BUG 4 fix: Parsers now raise ParseWarning (a subclass of UserWarning, not
Exception) when they produce content that is syntactically valid but so sparse
that passing it to the LLM would likely cause hallucinations.  This keeps the
normal ValueError path for hard failures and adds a separate, named warning
path for graceful degradation.
"""


import math
from typing import Any, Dict, List

import pandas as pd


def sanitize_records(df: pd.DataFrame, n: int = 5) -> List[Dict[str, Any]]:
    """Converts a DataFrame head to a list of dicts with NaN/Inf replaced by None.

    The JSON specification does not support ``NaN``, ``Infinity``, or
    ``-Infinity``.  Supabase (and ``json.dumps`` in strict mode) will
    reject any payload containing these sentinel values.  This helper
    ensures all parser outputs are JSON-safe before they reach the DB
    insert layer.

    Args:
        df: Source DataFrame.
        n: Number of head rows to include.

    Returns:
        List of record dicts safe for ``json.dumps()``.
    """
    records = df.head(n).to_dict(orient="records")
    return _sanitize_value(records)


def _sanitize_value(obj: Any) -> Any:
    """Recursively replace non-JSON-compliant floats with None."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_value(item) for item in obj]
    return obj


class ParseWarning(UserWarning):
    """Raised when a parser succeeds but yields near-empty or unusable content.

    Propagated by process_service.process_documents() as an ``warnings``
    warning, caught at the API layer, and emitted as an SSE ``"warning"`` event
    so the user knows their file produced minimal context *before* the agent
    loop begins.

    Args:
        message: Human-readable description of why the content is insufficient.
        filename: The file that triggered the warning.
        char_count: Number of meaningful characters actually extracted.
    """

    def __init__(self, message: str, filename: str = "", char_count: int = 0) -> None:
        super().__init__(message)
        self.filename = filename
        self.char_count = char_count
