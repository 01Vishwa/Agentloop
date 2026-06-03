"""Schema merger service — infers join candidates from stored file metadata.

Public API
----------
    async def merge_schemas(file_metas: List[dict]) -> MultiFileContext

``file_metas`` is a list of dicts matching the ``workspace_files`` table row
shape (file_name, file_path, file_type, row_count, schema_json, upload_order).

The function assigns variable names df1…dfN by ``upload_order`` (ascending),
builds ``FileSchema`` objects from the stored ``schema_json``, then runs three
join-inference passes over every pair of files (i < j):

  Pass 1 — Exact normalised name match            → confidence 0.95
  Pass 2 — Same dtype + sample value overlap >30% → confidence 0.70
  Pass 3 — difflib fuzzy ratio > 0.80             → confidence 0.55

After all passes the candidate list is deduplicated (highest confidence wins),
sorted descending, and capped at 5 entries.

No file I/O is performed — all inference is done from ``schema_json`` alone.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from models.multi_file_context import (
    ColumnMeta,
    FileSchema,
    JoinCandidate,
    MultiFileContext,
)

logger = logging.getLogger("uvicorn.info")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_CANDIDATES = 5
_FUZZY_RATIO_THRESHOLD = 0.80
_SAMPLE_OVERLAP_THRESHOLD = 0.30

_CONFIDENCE_EXACT = 0.95
_CONFIDENCE_SAMPLE = 0.70
_CONFIDENCE_FUZZY = 0.55


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_col(name: str) -> str:
    """Normalises a column name for exact-match comparison.

    Steps:
      1. Lowercase
      2. Strip leading/trailing whitespace
      3. Replace spaces and hyphens with underscores

    Args:
        name: Raw column name from the schema.

    Returns:
        Normalised column name string.
    """
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def _build_file_schema(meta: Dict[str, Any], var_name: str) -> FileSchema:
    """Constructs a FileSchema from a workspace_files row dict.

    The ``schema_json`` field stores the parser's ``metadata`` dict which
    follows the ``UnifiedDocumentContext`` shape::

        {
          "columns": ["col_a", "col_b", ...],
          "dtypes":  {"col_a": "int64", "col_b": "object", ...},
          "sample_rows": [{"col_a": 1, "col_b": "x"}, ...],
          ...
        }

    Args:
        meta: A workspace_files table row dict.
        var_name: Python variable name to assign (e.g. 'df1').

    Returns:
        Populated FileSchema instance.
    """
    schema_json: Dict[str, Any] = meta.get("schema_json") or {}

    columns_list: List[str] = schema_json.get("columns") or []
    dtypes_map: Dict[str, str] = schema_json.get("dtypes") or {}
    sample_rows: List[Dict[str, Any]] = schema_json.get("sample_rows") or []

    col_metas: List[ColumnMeta] = []
    for col_name in columns_list:
        dtype = dtypes_map.get(col_name, "object")
        # Collect up to 5 unique non-null sample values for this column
        samples: List[str] = []
        seen: set = set()
        for row in sample_rows:
            val = row.get(col_name)
            if val is None:
                continue
            val_str = str(val)[:50]
            if val_str not in seen:
                seen.add(val_str)
                samples.append(val_str)
            if len(samples) >= 5:
                break

        col_metas.append(ColumnMeta(name=col_name, dtype=dtype, sample_values=samples))

    return FileSchema(
        var_name=var_name,
        file_name=meta.get("file_name", "unknown"),
        file_path=meta.get("file_path", ""),
        file_type=meta.get("file_type", "csv"),
        row_count=meta.get("row_count") or 0,
        columns=col_metas,
    )


def _candidate_key(left_col: str, right_col: str, left_var: str, right_var: str) -> str:
    """Returns a stable deduplication key for a join candidate."""
    return f"{left_var}.{left_col}:{right_var}.{right_col}"


# ---------------------------------------------------------------------------
# The three inference passes
# ---------------------------------------------------------------------------

def _pass1_exact(
    fs_i: FileSchema,
    fs_j: FileSchema,
) -> List[JoinCandidate]:
    """Pass 1: Exact normalised name match.

    Args:
        fs_i: Left file schema.
        fs_j: Right file schema.

    Returns:
        List of JoinCandidates with confidence=0.95.
    """
    candidates: List[JoinCandidate] = []
    norm_j = {_normalise_col(c.name): c.name for c in fs_j.columns}

    for col_i in fs_i.columns:
        norm_i = _normalise_col(col_i.name)
        if norm_i in norm_j:
            candidates.append(
                JoinCandidate(
                    left_var=fs_i.var_name,
                    right_var=fs_j.var_name,
                    left_col=col_i.name,
                    right_col=norm_j[norm_i],
                    confidence=_CONFIDENCE_EXACT,
                    reason="exact name match after normalisation",
                )
            )
    return candidates


def _pass2_sample_overlap(
    fs_i: FileSchema,
    fs_j: FileSchema,
) -> List[JoinCandidate]:
    """Pass 2: Matching dtype + sample value set intersection > 30%.

    Args:
        fs_i: Left file schema.
        fs_j: Right file schema.

    Returns:
        List of JoinCandidates with confidence=0.70.
    """
    candidates: List[JoinCandidate] = []

    for col_i in fs_i.columns:
        if not col_i.sample_values:
            continue
        set_i = set(col_i.sample_values)

        for col_j in fs_j.columns:
            if not col_j.sample_values:
                continue
            # Dtypes must match (normalised: strip the bit-width suffix for int/float comparison)
            dtype_i = col_i.dtype.lower().rstrip("0123456789")
            dtype_j = col_j.dtype.lower().rstrip("0123456789")
            if dtype_i != dtype_j:
                continue

            set_j = set(col_j.sample_values)
            intersection = set_i & set_j
            overlap_ratio = len(intersection) / min(len(set_i), len(set_j))

            if overlap_ratio > _SAMPLE_OVERLAP_THRESHOLD:
                pct = int(overlap_ratio * 100)
                candidates.append(
                    JoinCandidate(
                        left_var=fs_i.var_name,
                        right_var=fs_j.var_name,
                        left_col=col_i.name,
                        right_col=col_j.name,
                        confidence=_CONFIDENCE_SAMPLE,
                        reason=(
                            f"matching dtype and overlapping sample values ({pct}% overlap)"
                        ),
                    )
                )
    return candidates


def _pass3_fuzzy(
    fs_i: FileSchema,
    fs_j: FileSchema,
    existing_keys: set,
) -> List[JoinCandidate]:
    """Pass 3: difflib fuzzy name ratio > 0.80.

    Only emits a candidate if no higher-confidence candidate already exists
    for the same column pair.

    Args:
        fs_i: Left file schema.
        fs_j: Right file schema.
        existing_keys: Set of candidate keys already found in Passes 1 & 2.

    Returns:
        List of JoinCandidates with confidence=0.55.
    """
    candidates: List[JoinCandidate] = []

    for col_i in fs_i.columns:
        for col_j in fs_j.columns:
            key = _candidate_key(col_i.name, col_j.name, fs_i.var_name, fs_j.var_name)
            if key in existing_keys:
                continue  # Already covered by a higher-confidence pass

            ratio = difflib.SequenceMatcher(
                None, col_i.name.lower(), col_j.name.lower()
            ).ratio()

            if ratio > _FUZZY_RATIO_THRESHOLD:
                candidates.append(
                    JoinCandidate(
                        left_var=fs_i.var_name,
                        right_var=fs_j.var_name,
                        left_col=col_i.name,
                        right_col=col_j.name,
                        confidence=_CONFIDENCE_FUZZY,
                        reason=f"fuzzy name match (ratio: {ratio:.2f})",
                    )
                )
    return candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def merge_schemas(file_metas: List[Dict[str, Any]]) -> MultiFileContext:
    """Infers join candidates from stored workspace_files metadata.

    Runs three passes over every ordered file pair (i < j), deduplicates
    by highest confidence, and returns a MultiFileContext with at most
    ``_MAX_CANDIDATES`` join candidates.

    This function is intentionally pure (no I/O beyond CPU work) and
    therefore needs no async I/O — the ``async def`` signature is kept for
    API consistency with the async controller layer.

    Args:
        file_metas: List of workspace_files row dicts, one per uploaded file.
            Must include at minimum: file_name, file_path, file_type,
            row_count, schema_json, upload_order.

    Returns:
        MultiFileContext with populated files and join_candidates.
    """
    if not file_metas:
        logger.info("[SchemaMerger] No file metas provided — returning empty context.")
        return MultiFileContext(files=[], join_candidates=[])

    # Sort by upload_order ascending to assign df1, df2, ... deterministically
    sorted_metas = sorted(file_metas, key=lambda m: m.get("upload_order", 1))

    # Build FileSchema objects
    file_schemas: List[FileSchema] = []
    for idx, meta in enumerate(sorted_metas, start=1):
        var_name = f"df{idx}"
        fs = _build_file_schema(meta, var_name)
        file_schemas.append(fs)
        logger.debug(
            "[SchemaMerger] Built schema for %s (%s) with %d columns",
            fs.file_name,
            var_name,
            len(fs.columns),
        )

    # Single file — no join candidates possible
    if len(file_schemas) == 1:
        logger.info(
            "[SchemaMerger] Single file workspace — skipping join inference."
        )
        return MultiFileContext(files=file_schemas, join_candidates=[])

    # --- Run all three passes over every pair (i < j) ---
    # best_candidates: key → (confidence, JoinCandidate)
    best_candidates: Dict[str, Tuple[float, JoinCandidate]] = {}

    for i in range(len(file_schemas)):
        for j in range(i + 1, len(file_schemas)):
            fs_i = file_schemas[i]
            fs_j = file_schemas[j]

            # Pass 1: Exact normalised match
            for cand in _pass1_exact(fs_i, fs_j):
                key = _candidate_key(cand.left_col, cand.right_col, cand.left_var, cand.right_var)
                if key not in best_candidates or cand.confidence > best_candidates[key][0]:
                    best_candidates[key] = (cand.confidence, cand)

            # Pass 2: Dtype + sample overlap
            for cand in _pass2_sample_overlap(fs_i, fs_j):
                key = _candidate_key(cand.left_col, cand.right_col, cand.left_var, cand.right_var)
                if key not in best_candidates or cand.confidence > best_candidates[key][0]:
                    best_candidates[key] = (cand.confidence, cand)

            # Pass 3: Fuzzy name match (skip already-found pairs)
            existing_keys = set(best_candidates.keys())
            for cand in _pass3_fuzzy(fs_i, fs_j, existing_keys):
                key = _candidate_key(cand.left_col, cand.right_col, cand.left_var, cand.right_var)
                if key not in best_candidates or cand.confidence > best_candidates[key][0]:
                    best_candidates[key] = (cand.confidence, cand)

    # Deduplicate, sort by confidence descending, cap at _MAX_CANDIDATES
    final_candidates = sorted(
        (cand for _, cand in best_candidates.values()),
        key=lambda c: c.confidence,
        reverse=True,
    )[:_MAX_CANDIDATES]

    logger.info(
        "[SchemaMerger] %d file(s) → %d join candidate(s) (capped at %d).",
        len(file_schemas),
        len(final_candidates),
        _MAX_CANDIDATES,
    )

    return MultiFileContext(files=file_schemas, join_candidates=final_candidates)
