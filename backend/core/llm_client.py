"""NVIDIA NIM LLM factory — singleton ChatNVIDIA instances per model.

All DS-STAR agents import ``get_nim_llm()`` from this module.

Gap fix applied:
- Gemma-4 and other non-function-calling models now fall back to a JSON-output
  parser instead of OpenAI function-calling structured output, preventing silent
  runtime failures when those models are selected.
- Cache key now includes temperature to avoid cross-temperature contamination.
"""

import logging
from typing import Optional

from langchain_nvidia_ai_endpoints import ChatNVIDIA

from core.config import (
    NVIDIA_API_KEY,
    NIM_MODEL_DEFAULT,
    NIM_MODEL_CODER,
    NIM_MODEL_PRO,
    NIM_MODEL_FLASH,
)

logger = logging.getLogger("uvicorn.info")

# ---------------------------------------------------------------------------
# Models that do NOT support OpenAI function-calling structured output
# ---------------------------------------------------------------------------

# These models require JSON-mode output parsing instead.
_NON_FUNCTION_CALLING_MODELS = frozenset({
    "google/gemma-4-31b-it",
    "google/gemma-3-27b-it",
    "mistralai/mixtral-8x7b-instruct-v0.1",
    "nvidia/nemotron-3-super-120b-a12b",
    # CodeLlama does not support OpenAI function-calling schema;
    # use JSON-mode structured output instead.
    "meta/codellama-70b-instruct",
})

# ---------------------------------------------------------------------------
# Module-level singleton caches: cache_key → ChatNVIDIA instance
#
# We intentionally isolate caches by "scope":
# - shared: default general-purpose cache
# - structured: used by with_structured_output bindings
# - raw: used by raw-completion fallback paths
#
# This prevents a structured-output path from accidentally reusing the same
# underlying client instance as a raw path for the same model+temperature.
# ---------------------------------------------------------------------------

_llm_cache_shared: dict[str, ChatNVIDIA] = {}
_llm_cache_structured: dict[str, ChatNVIDIA] = {}
_llm_cache_raw: dict[str, ChatNVIDIA] = {}


def get_nim_llm(
    model: Optional[str] = None,
    temperature: float = 0.1,
    *,
    cache_scope: str = "shared",
    use_cache: bool = True,
) -> ChatNVIDIA:
    """Returns a cached ChatNVIDIA instance for the requested model.

    Args:
        model: NIM model identifier. Defaults to ``NIM_MODEL_DEFAULT``.
        temperature: Sampling temperature (lower = more deterministic).
        cache_scope: Cache partition. One of ``shared``, ``structured``, ``raw``.
            Use distinct scopes when callers must avoid cross-path reuse.
        use_cache: When False, always create a fresh ChatNVIDIA instance.

    Returns:
        ChatNVIDIA: Authenticated, ready-to-use LLM instance.

    Raises:
        RuntimeError: If ``NVIDIA_API_KEY`` is not configured.
    """
    if not NVIDIA_API_KEY or NVIDIA_API_KEY == "your_nvidia_api_key_here":
        raise RuntimeError(
            "NVIDIA_API_KEY is not configured in backend/.env. "
            "Obtain a key at https://build.nvidia.com and set it there."
        )

    resolved_model = model or NIM_MODEL_DEFAULT
    cache_key = f"{resolved_model}:{temperature}"

    cache_map = {
        "shared": _llm_cache_shared,
        "structured": _llm_cache_structured,
        "raw": _llm_cache_raw,
    }.get(cache_scope)
    if cache_map is None:
        raise ValueError(
            f"Unknown cache_scope '{cache_scope}'. Expected one of: shared, structured, raw."
        )

    if use_cache and cache_key in cache_map:
        return cache_map[cache_key]

    kwargs: dict = {}

    # Gemma-4 thinking mode
    if resolved_model == "google/gemma-4-31b-it":
        kwargs["model_kwargs"] = {
            "chat_template_kwargs": {"enable_thinking": True}
        }

    # Nemotron-3 Super thinking mode
    elif resolved_model == "nvidia/nemotron-3-super-120b-a12b":
        kwargs["chat_template_kwargs"] = {"enable_thinking": True}
        kwargs["max_tokens"] = 16384
        kwargs["top_p"] = 0.95
        kwargs["reasoning_budget"] = 16384

    llm = ChatNVIDIA(
        model=resolved_model,
        api_key=NVIDIA_API_KEY,
        temperature=temperature,
        **kwargs,
    )
    if use_cache:
        cache_map[cache_key] = llm
    logger.info(
        "[NIM] ChatNVIDIA initialised — model=%s, temp=%.2f, scope=%s, cached=%s",
        resolved_model,
        temperature,
        cache_scope,
        use_cache,
    )
    return llm


