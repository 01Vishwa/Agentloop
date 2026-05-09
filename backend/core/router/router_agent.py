"""RouterAgent — decides how to refine an insufficient plan.

Uses NVIDIA NIM with ``.with_structured_output`` for hard schema-compliance.

Gap fixes applied:
- Added ``REMOVE_STEPS`` action to match the paper's diagram (Router can prune
  wrong steps, not just append/fix them).
- Removed ``data_description`` from the router prompt (unnecessary tokens).
- Added ``execution_output`` parameter so the router sees actual error tracebacks.
"""

import asyncio
import logging
from typing import Any, Dict, List, Literal, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from core.token_tracker import TokenTracker, tracker_callback_config

logger = logging.getLogger("uvicorn.info")


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class StepDetail(BaseModel):
    """A single plan step payload (index/status added by the orchestrator)."""

    description: str = Field(
        description="Clear, specific description of what this step should do."
    )


class RouterOutput(BaseModel):
    """Routing decision from the LLM plan optimizer."""

    action: Literal["ADD_STEP", "FIX_STEP", "REMOVE_STEPS"] = Field(
        description=(
            "ADD_STEP to append a new step at the end. "
            "FIX_STEP to replace an existing broken step. "
            "REMOVE_STEPS to prune all steps from ``remove_from_index`` onward "
            "when the plan took a fundamentally wrong direction."
        )
    )
    step_index: Optional[int] = Field(
        default=None,
        description=(
            "Zero-based index of the step to fix (required for FIX_STEP, "
            "null otherwise)."
        ),
    )
    remove_from_index: Optional[int] = Field(
        default=None,
        description=(
            "Zero-based index from which to remove steps (inclusive). "
            "Required for REMOVE_STEPS."
        ),
    )
    new_step: StepDetail = Field(
        description=(
            "The new or replacement step content. "
            "For REMOVE_STEPS, this is the corrected first step to put in place "
            "of the removed ones."
        )
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_ROUTER_SYSTEM = """\
You are a data science plan optimizer.
The current analysis plan was judged as INSUFFICIENT by a quality judge.
Your job is to decide the single best repair action.

Choose ONE action:

1. "ADD_STEP": Add a new step at the END of the plan.
   Use when: the plan is correct so far but incomplete.

2. "FIX_STEP": Replace a specific existing step that is wrong/incomplete.
   Use when: one step is identified as the root cause of failure.
   Set step_index to the zero-based index of the broken step.

3. "REMOVE_STEPS": Prune all steps from ``remove_from_index`` onward,
   replacing them with a single corrected step.
   Use when: the plan took a fundamentally wrong direction and multiple
   steps need to be discarded.

The verifier feedback and execution errors will guide your decision.
If there is a Python traceback in the execution output, prefer FIX_STEP or REMOVE_STEPS.
If execution succeeded but output is incomplete, prefer ADD_STEP.
"""

_ROUTER_HUMAN = """\
USER QUERY:
{query}

CURRENT PLAN STEPS:
{plan_steps}

VERIFIER FEEDBACK:
{verifier_reason}

LAST EXECUTION OUTPUT (errors/tracebacks):
{execution_output}
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class RouterAgent:
    """Decides and produces the plan mutation needed after a failed verification.

    Supports three actions: ADD_STEP, FIX_STEP, REMOVE_STEPS.
    Uses ``.with_structured_output`` for schema compliance.
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
            schema=RouterOutput,
            temperature=self._temperature,
        )
        return (
            ChatPromptTemplate.from_messages([
                ("system", _ROUTER_SYSTEM),
                ("human", _ROUTER_HUMAN),
            ])
            | structured_llm
        )

    async def route(
        self,
        query: str,
        plan_steps: List[Dict[str, Any]],
        verifier_reason: str,
        data_description: str = "",
        execution_output: str = "",
        token_tracker: Optional[TokenTracker] = None,
    ) -> Dict[str, Any]:
        """Generates a routing decision to refine the plan.

        Args:
            query: Original user query.
            plan_steps: Current plan steps.
            verifier_reason: Explanation from VerifierAgent.
            execution_output: Combined stdout/stderr from last execution.
            token_tracker: Optional run-level tracker.  When provided, token
                usage from this LLM call is recorded automatically via a
                LangChain callback.

        Returns:
            Dict with keys ``action``, ``step_index``, ``remove_from_index``,
            ``new_step``.
        """
        # data_description is accepted for backward compatibility (tests / older callers),
        # but intentionally not used in the prompt to conserve tokens.
        _ = data_description

        formatted_steps = "\n".join(
            f"  Step {s['index'] + 1}: {s['description']}"
            for s in plan_steps
        )

        # Pass enough execution output for the router to see late-appearing
        # tracebacks (raised from 1500 → 3000 chars).
        exec_excerpt = execution_output[:3000] if execution_output else "(none)"
        if self._chain is None:
            built = self._build_chain()      # heavy work — no lock held
            async with self._lock:
                if self._chain is None:      # double-checked locking
                    self._chain = built
        chain = self._chain
        result: RouterOutput = await chain.ainvoke(
            {
                "query": query,
                "plan_steps": formatted_steps,
                "verifier_reason": verifier_reason,
                "execution_output": exec_excerpt,
            },
            config=tracker_callback_config(token_tracker),
        )

        logger.info(
            "[Router] Decision: action=%s, step_index=%s, remove_from=%s, new_step=%s",
            result.action,
            result.step_index,
            result.remove_from_index,
            result.new_step.description[:60],
        )

        # GAP 7 fix: guard against REMOVE_STEPS without a valid remove_from_index.
        # If the LLM chose REMOVE_STEPS but forgot to set the index, coerce to
        # FIX_STEP at step 0 so the planner still makes a meaningful repair
        # rather than silently falling through to ADD_STEP.
        action = result.action
        step_index = result.step_index
        remove_from_index = result.remove_from_index

        if action == "REMOVE_STEPS" and remove_from_index is None:
            logger.warning(
                "[Router] REMOVE_STEPS action missing remove_from_index — "
                "coercing to FIX_STEP at step 0."
            )
            action = "FIX_STEP"
            step_index = step_index if step_index is not None else 0

        return {
            "action": action,
            "step_index": step_index,
            "remove_from_index": remove_from_index,
            "new_step": result.new_step.model_dump(),
        }
