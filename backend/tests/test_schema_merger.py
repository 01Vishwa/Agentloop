"""Tests for schema_merger — the join key inference service.

Covers:
  - Exact name match across two files
  - Fuzzy name match fallback
  - Dtype+sample overlap match
  - No match (empty candidates)
  - Three-file scenario (all pairs checked)
  - Single file returns empty candidates
  - Case-insensitive exact match
"""

import asyncio
import pytest

from models.multi_file_context import ColumnMeta, FileSchema, MultiFileContext


# ---------------------------------------------------------------------------
# Helpers to build minimal schema metadata rows
# ---------------------------------------------------------------------------

def _make_row(
    file_name: str,
    columns: list[dict],
    var_name: str | None = None,
    file_path: str = "/workspace/ws1/file.csv",
    file_type: str = "csv",
    row_count: int = 100,
    order: int = 1,
) -> dict:
    """Produces a workspace_files row dict suitable for merge_schemas()."""
    schema_json = {
        "columns": [c["name"] for c in columns],
        "dtypes": {c["name"]: c.get("dtype", "object") for c in columns},
        "sample_rows": [
            {c["name"]: (c.get("samples", ["x"])[0] if c.get("samples") else None) for c in columns}
        ],
        "row_count": row_count,
    }
    return {
        "file_name": file_name,
        "file_path": file_path,
        "file_type": file_type,
        "row_count": row_count,
        "schema_json": schema_json,
        "upload_order": order,
    }


# ---------------------------------------------------------------------------
# Async wrapper
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSchemaMergerExactMatch:
    """Exact column name match should produce a high-confidence candidate."""

    def test_exact_name_match_produces_candidate(self):
        from services.schema_merger import merge_schemas

        rows = [
            _make_row("orders.csv",    [{"name": "customer_id", "dtype": "int64"}],  order=1),
            _make_row("customers.csv", [{"name": "customer_id", "dtype": "int64"}],  order=2),
        ]
        ctx = run(merge_schemas(rows))

        assert isinstance(ctx, MultiFileContext)
        assert len(ctx.files) == 2
        assert len(ctx.join_candidates) >= 1

        top = ctx.join_candidates[0]
        assert top.left_col  == "customer_id"
        assert top.right_col == "customer_id"
        assert top.confidence >= 0.8, f"Expected >= 0.8, got {top.confidence}"

    def test_exact_match_case_insensitive(self):
        """Column names that differ only in case should still match."""
        from services.schema_merger import merge_schemas

        rows = [
            _make_row("a.csv", [{"name": "OrderID", "dtype": "int64"}], order=1),
            _make_row("b.csv", [{"name": "orderid", "dtype": "int64"}], order=2),
        ]
        ctx = run(merge_schemas(rows))
        assert any(
            c.confidence >= 0.7
            for c in ctx.join_candidates
        ), "Expected at least one candidate with conf >= 0.7 for case-insensitive match"


class TestSchemaMergerNoMatch:
    """Completely disjoint schemas should yield empty or low-confidence candidates."""

    def test_no_shared_columns_yields_empty_or_low(self):
        from services.schema_merger import merge_schemas

        rows = [
            _make_row("a.csv", [{"name": "foo", "dtype": "object"}], order=1),
            _make_row("b.csv", [{"name": "bar", "dtype": "object"}], order=2),
        ]
        ctx = run(merge_schemas(rows))

        high_conf = [c for c in ctx.join_candidates if c.confidence >= 0.5]
        assert len(high_conf) == 0, (
            f"Expected no high-confidence candidates for disjoint schemas, got {high_conf}"
        )


class TestSchemaMergerDtypeOverlap:
    """When column names differ but dtypes and sample values overlap, a candidate should appear."""

    def test_sample_value_overlap_detected(self):
        from services.schema_merger import merge_schemas

        shared_ids = ["101", "102", "103", "104", "105"]
        rows = [
            _make_row(
                "orders.csv",
                [{"name": "order_cust", "dtype": "int64", "samples": shared_ids}],
                order=1,
            ),
            _make_row(
                "customers.csv",
                [{"name": "client_num", "dtype": "int64", "samples": shared_ids}],
                order=2,
            ),
        ]
        ctx = run(merge_schemas(rows))

        # There should be at least one candidate — may not be high-confidence
        assert len(ctx.join_candidates) >= 1, "Expected at least one candidate via sample overlap"


class TestSchemaMergerThreeFiles:
    """Three files should produce candidates across all pairs."""

    def test_three_file_all_pairs_checked(self):
        from services.schema_merger import merge_schemas

        rows = [
            _make_row("a.csv", [{"name": "id", "dtype": "int64"}], order=1),
            _make_row("b.csv", [{"name": "id", "dtype": "int64"}], order=2),
            _make_row("c.csv", [{"name": "id", "dtype": "int64"}], order=3),
        ]
        ctx = run(merge_schemas(rows))

        assert len(ctx.files) == 3
        # Pairs: (df1,df2), (df1,df3), (df2,df3) — each 'id'<->'id' should match
        assert len(ctx.join_candidates) >= 3, (
            f"Expected >= 3 candidates for 3-file exact-match scenario, got {ctx.join_candidates}"
        )


