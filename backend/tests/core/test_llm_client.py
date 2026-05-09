"""Unit tests for llm_client cache isolation between raw/structured modes."""

from core import llm_client


class _FakeChatNVIDIA:
    """Tiny ChatNVIDIA test double that records init args."""

    init_calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeChatNVIDIA.init_calls.append(kwargs)

    def with_structured_output(self, schema, method=None):  # pragma: no cover - trivial stub
        return ("structured", schema, method, self.kwargs.get("model"))


def _reset_state():
    _FakeChatNVIDIA.init_calls.clear()
    llm_client._llm_cache_shared.clear()      # pylint: disable=protected-access
    llm_client._llm_cache_structured.clear()  # pylint: disable=protected-access
    llm_client._llm_cache_raw.clear()         # pylint: disable=protected-access


def test_get_nim_llm_cache_isolated_by_scope(monkeypatch):
    """Same model+temp should create separate clients for raw vs structured scopes."""
    _reset_state()
    monkeypatch.setattr(llm_client, "NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setattr(llm_client, "ChatNVIDIA", _FakeChatNVIDIA)

    a = llm_client.get_nim_llm("meta/llama-3.3-70b-instruct", 0.1, cache_scope="structured")
    b = llm_client.get_nim_llm("meta/llama-3.3-70b-instruct", 0.1, cache_scope="structured")
    c = llm_client.get_nim_llm("meta/llama-3.3-70b-instruct", 0.1, cache_scope="raw")

    assert a is b  # same scope reuses cache
    assert c is not a  # different scope gets a different instance
    assert len(_FakeChatNVIDIA.init_calls) == 2


def test_get_nim_llm_use_cache_false_returns_fresh(monkeypatch):
    """use_cache=False always creates a new client for contamination-safe fallback."""
    _reset_state()
    monkeypatch.setattr(llm_client, "NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setattr(llm_client, "ChatNVIDIA", _FakeChatNVIDIA)

    a = llm_client.get_nim_llm("meta/llama-3.3-70b-instruct", 0.1, cache_scope="raw", use_cache=False)
    b = llm_client.get_nim_llm("meta/llama-3.3-70b-instruct", 0.1, cache_scope="raw", use_cache=False)

    assert a is not b
    assert len(_FakeChatNVIDIA.init_calls) == 2


def test_get_structured_llm_uses_structured_scope(monkeypatch):
    """Structured helper should allocate from the dedicated structured scope."""
    _reset_state()
    monkeypatch.setattr(llm_client, "NVIDIA_API_KEY", "nvapi-test")
    monkeypatch.setattr(llm_client, "ChatNVIDIA", _FakeChatNVIDIA)

    llm_client.get_structured_llm_with_mode(
        model="meta/llama-3.3-70b-instruct",
        schema=dict,
        temperature=0.1,
        force_json_mode=True,
    )
    assert len(_FakeChatNVIDIA.init_calls) == 1
    assert len(llm_client._llm_cache_structured) == 1  # pylint: disable=protected-access
    assert len(llm_client._llm_cache_raw) == 0         # pylint: disable=protected-access

