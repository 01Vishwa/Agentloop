"""General utility functions and helpers."""

import math
from typing import Any


def sanitize_floats(obj: Any) -> Any:
    """Recursively replaces non-JSON-compliant float values with None.

    Python's ``json`` module rejects ``float('nan')``, ``float('inf')``, and
    ``float('-inf')``.  This function walks the data tree returned by parsers
    and substitutes any such value with ``None`` so FastAPI can always
    serialize the response cleanly.

    Args:
        obj: Any Python object (dict, list, float, str, …).

    Returns:
        A deep copy of *obj* with all nan/inf floats replaced by ``None``.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_floats(item) for item in obj]
    return obj