class TestSchemaMergerSingleFile:
    """Single-file workspace should return 0 candidates."""

    def test_single_file_returns_empty_candidates(self):
        from services.schema_merger import merge_schemas

        rows = [_make_row("only.csv", [{"name": "col_a", "dtype": "object"}], order=1)]
        ctx = run(merge_schemas(rows))

        assert len(ctx.files) == 1
        assert len(ctx.join_candidates) == 0


class TestSchemaMergerOrdering:
    """Candidates should be sorted by confidence descending."""

    def test_candidates_sorted_by_confidence(self):
        from services.schema_merger import merge_schemas

        rows = [
            _make_row("x.csv", [
                {"name": "key", "dtype": "int64"},
                {"name": "fuzzkey", "dtype": "int64"},
            ], order=1),
            _make_row("y.csv", [
                {"name": "key", "dtype": "int64"},
                {"name": "fuzzy_key", "dtype": "int64"},
            ], order=2),
        ]
        ctx = run(merge_schemas(rows))

        confidences = [c.confidence for c in ctx.join_candidates]
        assert confidences == sorted(confidences, reverse=True), (
            f"Candidates not sorted by confidence: {confidences}"
        )


class TestSchemaMergerVariableNames:
    """Variable names assigned must be df1, df2, ... in upload order."""

    def test_var_names_assigned_sequentially(self):
        from services.schema_merger import merge_schemas

        rows = [
            _make_row("first.csv",  [{"name": "x"}], order=1),
            _make_row("second.csv", [{"name": "x"}], order=2),
            _make_row("third.csv",  [{"name": "x"}], order=3),
        ]
        ctx = run(merge_schemas(rows))

        var_names = [f.var_name for f in ctx.files]
        assert var_names == ["df1", "df2", "df3"], f"Unexpected var_names: {var_names}"


class TestMultiFileContextToPromptStr:
    """to_prompt_str() must produce a non-empty, well-formed prompt block."""

    def test_prompt_str_contains_all_files(self):
        from services.schema_merger import merge_schemas

        rows = [
            _make_row("alpha.csv", [{"name": "id", "dtype": "int64"}], order=1),
            _make_row("beta.csv",  [{"name": "id", "dtype": "int64"}], order=2),
        ]
        ctx = run(merge_schemas(rows))
        prompt = ctx.to_prompt_str()

        assert "alpha.csv" in prompt
        assert "beta.csv"  in prompt
        assert "JOIN CANDIDATES" in prompt

    def test_prompt_str_under_token_budget(self):
        """Rough check: prompt should not exceed 3000 chars for typical inputs."""
        from services.schema_merger import merge_schemas

        cols = [{"name": f"col_{i}", "dtype": "object"} for i in range(40)]
        rows = [
            _make_row("wide1.csv", cols, order=1),
            _make_row("wide2.csv", cols, order=2),
        ]
        ctx = run(merge_schemas(rows))
        prompt = ctx.to_prompt_str()

        assert len(prompt) <= 3000, (
            f"to_prompt_str() too long ({len(prompt)} chars) — likely not truncating columns"
        )


class TestMultiFileContextToReaderHeader:
    """to_reader_header() must produce valid-looking Python for each file type."""

    def _make_ctx(self, files_specs):
        files = []
        for i, (fname, ftype, fpath) in enumerate(files_specs, start=1):
            fs = FileSchema(
                var_name=f"df{i}",
                file_name=fname,
                file_path=fpath,
                file_type=ftype,
                row_count=100,
                columns=[ColumnMeta(name="col", dtype="int64")],
            )
            files.append(fs)
        return MultiFileContext(files=files)

    def test_csv_reader(self):
        ctx = self._make_ctx([("data.csv", "csv", "/ws/data.csv")])
        header = ctx.to_reader_header(workspace_id="ws1")
        assert "pd.read_csv" in header
        assert "df1" in header

    def test_excel_reader(self):
        ctx = self._make_ctx([("data.xlsx", "xlsx", "/ws/data.xlsx")])
        header = ctx.to_reader_header(workspace_id="ws1")
        assert "pd.read_excel" in header

    def test_parquet_reader(self):
        ctx = self._make_ctx([("data.parquet", "parquet", "/ws/data.parquet")])
        header = ctx.to_reader_header(workspace_id="ws1")
        assert "pd.read_parquet" in header

    def test_markdown_fallback(self):
        ctx = self._make_ctx([("notes.md", "md", "/ws/notes.md")])
        header = ctx.to_reader_header(workspace_id="ws1")
        assert "startswith('|')" in header
        assert "df1" in header

    def test_multi_file_header_contains_verification_prints(self):
        ctx = self._make_ctx([
            ("a.csv",  "csv",  "/ws/a.csv"),
            ("b.xlsx", "xlsx", "/ws/b.xlsx"),
        ])
        header = ctx.to_reader_header(workspace_id="ws1")
        assert "df1 shape" in header
        assert "df2 shape" in header
