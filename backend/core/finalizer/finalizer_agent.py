"""FinalizerAgent — post-verification output formatter.

Takes the raw execution output (stdout from the last successful run) and
transforms it into a clean, structured, human-readable response.

Uses the Flash model (NIM_MODEL_FLASH) since formatting is a fast,
deterministic task that does not require deep reasoning.

Architecture position:
    Verifier [is_sufficient=True] → Finalizer → final_output
"""

import asyncio
import ast
import logging
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from core.llm_client import get_structured_llm
from core.token_tracker import TokenTracker, tracker_callback_config

logger = logging.getLogger("uvicorn.info")

_MAX_OUTPUT_CHARS = 6_000


def _is_column_listing_query(query: str) -> bool:
    """Returns True when query intent is to list/show column names."""
    q = (query or "").strip().lower()
    if not q:
        return False
    has_column_word = any(w in q for w in ("column", "columns", "header", "headers", "field", "fields"))
    has_listing_intent = any(w in q for w in ("list", "show", "print", "display", "name", "names", "what are"))
    return has_column_word and has_listing_intent


def _extract_column_map_from_output(execution_output: str) -> Dict[str, List[str]]:
    """Parses ``<file>: [..columns..]`` lines emitted by execution scripts."""
    out = execution_output or ""
    col_map: Dict[str, List[str]] = {}
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line or "[" not in line or "]" not in line:
            continue
        left, right = line.split(":", 1)
        file_name = left.strip() or "dataset"
        list_start = right.find("[")
        list_end = right.rfind("]")
        if list_start < 0 or list_end <= list_start:
            continue
        bracket = right[list_start:list_end + 1]
        try:
            parsed = ast.literal_eval(bracket)
        except Exception:  # pylint: disable=broad-except
            continue
        if isinstance(parsed, list):
            cols = [str(c) for c in parsed if str(c).strip()]
            if cols:
                col_map[file_name] = cols
    return col_map


