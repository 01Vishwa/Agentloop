"""Multi-file context models for the workspace join feature.

Defines the canonical Pydantic models used throughout the multi-file
pipeline:

  ColumnMeta       — one column from a parsed file schema
  FileSchema       — full schema representation of one uploaded file
  JoinCandidate    — a probable join key pair between two files
  MultiFileContext — container passed between Analyzer → Planner → Coder

The ``to_prompt_str()`` method on MultiFileContext produces a compact,
LLM-readable summary under 600 tokens regardless of file count.  It is
injected into agent prompts as the authoritative multi-file schema context.
"""

from __future__ import annotations

import textwrap
from typing import List

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Column-level metadata
# ---------------------------------------------------------------------------

class ColumnMeta(BaseModel):
    """Metadata for a single column in a parsed file."""

    name: str = Field(description="Column name as it appears in the file header.")
    dtype: str = Field(description="Pandas dtype string, e.g. 'int64', 'object', 'float64'.")
    sample_values: List[str] = Field(
        default_factory=list,
        description="Up to 5 representative non-null values serialised as strings.",
    )


# ---------------------------------------------------------------------------
# Per-file schema
# ---------------------------------------------------------------------------

class FileSchema(BaseModel):
    """Full schema representation of one uploaded workspace file."""

    var_name: str = Field(
        description="Python variable name assigned to this file, e.g. 'df1', 'df2'."
    )
    file_name: str = Field(description="Original filename, e.g. 'orders.csv'.")
    file_path: str = Field(description="Absolute path to the file on disk.")
    file_type: str = Field(description="One of: csv, xlsx, parquet, md, json.")
    row_count: int = Field(default=0, description="Number of rows in the file.")
    columns: List[ColumnMeta] = Field(
        default_factory=list,
        description="Column schemas for this file.",
    )


# ---------------------------------------------------------------------------
# Join candidate
# ---------------------------------------------------------------------------

class JoinCandidate(BaseModel):
    """A probable join key relationship between two files."""

    left_var: str = Field(description="Variable name of the left-hand file, e.g. 'df1'.")
    right_var: str = Field(description="Variable name of the right-hand file, e.g. 'df2'.")
    left_col: str = Field(description="Column name in the left file.")
    right_col: str = Field(description="Column name in the right file.")
    confidence: float = Field(
        description="Confidence score in [0.0, 1.0].  Higher = more likely a valid join key.",
        ge=0.0,
        le=1.0,
    )
    reason: str = Field(
        description="Human-readable explanation of why this candidate was selected."
    )


# ---------------------------------------------------------------------------
# Top-level container
# ---------------------------------------------------------------------------

# Maximum columns to include per file in the prompt string before truncating.
_MAX_COLS_PER_FILE = 8
# Maximum sample values to show per column in the prompt string.
_MAX_SAMPLES_PER_COL = 3


def _score_column_for_prompt(col: ColumnMeta) -> float:
    """Heuristic priority score for selecting columns shown in to_prompt_str().

    Numeric columns and low-cardinality strings are most likely to be join
    keys or analysis targets, so they rank highest.

    Args:
        col: ColumnMeta to score.

    Returns:
        Float score — higher is more important to show.
    """
    dtype_lower = col.dtype.lower()
    is_numeric = any(k in dtype_lower for k in ("int", "float", "decimal", "numeric"))
    is_object = "object" in dtype_lower or "string" in dtype_lower
    unique_samples = len(set(col.sample_values))
    # Low cardinality = likely a key or categorical column
    is_low_cardinality = is_object and unique_samples <= 5
    return (
        3.0 if is_numeric else 2.0 if is_low_cardinality else 1.0
    )


