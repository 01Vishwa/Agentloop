"""Unit tests for the CoderAgent's schema-recovery helpers.

Covers the column-name JSON-key hallucination path that previously caused
`CoderAgent schema mismatch ... Stopping early to avoid retry loop.` aborts.
"""

import os

import pytest

from core.coder.coder_agent import (
    CoderAgent,
    _build_column_listing_fallback_script,
    _extract_code_from_model_text,
    _is_column_listing_query,
    _parse_columns_from_schema_hints,
    _should_use_raw_completion,
)


# ---------------------------------------------------------------------------
# _extract_code_from_model_text — schema-aware recovery
# ---------------------------------------------------------------------------

def test_extractor_recovers_code_from_column_name_key():
    """JSON wrapper with a known data-column name as the key is recovered."""
    text = '{"age": "import pandas as pd\\nprint(\'hello\')"}'
    out = _extract_code_from_model_text(text, known_columns=["Age", "Gender"])
    assert out == "import pandas as pd\nprint('hello')"


def test_extractor_recovers_via_python_signal_tokens():
    """Multi-field JSON: pick the field that looks like real Python."""
    text = (
        '{"explanation": "We compute the distribution.", '
        '"snippet": "import pandas as pd\\ndf.describe()"}'
    )
    out = _extract_code_from_model_text(text, known_columns=[])
    assert "import pandas as pd" in out
    assert "df.describe()" in out


def test_extractor_canonical_code_key_still_wins():
    """When the canonical 'code' key is present we never fall back."""
    text = '{"code": "print(1)", "extra": "import pandas as pd"}'
    out = _extract_code_from_model_text(text, known_columns=["age"])
    assert out == "print(1)"


def test_extractor_returns_empty_for_garbage():
    """No JSON, no fences, just whitespace -> empty string."""
    assert _extract_code_from_model_text("", known_columns=[]) == ""
    assert _extract_code_from_model_text("   ", known_columns=[]) == ""


def test_extractor_strips_markdown_fences():
    """Markdown code fences are stripped before JSON parsing."""
    text = "```json\n{\"code\": \"print(1)\"}\n```"
    out = _extract_code_from_model_text(text)
    assert out == "print(1)"


def test_extractor_handles_alias_keys():
    """Common aliases (script/python/answer) are accepted."""
    assert (
        _extract_code_from_model_text('{"script": "import x"}') == "import x"
    )
    assert (
        _extract_code_from_model_text('{"answer": "import y"}') == "import y"
    )


def test_extractor_case_insensitive_column_match():
    """Column-name match must be case-insensitive."""
    text = '{"AGE": "import pandas as pd\\nprint(df)"}'
    out = _extract_code_from_model_text(text, known_columns=["Age"])
    assert "import pandas as pd" in out


# ---------------------------------------------------------------------------
# _parse_columns_from_schema_hints
# ---------------------------------------------------------------------------

def test_parse_columns_from_orchestrator_hint_format():
    """Parses the exact format the orchestrator emits."""
    hints = (
        "healthcare_dataset.csv: ['Name', 'Age', 'Gender', 'Blood Type']\n"
        "extra.csv: ['Foo', 'Bar']"
    )
    cols = _parse_columns_from_schema_hints(hints)
    assert "Name" in cols
    assert "Age" in cols
    assert "Gender" in cols
    assert "Blood Type" in cols
    assert "Foo" in cols
    assert "Bar" in cols


def test_parse_columns_returns_empty_for_unknown_marker():
    """Sentinel values from the orchestrator yield no columns."""
    assert _parse_columns_from_schema_hints("(unknown)") == []
    assert _parse_columns_from_schema_hints("(none provided)") == []
    assert _parse_columns_from_schema_hints("") == []


def test_is_column_listing_query_intent_detection():
    """Column-list requests are detected; unrelated queries are ignored."""
    assert _is_column_listing_query("list down all columns here in this file") is True
    assert _is_column_listing_query("show column names") is True
    assert _is_column_listing_query("what is the average billing amount?") is False


def test_build_column_listing_fallback_script_includes_hint_columns():
    """Fallback script embeds schema-hint columns for no-file scenarios."""
    script = _build_column_listing_fallback_script(["Name", "Age"])
    assert "Columns (from schema hints):" in script
    assert "['Name', 'Age']" in script


# ---------------------------------------------------------------------------
# _should_use_raw_completion — wildcard pattern matcher
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model",
    [
        "meta/codellama-70b-instruct",
        "meta/codellama-34b-instruct",
        "meta/llama-3.1-70b-instruct",
        "meta/llama-3.3-70b-instruct",
        "meta/llama-3.2-3b-instruct",
        "meta/llama-4-1t-instruct",
        "META/LLAMA-3.1-70B-INSTRUCT",  # case-insensitive
    ],
)
def test_should_use_raw_completion_positive(model: str):
    assert _should_use_raw_completion(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "meta/llama-3.1-70b-base",          # not -instruct
        "openai/gpt-4o",
        "google/gemma-3-27b-it",
        "nvidia/nemotron-3-super-120b-a12b",
        None,
        "",
    ],
)
def test_should_use_raw_completion_negative(model):
    assert _should_use_raw_completion(model) is False


def test_should_use_raw_completion_env_override(monkeypatch: pytest.MonkeyPatch):
    """Models in CODER_RAW_COMPLETION_MODELS bypass structured output."""
    monkeypatch.setenv(
        "CODER_RAW_COMPLETION_MODELS",
        "openai/gpt-4o, google/gemma-3-27b-it",
    )
    assert _should_use_raw_completion("openai/gpt-4o") is True
    assert _should_use_raw_completion("google/gemma-3-27b-it") is True
    assert _should_use_raw_completion("openai/gpt-3.5-turbo") is False


# ---------------------------------------------------------------------------
# CoderAgent.force_raw_completion — sticky mode flip
# ---------------------------------------------------------------------------

def test_force_raw_completion_flips_state_and_invalidates_chain():
    """force_raw_completion sets the sticky flag and clears the cached chain."""
    agent = CoderAgent(model="meta/some-fc-capable-model")
    agent._chain = "sentinel-chain"  # pretend chain was built
    assert agent._force_raw is False
    assert agent._mode_locked is False

    agent.force_raw_completion(reason="unit test")

    assert agent._force_raw is True
    assert agent._mode_locked is True
    assert agent._chain is None  # invalidated so next call rebuilds


def test_force_raw_completion_idempotent():
    """Repeated calls do not re-trigger logging or chain rebuilds."""
    agent = CoderAgent(model="meta/some-fc-capable-model")
    agent.force_raw_completion(reason="first")
    agent._chain = "rebuilt-chain"  # simulate next generate_code rebuilding

    agent.force_raw_completion(reason="second")  # should be a no-op

    assert agent._force_raw is True
    assert agent._chain == "rebuilt-chain"  # NOT invalidated again


def test_force_raw_completion_engages_raw_path():
    """After flipping, _is_raw_path() returns True even for FC-capable models."""
    agent = CoderAgent(model="openai/gpt-4o")
    assert agent._is_raw_path() is False

    agent.force_raw_completion(reason="schema mismatch")
    assert agent._is_raw_path() is True