def _build_column_listing_final_output(col_map: Dict[str, List[str]]) -> Dict[str, Any]:
    """Builds deterministic final output for column-listing style queries."""
    if not col_map:
        return {
            "headline": "Columns could not be parsed from execution output.",
            "formatted_output": (
                "The run completed, but the column list was not found in stdout. "
                "Please rerun with explicit `print(df.columns.tolist())` output."
            ),
            "confidence": 0.4,
        }

    total_cols = sum(len(cols) for cols in col_map.values())
    file_count = len(col_map)
    headline = f"Detected {total_cols} column(s) across {file_count} file(s)."
    sections: list[str] = [
        f"Found **{total_cols}** column(s) across **{file_count}** uploaded file(s).",
        "",
    ]
    for fname, cols in col_map.items():
        sections.append(f"### `{fname}`")
        sections.extend(f"- `{c}`" for c in cols)
        sections.append("")
    return {
        "headline": headline,
        "formatted_output": "\n".join(sections).strip(),
        "confidence": 1.0,
    }


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class FinalizerOutput(BaseModel):
    """Structured final response from the Finalizer agent."""

    headline: str = Field(
        description=(
            "A single, clear sentence directly answering the user's query. "
            "Must be concrete — include actual numbers or results if available."
        )
    )
    formatted_output: str = Field(
        description=(
            "The full formatted response in markdown. Include: "
            "a short executive summary, then key findings as a bullet list, "
            "then any important caveats. No fabricated data."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confidence that the formatted output answers the query completely. "
            "1.0 = fully answered. 0.0 = output was incomplete or ambiguous."
        ),
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_FINALIZER_SYSTEM = """\
You are a technical writing specialist working at the final stage of a
data science agent pipeline. Your job is to take raw execution output and
transform it into a clean, structured, professional response.

STRICT RULES:
1. NO HALLUCINATION: Only use information present in the execution output.
   Do NOT invent numbers, trends, or conclusions not supported by the data.
2. DIRECT ANSWER FIRST: The headline must directly answer the user's query.
   If the answer is a number, include the number. Do not be vague.
3. STRUCTURED FORMAT: Use markdown with bullet points for findings.
   Keep the executive summary ≤ 3 sentences.
4. CAVEATS: If output contains NaN, zero-variance warnings, or incomplete
   data, include a clear caveat section at the end.
5. ARTIFACTS: If chart file names appear in the output (e.g. chart.png),
   mention them naturally (e.g. "See the attached bar chart").
6. DO NOT restate the question or the plan steps in the output.
"""

_FINALIZER_HUMAN = """\
USER QUERY:
{query}

RAW EXECUTION OUTPUT:
{execution_output}

ARTIFACT FILES PRODUCED:
{artifact_list}

ANALYSIS PLAN (for context — do not repeat verbatim):
{plan_steps}
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FinalizerAgent:
    """Formats verified execution output into a structured final response.

    Enforces no-hallucination constraints and deterministic formatting rules.
    Uses the Flash LLM tier for speed (formatting is not reasoning-heavy).

    Attributes:
        _model: NIM model identifier (defaults to NIM_MODEL_FLASH).
        _temperature: LLM sampling temperature.
        _chain: Lazily initialised LangChain pipeline.
        _lock: Thread-safety guard for lazy chain initialisation.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> None:
        """Initialises the agent.

        Args:
            model: NIM model identifier. Defaults to ``NIM_MODEL_FLASH``.
            temperature: Sampling temperature. Defaults to 0.1.
        """
        self._model = model
        self._temperature = temperature if temperature is not None else 0.1
        self._chain = None
        self._lock = asyncio.Lock()  # async-safe lazy init

    def _build_chain(self):
        """Builds (but does NOT cache) the LangChain pipeline.

        Heavy work — LLM init, imports — happens here, OUTSIDE the async lock.
        """
        from core.config import NIM_MODEL_FLASH  # pylint: disable=import-outside-toplevel
        resolved = self._model or NIM_MODEL_FLASH
        structured_llm = get_structured_llm(
            model=resolved,
            schema=FinalizerOutput,
            temperature=self._temperature,
        )
        return (
            ChatPromptTemplate.from_messages([
                ("system", _FINALIZER_SYSTEM),
                ("human", _FINALIZER_HUMAN),
            ])
            | structured_llm
        )

    async def finalize(
        self,
        query: str,
        execution_output: str,
        plan_steps: List[Dict[str, Any]],
        artifact_names: Optional[List[str]] = None,
        token_tracker: Optional[TokenTracker] = None,
    ) -> Dict[str, Any]:
        """Formats raw execution output into a structured final response.

        Args:
            query: The original user query.
            execution_output: Combined stdout from the last successful run.
            plan_steps: Final plan steps from the orchestrator.
            artifact_names: List of artifact filenames (charts, CSVs, etc.).
            token_tracker: Optional run-level tracker.  When provided, token
                usage from this LLM call is recorded automatically via a
                LangChain callback.

        Returns:
            Dict with keys:
                - ``headline`` (str): Direct one-sentence answer.
                - ``formatted_output`` (str): Full markdown-formatted response.
                - ``confidence`` (float): Answer completeness confidence score.
        """
        formatted_steps = "\n".join(
            f"  Step {s['index'] + 1}: {s['description']}"
            for s in plan_steps
        )
        artifact_list = (
            "\n".join(f"  - {name}" for name in artifact_names)
            if artifact_names
            else "  (none)"
        )
        trimmed_output = execution_output[:_MAX_OUTPUT_CHARS]

        if _is_column_listing_query(query):
            col_map = _extract_column_map_from_output(trimmed_output)
            deterministic = _build_column_listing_final_output(col_map)
            logger.info(
                "[Finalizer] Deterministic column-list formatting applied | files=%d",
                len(col_map),
            )
            return deterministic

        if self._chain is None:
            built = self._build_chain()      # heavy work — no lock held
            async with self._lock:
                if self._chain is None:      # double-checked locking
                    self._chain = built
        chain = self._chain

        result: FinalizerOutput = await chain.ainvoke(
            {
                "query": query,
                "execution_output": trimmed_output or "(no output produced)",
                # Backward-compatible key used by older tests/clients.
                # Keep alongside artifact_list (the actual prompt placeholder).
                "artifacts": ", ".join(artifact_names or []),
                "artifact_list": artifact_list,
                "plan_steps": formatted_steps,
            },
            config=tracker_callback_config(token_tracker),
        )

        logger.info(
            "[Finalizer] confidence=%.2f | headline=%s",
            result.confidence,
            result.headline[:80],
        )

        return result.model_dump()