class MultiFileContext(BaseModel):
    """Container for all per-file schemas and inferred join candidates.

    Passed between the schema_merger, Analyzer, Planner, and Coder so that
    every agent stage has the same ground-truth multi-file view.
    """

    files: List[FileSchema] = Field(
        default_factory=list,
        description="One FileSchema per uploaded file, ordered by upload_order.",
    )
    join_candidates: List[JoinCandidate] = Field(
        default_factory=list,
        description="Inferred join key pairs, sorted by confidence descending.",
    )

    def to_prompt_str(self) -> str:
        """Compact, LLM-readable summary of the multi-file context.

        Output format::

            FILES:
              df1 = orders.csv (12450 rows) | columns: order_id(int64), customer_id(int64), amount(float64), ...
              df2 = customers.csv (3200 rows) | columns: cust_id(int64), name(object), region(object), ...
            JOIN CANDIDATES (by confidence):
              df1['customer_id'] <-> df2['cust_id']  [0.95 — exact name match after normalisation]
              df1['region_code'] <-> df2['region']   [0.60 — fuzzy name match (ratio: 0.83)]

        Truncation rules (to stay under 600 tokens):
        - At most ``_MAX_COLS_PER_FILE`` columns per file (highest-priority first).
        - At most ``_MAX_SAMPLES_PER_COL`` sample values per column.
        - Sample values truncated to 20 characters each.

        Returns:
            Formatted multi-line string safe to inject directly into any
            LLM prompt as a context block.
        """
        lines: List[str] = ["FILES:"]

        for fs in self.files:
            # Select top columns by priority score
            ranked = sorted(fs.columns, key=_score_column_for_prompt, reverse=True)
            shown = ranked[:_MAX_COLS_PER_FILE]
            truncated = len(fs.columns) - len(shown)

            col_parts: List[str] = []
            for col in shown:
                samples = col.sample_values[:_MAX_SAMPLES_PER_COL]
                samples_str = ", ".join(str(v)[:20] for v in samples)
                samples_display = f" [{samples_str}]" if samples_str else ""
                col_parts.append(f"{col.name}({col.dtype}){samples_display}")

            col_str = ", ".join(col_parts)
            if truncated > 0:
                col_str += f", ...+{truncated} more"

            lines.append(
                f"  {fs.var_name} = {fs.file_name} ({fs.row_count:,} rows)"
                f" | columns: {col_str}"
            )

        if self.join_candidates:
            lines.append("JOIN CANDIDATES (by confidence):")
            for jc in self.join_candidates:
                lines.append(
                    f"  {jc.left_var}['{jc.left_col}'] <-> "
                    f"{jc.right_var}['{jc.right_col}']"
                    f"  [{jc.confidence:.2f} — {jc.reason}]"
                )
        else:
            lines.append("JOIN CANDIDATES: (none detected)")

        return "\n".join(lines)

    def to_reader_header(self, workspace_id: str) -> str:
        """Generates the Python data-loading header for the CoderAgent.

        Produces one ``pd.read_*`` call per file, using the correct reader
        for each file_type.  The generated variable names match ``var_name``
        fields exactly so the coder's subsequent code can reference them.

        Args:
            workspace_id: Workspace UUID used to resolve the storage path.

        Returns:
            Multi-line Python source string that pre-loads all DataFrames.
        """
        reader_map = {
            "csv": "pd.read_csv",
            "xlsx": "pd.read_excel",
            "parquet": "pd.read_parquet",
            "json": "pd.read_json",
            "md": None,  # handled specially below
        }

        lines = ["import pandas as pd", "import os", ""]

        for fs in self.files:
            path_expr = repr(fs.file_path)
            ft = fs.file_type.lower()
            reader = reader_map.get(ft)

            if reader is None:
                # Markdown: extract first table using regex fallback
                lines.append(
                    textwrap.dedent(f"""\
                    # Load markdown file and extract the first table
                    _md_path_{fs.var_name} = {path_expr}
                    _md_lines = open(_md_path_{fs.var_name}, encoding='utf-8', errors='replace').readlines()
                    _md_table_lines = [l for l in _md_lines if l.strip().startswith('|')]
                    if _md_table_lines:
                        import io as _io
                        _md_csv = '\\n'.join(
                            ','.join(c.strip() for c in row.strip().strip('|').split('|'))
                            for row in _md_table_lines
                            if not set(row.replace('|', '').replace('-', '').replace(' ', '')) == set()
                        )
                        {fs.var_name} = pd.read_csv(_io.StringIO(_md_csv))
                    else:
                        {fs.var_name} = pd.DataFrame()
                        print('WARNING: No markdown table found in {fs.file_name}')
                    """)
                )
            else:
                lines.append(f"{fs.var_name} = {reader}({path_expr})")

        lines.append("")
        # Verification prints required by the VerifierAgent
        for fs in self.files:
            lines.append(
                f"print(f'{fs.var_name} shape: {{{fs.var_name}.shape}}')"
            )
        lines.append("")

        return "\n".join(lines)
