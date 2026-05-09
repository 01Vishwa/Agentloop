"""VerifierAgent — LLM judge that decides if the plan is sufficient.

Uses NVIDIA NIM with ``.with_structured_output`` for hard schema-compliance.

Gap fixes applied:
- Code trimmed to 8 000 chars (was 4 000 — too aggressive for multi-file scripts).
- Artifact awareness added: visualization tasks produce PNG files, not stdout.
  The verifier now receives artifact filenames and treats them as valid output.
- Fixed false-positive in hard-failure rule: "raise" keyword check now uses
  a more precise pattern that won't fire on comments/strings.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from core.token_tracker import TokenTracker, tracker_callback_config

logger = logging.getLogger("uvicorn.info")

# Maximum chars of generated code passed to verifier (raised from 4000)
_MAX_CODE_CHARS = 8_000
# Maximum chars of execution output passed to verifier
_MAX_EXEC_CHARS = 3_000

# Pattern that matches a real Python exception line (not a comment/string).
# Uses word boundary \b so 'raise' only matches as a keyword, not as part
# of method/variable names like 'to_raise_warning()' or 'fundraise'.
_EXCEPTION_PATTERN = re.compile(
    r"^(Traceback|.*Error:|.*Exception:|\s+raise\b)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class VerifierOutput(BaseModel):
    """Structured sufficiency verdict from the LLM judge."""

    is_sufficient: bool = Field(
        description="True if the output fully answers the user query, False otherwise."
    )
    reason: str = Field(
        description="Concise explanation of why the plan is or is not sufficient."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score between 0.0 (no confidence) and 1.0 (certain).",
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_VERIFIER_SYSTEM = """\
You are a rigorous data science quality judge.
Evaluate whether the current analysis plan and its code execution results
are sufficient to answer the user's original query.

HARD FAILURE RULES (any of these means is_sufficient = False):
1. The execution output contains a Python Traceback or a line starting with
   "<ExceptionType>Error:" — this is a crash and is NEVER sufficient.
2. The execution output contains a bare "nan" or "NaN" for a requested
   calculation WITHOUT a printed explanation of why it is NaN.
3. TASK-TYPE-AWARE empty-output check:
   - For INSIGHT or DATA WRANGLING tasks (plan steps contain verbs like
     'compute', 'print', 'calculate', 'count', 'filter', 'clean', 'merge'):
     The code must have printed something to stdout AND/OR saved artifact files.
     No output of any kind is ALWAYS a failure for these task types.
   - For VISUALIZATION tasks (plan steps contain 'plot', 'chart', 'histogram',
     'scatter', 'bar chart', 'visualize', 'save figure', 'distribution'):
     Producing one or more artifact files (.png/.jpg/.svg/.html) is SUFFICIENT output
     even if stdout is empty or minimal.

ARTIFACT AWARENESS (important for visualization tasks):
- If the plan required a PLOT/CHART and one or more .png / .jpg / .html / .svg artifact files
  were produced, treat this as sufficient output — even if stdout is minimal.
- Plotly charts are saved as .html files (interactive) — these count as valid visualizations.
- Artifact files are listed under "PRODUCED ARTIFACTS" below.

Evaluation criteria (only apply if no hard failures):
1. Does the output actually answer the user's query with concrete values or plots?
2. Did the code execute without errors affecting correctness?
3. Are the results meaningful and based on real data?
4. Is the analysis complete, or are important steps missing?

IMPORTANT: Be strict. A partial or error-prone result does NOT qualify.
Only mark is_sufficient = True if the output clearly and correctly answers the query.
"""

_VERIFIER_HUMAN = """\
USER QUERY:
{query}

DATA DESCRIPTION:
{data_description}

CURRENT ANALYSIS PLAN:
{plan_steps}

GENERATED CODE (last {code_chars} chars shown):
{code}

EXECUTION OUTPUT:
{execution_output}

PRODUCED ARTIFACTS (files written to ./outputs/):
{artifact_list}
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class VerifierAgent:
    """Evaluates whether the current plan and its output satisfy the query.

    Uses ``.with_structured_output`` to enforce schema compliance at the
    function-calling protocol level.
    """

    def __init__(
        self, model: Optional[str] = None, temperature: Optional[float] = None
    ) -> None:
        """Initialises the agent.

        Args:
            model: NIM model identifier; defaults to ``NIM_MODEL_DEFAULT``.
            temperature: Sampling temperature; defaults to 0.1.
        """
        self._model = model
        self._temperature = temperature if temperature is not None else 0.1
        self._chain = None  # lazily built
        self._lock = asyncio.Lock()  # async-safe lazy init

    def _build_chain(self):
        """Builds (but does NOT cache) the LangChain pipeline.

        Heavy work — LLM init, imports — happens here, OUTSIDE the async lock.
        """
        from core.llm_client import get_structured_llm  # pylint: disable=import-outside-toplevel
        structured_llm = get_structured_llm(
            model=self._model,
            schema=VerifierOutput,
            temperature=self._temperature,
        )
        return (
            ChatPromptTemplate.from_messages([
                ("system", _VERIFIER_SYSTEM),
                ("human", _VERIFIER_HUMAN),
            ])
            | structured_llm
        )

    async def verify(
        self,
        query: str,
        data_description: str,
        plan_steps: List[Dict[str, Any]],
        code: str,
        execution_output: str,
        artifact_names: Optional[List[str]] = None,
        token_tracker: Optional[TokenTracker] = None,
    ) -> Dict[str, Any]:
        """Runs the LLM judge and returns a typed verification result dict.

        Args:
            query: Original user query.
            data_description: Output of FileAnalyzerAgent.
            plan_steps: Current plan steps.
            code: The generated Python script.
            execution_output: Combined stdout/stderr from the executor.
            artifact_names: List of filenames written to ./outputs/ (if any).
            token_tracker: Optional run-level tracker.  When provided, token
                usage from this LLM call is recorded automatically via a
                LangChain callback.

        Returns:
            Dict with keys ``is_sufficient`` (bool), ``reason`` (str),
            ``confidence`` (float).
        """
        formatted_steps = "\n".join(
            f"  Step {s['index'] + 1}: {s['description']}"
            for s in plan_steps
        )

        # Trim large inputs
        trimmed_output = execution_output[:_MAX_EXEC_CHARS]
        trimmed_code = code[-_MAX_CODE_CHARS:]   # keep the END of the script (results)
        code_chars = min(len(code), _MAX_CODE_CHARS)

        artifact_list = (
            "\n".join(f"  - {name}" for name in artifact_names)
            if artifact_names
            else "  (none)"
        )

        if self._chain is None:
            built = self._build_chain()      # heavy work — no lock held
            async with self._lock:
                if self._chain is None:      # double-checked locking
                    self._chain = built
        chain = self._chain

        result: VerifierOutput = await chain.ainvoke(
            {
                "query": query,
                "data_description": data_description,
                "plan_steps": formatted_steps,
                "code_chars": code_chars,
                "code": trimmed_code,
                "execution_output": trimmed_output or "(no output)",
                "artifact_list": artifact_list,
            },
            config=tracker_callback_config(token_tracker),
        )

        logger.info(
            "[Verifier] is_sufficient=%s, confidence=%.2f, reason=%s",
            result.is_sufficient,
            result.confidence,
            result.reason[:80],
        )

        return result.model_dump()
