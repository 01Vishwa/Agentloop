"""SubQuestionGeneratorAgent — decomposes open-ended queries for DS-STAR+.

Receives the user's open-ended research query together with file summaries
and produces a list of atomic, independently-solvable sub-questions that
together provide full coverage of the original intent.

Uses the Flash model (NIM_MODEL_FLASH) since question generation is fast
and does not require deep reasoning.

Architecture position:
    [DS-STAR+ mode]
    User Query → SubQuestionGenerator → List[sub-questions]
                                       → [parallel DS-STAR runs]
                                       → ReportWriter
"""

import asyncio
import logging
from typing import List, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

logger = logging.getLogger("uvicorn.info")

_MAX_SUMMARY_CHARS = 4_000
_MAX_DRAFT_CHARS = 6_000


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class SubQuestionsOutput(BaseModel):
    """Structured list of atomic sub-questions."""

    sub_questions: List[str] = Field(
        description=(
            "List of atomic, independently-solvable questions. "
            "Each question must be answerable from the provided data files "
            "without reference to other sub-questions. "
            "Minimum 2, maximum 8 questions."
        ),
        min_length=2,
        max_length=8,
    )
    coverage_summary: str = Field(
        description=(
            "One sentence explaining how these sub-questions together "
            "completely answer the original query."
        )
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SUBQ_SYSTEM = """\
You are a research decomposition specialist working inside a data science
agent system. Your task is to break down an open-ended research query into
a set of ATOMIC sub-questions.

RULES:
1. ATOMIC: Each sub-question must be answerable independently from the data,
   without needing the answer to another sub-question first.
2. NO OVERLAP: Avoid duplicate or near-duplicate questions. Each question
   should target a distinct aspect of the data or query.
3. MAXIMUM COVERAGE: Together, the questions must cover ALL important aspects
   of the original query. Do not leave major dimensions unaddressed.
4. DATA-GROUNDED: Each question must reference something that can actually be
   computed or extracted from the described data files.
5. CONCRETE: Write specific, measurable questions (e.g. "What is the average
   sales revenue by region?" NOT "Analyse the sales data").
6. COUNT: Generate between 2 and 8 sub-questions depending on query complexity.
   Simple queries → 2–3. Complex multi-dimensional queries → 5–8.
"""

_SUBQ_HUMAN = """\
ORIGINAL RESEARCH QUERY:
{query}

AVAILABLE DATA FILES SUMMARY:
{data_summary}
"""

# ---------------------------------------------------------------------------
# Refinement prompt (DS-STAR+ iterative refinement — GAP 1 fix)
# Used when the generator is re-engaged with the initial draft to find gaps.
# ---------------------------------------------------------------------------

_SUBQ_REFINEMENT_SYSTEM = """\
You are a research gap analyst working inside a data science agent system.
An initial research report has been drafted from a set of sub-question analyses.
Your task is to identify what is MISSING from the report and generate a NEW
set of supplementary sub-questions that will fill those gaps.

RULES:
1. READ the draft report carefully to understand what has already been covered.
2. IDENTIFY at least 1 and up to 5 gaps — important aspects of the original query
   that are not addressed or are only superficially mentioned in the draft.
3. Generate ONLY supplementary questions — do NOT repeat questions already answered.
4. Each question must be ATOMIC, independently answerable from the data.
5. Each question must target NEW information not included in the current draft.
6. DATA-GROUNDED: each question must be answerable from the available data files.
"""

_SUBQ_REFINEMENT_HUMAN = """\
ORIGINAL RESEARCH QUERY:
{query}

AVAILABLE DATA FILES SUMMARY:
{data_summary}

CURRENT DRAFT REPORT (identify what is missing from this):
{draft_report}
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class SubQuestionGeneratorAgent:
    """Decomposes an open-ended query into atomic, independently-solvable sub-questions.

    Each sub-question is designed to be processed by a separate DS-STAR
    execution loop, with results aggregated by the ReportWriterAgent.

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
            temperature: Sampling temperature. Defaults to 0.2 (slightly
                higher than other agents to encourage question diversity).
        """
        self._model = model
        self._temperature = temperature if temperature is not None else 0.2
        self._chain = None
        self._lock = asyncio.Lock()  # async-safe lazy init

    def _build_chain(self):
        """Builds (but does NOT cache) the LangChain pipeline.

        Heavy work — LLM init, imports — happens here, OUTSIDE the async lock.
        """
        from core.llm_client import get_structured_llm  # pylint: disable=import-outside-toplevel
        from core.config import NIM_MODEL_FLASH  # pylint: disable=import-outside-toplevel
        resolved = self._model or NIM_MODEL_FLASH
        structured_llm = get_structured_llm(
            model=resolved,
            schema=SubQuestionsOutput,
            temperature=self._temperature,
        )
        return (
            ChatPromptTemplate.from_messages([
                ("system", _SUBQ_SYSTEM),
                ("human", _SUBQ_HUMAN),
            ])
            | structured_llm
        )

    async def generate(
        self,
        query: str,
        data_summary: str,
        draft_report: Optional[str] = None,
    ) -> List[str]:
        """Generates atomic sub-questions from an open-ended query.

        When ``draft_report`` is provided (DS-STAR+ iterative refinement phase),
        switches to a gap-identification prompt that generates SUPPLEMENTARY
        questions targeting information missing from the current draft.
        This implements the paper's second-phase sub-question generation:
        the generator reads the draft, identifies gaps, and produces new
        questions to deepen the report's informational coverage.

        Args:
            query: The user's open-ended research query.
            data_summary: Combined text summary of available data files
                (output of FileAnalyzerAgent.analyze()).
            draft_report: Optional. The initial report draft from
                ReportWriterAgent. When provided, activates refinement mode.

        Returns:
            List of atomic sub-question strings.
        """
        trimmed_summary = data_summary[:_MAX_SUMMARY_CHARS]
        is_refinement = bool(draft_report and draft_report.strip())

        if is_refinement:
            # ── Refinement mode: use gap-identification prompt ────────────────
            trimmed_draft = draft_report[:_MAX_DRAFT_CHARS]  # type: ignore[index]
            if self._chain is None:
                built = self._build_chain()      # heavy work — no lock held
                async with self._lock:
                    if self._chain is None:      # double-checked locking
                        self._chain = built

            from core.llm_client import get_structured_llm  # pylint: disable=import-outside-toplevel
            from core.config import NIM_MODEL_FLASH  # pylint: disable=import-outside-toplevel
            resolved = self._model or NIM_MODEL_FLASH
            refinement_llm = get_structured_llm(
                model=resolved,
                schema=SubQuestionsOutput,
                temperature=self._temperature,
            )
            refinement_chain = (
                ChatPromptTemplate.from_messages([
                    ("system", _SUBQ_REFINEMENT_SYSTEM),
                    ("human", _SUBQ_REFINEMENT_HUMAN),
                ])
                | refinement_llm
            )
            result: SubQuestionsOutput = await refinement_chain.ainvoke({
                "query": query,
                "data_summary": trimmed_summary,
                "draft_report": trimmed_draft,
            })
            logger.info(
                "[SubQuestionGenerator] Refinement mode — %d gap questions | coverage=%s",
                len(result.sub_questions),
                result.coverage_summary[:80],
            )
        else:
            # ── Initial mode: standard decomposition ─────────────────────────
            if self._chain is None:
                built = self._build_chain()      # heavy work — no lock held
                async with self._lock:
                    if self._chain is None:      # double-checked locking
                        self._chain = built
            chain = self._chain

            result = await chain.ainvoke({
                "query": query,
                "data_summary": trimmed_summary,
            })
            logger.info(
                "[SubQuestionGenerator] Initial mode — %d sub-questions | coverage=%s",
                len(result.sub_questions),
                result.coverage_summary[:80],
            )

        return result.sub_questions
