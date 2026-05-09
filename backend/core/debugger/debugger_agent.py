"""DebuggerAgent — dedicated error-isolation and code-repair agent.

GAP-06 fix applied:
- LLM call now wrapped in ``asyncio.wait_for`` (60 s timeout) so a stalled
  NIM endpoint cannot freeze the debug loop indefinitely.

Intercepts Python tracebacks after a failed execution round, isolates the
failing segment, and rewrites only the broken portion rather than regenerating
the full script from scratch.

Uses the Pro model (NIM_MODEL_PRO) for maximum reasoning fidelity, as
debugging requires precise traceback interpretation and targeted code surgery.

Architecture position:
    Code → Execute → [FAIL] → Debugger → [corrected code] → Execute (retry)
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from core.llm_client import get_structured_llm
from core.token_tracker import TokenTracker, tracker_callback_config

logger = logging.getLogger("uvicorn.info")

# Maximum chars of traceback passed to the debugger
_MAX_TRACEBACK_CHARS = 3_000
# Maximum chars of code passed to the debugger (keep the full script visible)
_MAX_CODE_CHARS = 10_000
# Maximum seconds to wait for the Debugger LLM call before giving up
_DEBUGGER_LLM_TIMEOUT_S = 60


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class DebuggerOutput(BaseModel):
    """Structured output from the Debugger agent."""

    corrected_code: str = Field(
        description=(
            "The complete, corrected Python script. "
            "Must be self-contained and runnable from top to bottom. "
            "No markdown fences, no explanations — raw Python only."
        )
    )
    error_type: str = Field(
        description=(
            "Classification of the error. One of: "
            "SyntaxError | NameError | IndexError | KeyError | "
            "TypeError | ValueError | ImportError | RuntimeError | Other."
        )
    )
    fix_summary: str = Field(
        description=(
            "One-sentence description of the specific fix applied. "
            "Example: 'Replaced deprecated np.int with np.int64 on line 34.'"
        )
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_DEBUGGER_SYSTEM = """\
You are an expert Python debugger embedded in a data science agent pipeline.

Your ONLY job is to receive a failing Python script and its traceback, then
produce a corrected version of the script that will run successfully.

RULES — read carefully:
1. SURGICAL REPAIR ONLY: Fix the specific failing segment. Do NOT rewrite
   sections that are already working. Do NOT restructure the script logic.
2. PRESERVE OUTPUTS: If working code already printed results or saved files,
   keep those lines intact.
3. COMMON FIXES to apply automatically:
   - np.int, np.float, np.bool, np.object → use np.int64 / np.float64 /
     np.bool_ / object (builtin) instead.
   - iloc[row, col] with a boolean Series → use .loc[] instead.
   - Missing import → add it at the top of the script.
   - KeyError on column → use df.get(col) or check df.columns first.
   - Division by zero / NaN result → add a zero-variance guard.
4. After fixing, return the ENTIRE corrected script (not just the diff).
5. Output ONLY raw Python. No markdown fences (``` or `python`).
6. Classify the error_type from the traceback accurately.
7. Write a concise fix_summary (one sentence maximum).
"""

_DEBUGGER_HUMAN = """\
PYTHON TRACEBACK (error to fix):
{traceback}

DATA SCHEMA CONTEXT (column names and types):
{schema_context}

FAILING SCRIPT (fix this):
{code}

ANALYSIS PLAN (context for what the script is trying to do):
{plan_steps}
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class DebuggerAgent:
    """Receives a failing script + traceback and returns corrected code.

    Uses the Pro LLM tier for precise traceback analysis and targeted
    code repair. Employs structured output to enforce schema compliance.

    Attributes:
        _model: NIM model identifier (defaults to NIM_MODEL_PRO).
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
            model: NIM model identifier. Defaults to ``NIM_MODEL_PRO``.
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
        from core.config import NIM_MODEL_PRO  # pylint: disable=import-outside-toplevel
        resolved = self._model or NIM_MODEL_PRO
        structured_llm = get_structured_llm(
            model=resolved,
            schema=DebuggerOutput,
            temperature=self._temperature,
        )
        return (
            ChatPromptTemplate.from_messages([
                ("system", _DEBUGGER_SYSTEM),
                ("human", _DEBUGGER_HUMAN),
            ])
            | structured_llm
        )

    async def debug(
        self,
        traceback: str,
        code: str,
        plan_steps: List[Dict[str, Any]],
        schema_context: str = "",
        token_tracker: Optional[TokenTracker] = None,
    ) -> Dict[str, Any]:
        """Repairs the failing script and returns the corrected version.

        Args:
            traceback: The Python traceback string from the failed execution.
            code: The full source code that produced the traceback.
            plan_steps: Current plan steps, for context.
            schema_context: Data schema summary (column names / types) to help
                the debugger understand data-specific errors.
            token_tracker: Optional run-level tracker.  When provided, token
                usage from this LLM call is recorded automatically via a
                LangChain callback.

        Returns:
            Dict with keys:
                - ``corrected_code`` (str): The repaired, runnable script.
                - ``error_type`` (str): Classified error category.
                - ``fix_summary`` (str): One-line description of the fix.
        """
        formatted_steps = "\n".join(
            f"  Step {s['index'] + 1}: {s['description']}"
            for s in plan_steps
        )

        # Trim large inputs
        trimmed_tb = traceback[-_MAX_TRACEBACK_CHARS:]
        trimmed_code = code[-_MAX_CODE_CHARS:]

        if self._chain is None:
            built = self._build_chain()      # heavy work — no lock held
            async with self._lock:
                if self._chain is None:      # double-checked locking
                    self._chain = built
        chain = self._chain

        # GAP-06 fix: wrap in asyncio.wait_for so a stalled NIM endpoint
        # cannot freeze the debug loop indefinitely.
        result: DebuggerOutput = await asyncio.wait_for(
            chain.ainvoke(
                {
                    "traceback": trimmed_tb or "(no traceback — check stderr)",
                    "schema_context": schema_context or "(no schema context available)",
                    "code": trimmed_code,
                    "plan_steps": formatted_steps,
                },
                config=tracker_callback_config(token_tracker),
            ),
            timeout=_DEBUGGER_LLM_TIMEOUT_S,
        )

        # Strip accidental fences (defensive)
        corrected = result.corrected_code
        if corrected.startswith("```"):
            lines = corrected.split("\n")
            corrected = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            )

        logger.info(
            "[Debugger] error_type=%s | fix=%s | code_delta=%d chars",
            result.error_type,
            result.fix_summary[:80],
            len(corrected) - len(code),
        )

        return {
            "corrected_code": corrected.strip(),
            "error_type": result.error_type,
            "fix_summary": result.fix_summary,
        }