def supports_function_calling(model: Optional[str] = None) -> bool:
    """Returns True if the model supports OpenAI function-calling format.

    Args:
        model: NIM model identifier.

    Returns:
        bool: False for models that require JSON-mode fallback.
    """
    resolved = model or NIM_MODEL_DEFAULT
    return resolved not in _NON_FUNCTION_CALLING_MODELS


def get_structured_llm(model: Optional[str], schema, temperature: float = 0.1):
    """Returns an LLM bound to a structured output schema.

    For function-calling capable models, uses ``with_structured_output``.
    For others (e.g. Gemma-4), uses ``with_structured_output`` with
    ``method="json_mode"`` as a fallback.

    Args:
        model: NIM model identifier.
        schema: Pydantic BaseModel class defining the output schema.
        temperature: Sampling temperature.

    Returns:
        Runnable LLM chain that outputs the schema type.
    """
    return get_structured_llm_with_mode(
        model=model,
        schema=schema,
        temperature=temperature,
        force_json_mode=False,
    )


def get_structured_llm_with_mode(
    model: Optional[str],
    schema,
    temperature: float = 0.1,
    *,
    force_json_mode: bool = False,
):
    """Returns an LLM bound to a schema with explicit structured-output mode.

    JSON-mode avoids tool/function calling and is more robust for agents like the
    Coder that must always return a fixed JSON object (e.g. {"code": "..."}).
    """
    llm = get_nim_llm(model=model, temperature=temperature, cache_scope="structured")

    if force_json_mode:
        logger.info("[NIM] Forced JSON-mode structured output for model=%s", model)
        return llm.with_structured_output(schema, method="json_mode")

    if supports_function_calling(model):
        return llm.with_structured_output(schema)

    logger.info("[NIM] Using JSON-mode structured output for model=%s", model)
    return llm.with_structured_output(schema, method="json_mode")


def get_default_llm() -> ChatNVIDIA:
    """Convenience accessor for the default reasoning model.

    Returns:
        ChatNVIDIA: Default NIM LLM.
    """
    return get_nim_llm(model=NIM_MODEL_DEFAULT)


def get_coder_llm() -> ChatNVIDIA:
    """Convenience accessor for the code-generation model.

    Returns:
        ChatNVIDIA: Coder NIM LLM.
    """
    return get_nim_llm(model=NIM_MODEL_CODER)


def get_pro_llm(temperature: float = 0.1) -> ChatNVIDIA:
    """Returns the Pro (reasoning-heavy) model instance.

    Used by Planner, Coder, Verifier, and Debugger agents that require
    strong logical reasoning and high instruction-following fidelity.

    Args:
        temperature: Sampling temperature; defaults to 0.1 for determinism.

    Returns:
        ChatNVIDIA: Pro NIM LLM.
    """
    return get_nim_llm(model=NIM_MODEL_PRO, temperature=temperature)


def get_flash_llm(temperature: float = 0.1) -> ChatNVIDIA:
    """Returns the Flash (high-throughput) model instance.

    Used by SubQuestionGenerator, ReportWriter, and Analyzer summary agents
    that prioritise speed and cost efficiency over deep reasoning.

    Args:
        temperature: Sampling temperature; defaults to 0.1.

    Returns:
        ChatNVIDIA: Flash NIM LLM.
    """
    return get_nim_llm(model=NIM_MODEL_FLASH, temperature=temperature)


def get_pro_structured_llm(schema, temperature: float = 0.1):
    """Returns a Pro LLM bound to a Pydantic structured-output schema.

    Args:
        schema: Pydantic BaseModel class defining the output schema.
        temperature: Sampling temperature.

    Returns:
        Runnable LLM chain that outputs the schema type.
    """
    return get_structured_llm(model=NIM_MODEL_PRO, schema=schema, temperature=temperature)


def get_flash_structured_llm(schema, temperature: float = 0.1):
    """Returns a Flash LLM bound to a Pydantic structured-output schema.

    Args:
        schema: Pydantic BaseModel class defining the output schema.
        temperature: Sampling temperature.

    Returns:
        Runnable LLM chain that outputs the schema type.
    """
    return get_structured_llm(model=NIM_MODEL_FLASH, schema=schema, temperature=temperature)
