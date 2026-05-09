"""ReportWriterAgent — aggregates DS-STAR sub-question results into a report.

Receives the completed outputs from multiple parallel DS-STAR execution loops
(one per sub-question) and synthesises them into a structured markdown research
report with strict citation rules.

Uses the Flash model (NIM_MODEL_FLASH) for cost-efficient aggregation.

Architecture position:
    [DS-STAR+ mode]
    [parallel DS-STAR results] → ReportWriter → structured markdown report
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

logger = logging.getLogger("uvicorn.info")

_MAX_RESULT_CHARS_PER_Q = 1_500


# ---------------------------------------------------------------------------
# Pydantic output schema
# ---------------------------------------------------------------------------

class ReportOutput(BaseModel):
    """Structured research report output."""

    title: str = Field(
        description="A concise, descriptive title for the research report."
    )
    executive_summary: str = Field(
        description=(
            "A 2–4 sentence overview of the most important findings. "
            "Must reference concrete results, not vague generalisations."
        )
    )
    report_body: str = Field(
        description=(
            "Full markdown report body. Structure: \n"
            "## [Section title per sub-question]\n"
            "Finding text. Must cite sources as [Q1], [Q2], etc. "
            "where Q1 refers to sub-question 1.\n\n"
            "Do NOT synthesise beyond what the evidence supports."
        )
    )
    key_findings: List[str] = Field(
        description=(
            "Bullet list of the top 3–7 most important findings. "
            "Each finding must be concrete and cite its source [Qn]."
        ),
        min_length=3,
        max_length=7,
    )
    caveats: List[str] = Field(
        description=(
            "List of data quality issues, limitations, or warnings encountered "
            "during the analysis. Empty list if none."
        ),
        default_factory=list,
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_WRITER_SYSTEM = """\
You are a senior data science research analyst. Your task is to synthesise
the results of multiple independent data analyses into a single, coherent,
evidence-backed markdown research report.

STRICT RULES:
1. CITATION REQUIRED: Every factual claim must cite its source sub-question
   using the notation [Q1], [Q2], ..., [Qn]. Claims without citations are
   not allowed.
2. NO HALLUCINATION: Only include findings that are directly supported by
   the sub-question outputs provided. Do not invent statistics or trends.
3. NO VAGUE GENERALISATIONS: Every sentence in the report body must be
   grounded in specific numbers, patterns, or observations from the data.
4. STRUCTURE: Organise the report body into one section per sub-question.
   Use ## headings matching the sub-question topic.
5. CAVEATS: If any sub-question produced an error, NaN result, or incomplete
   output, document it in the caveats list. Do not omit failures.
6. EXECUTIVE SUMMARY: Write this last, after all sections are drafted.
   It must accurately represent the body, not guess at results.
"""

_WRITER_HUMAN_INITIAL = """\
ORIGINAL RESEARCH QUERY:
{query}

SUB-QUESTIONS AND THEIR RESULTS:
{qa_pairs}
"""

# Refinement variant: writer receives the draft + supplementary evidence
_WRITER_HUMAN_REFINEMENT = """\
ORIGINAL RESEARCH QUERY:
{query}

INITIAL DRAFT REPORT:
{draft_report}

SUPPLEMENTARY QUESTIONS AND THEIR RESULTS (integrate these into the report):
{supplementary_qa}

INSTRUCTION: Revise and enhance the Initial Draft Report by integrating the
Supplementary findings above. Do not discard any finding from the draft.
Add new sections or expand existing sections where the supplementary results
add value. Re-derive the executive summary and key_findings to reflect all
evidence (initial + supplementary). Every claim must still cite [Qn] notation.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_qa_pairs(
    sub_questions: List[str],
    results: List[Dict[str, Any]],
) -> str:
    """Formats sub-question / result pairs for the prompt.

    Args:
        sub_questions: Ordered list of sub-question strings.
        results: Ordered list of result dicts from DS-STAR runs.

    Returns:
        Formatted string with Q/A blocks.
    """
    lines = []
    for i, (question, result) in enumerate(zip(sub_questions, results), start=1):
        output = result.get("execution_output", "")
        insights = result.get("insights", {})
        summary = insights.get("summary", output) if insights else output
        trimmed = summary[:_MAX_RESULT_CHARS_PER_Q] if summary else "(no output)"
        status = result.get("status", "unknown")

        lines.append(f"[Q{i}] {question}")
        lines.append(f"Status: {status}")
        lines.append(f"Output:\n{trimmed}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ReportWriterAgent:
    """Synthesises parallel DS-STAR outputs into a structured research report.

    Enforces strict citation rules and no-hallucination constraints to ensure
    the report is reproducible and evidence-backed.

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

    def _build_chain(self, refinement: bool = False):
        """Builds (but does NOT cache) the LangChain pipeline.

        Heavy work — LLM init, imports — happens here, OUTSIDE the async lock.

        Args:
            refinement: If True, uses the refinement prompt that accepts a
                draft_report and supplementary_qa (DS-STAR+ iterative phase).
        """
        from core.llm_client import get_structured_llm  # pylint: disable=import-outside-toplevel
        from core.config import NIM_MODEL_FLASH  # pylint: disable=import-outside-toplevel
        resolved = self._model or NIM_MODEL_FLASH
        structured_llm = get_structured_llm(
            model=resolved,
            schema=ReportOutput,
            temperature=self._temperature,
        )
        human_template = _WRITER_HUMAN_REFINEMENT if refinement else _WRITER_HUMAN_INITIAL
        return (
            ChatPromptTemplate.from_messages([
                ("system", _WRITER_SYSTEM),
                ("human", human_template),
            ])
            | structured_llm
        )

    async def write(
        self,
        query: str,
        sub_questions: List[str],
        results: List[Dict[str, Any]],
        draft_report: Optional[str] = None,
        supplementary_questions: Optional[List[str]] = None,
        supplementary_results: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Generates the final research report from sub-question results.

        When ``draft_report`` + ``supplementary_questions`` + ``supplementary_results``
        are provided, operates in REFINEMENT mode (DS-STAR+ iterative phase):
        the writer integrates the supplementary evidence into the existing draft
        rather than writing from scratch. This implements the paper's Report Writer
        agent behaviour during the iterative refinement step.

        Args:
            query: The original open-ended research query.
            sub_questions: Ordered list of atomic sub-questions (initial round).
            results: Ordered list of DS-STAR result dicts (initial round).
            draft_report: Optional. The initial report body for refinement mode.
            supplementary_questions: Optional. Gap-filling sub-questions (refinement).
            supplementary_results: Optional. DS-STAR results for supplementary Qs.

        Returns:
            Dict with keys:
                - ``title`` (str): Report title.
                - ``executive_summary`` (str): 2–4 sentence overview.
                - ``report_body`` (str): Full markdown report with citations.
                - ``key_findings`` (List[str]): Top 3–7 cited findings.
                - ``caveats`` (List[str]): Data quality issues (if any).
        """
        is_refinement = (
            bool(draft_report)
            and bool(supplementary_questions)
            and bool(supplementary_results)
        )

        if is_refinement:
            # ── Refinement mode ───────────────────────────────────────────────────
            supplementary_qa = _format_qa_pairs(
                supplementary_questions,  # type: ignore[arg-type]
                supplementary_results,    # type: ignore[arg-type]
            )
            refinement_chain = self._build_chain(refinement=True)
            result: ReportOutput = await refinement_chain.ainvoke({
                "query": query,
                "draft_report": draft_report,
                "supplementary_qa": supplementary_qa,
            })
            logger.info(
                "[ReportWriter] Refinement complete | title=%s | findings=%d | caveats=%d",
                result.title[:60],
                len(result.key_findings),
                len(result.caveats),
            )
        else:
            # ── Initial mode ─────────────────────────────────────────────────────────
            qa_pairs = _format_qa_pairs(sub_questions, results)

            if self._chain is None:
                built = self._build_chain()      # heavy work — no lock held
                async with self._lock:
                    if self._chain is None:      # double-checked locking
                        self._chain = built
            chain = self._chain

            result = await chain.ainvoke({
                "query": query,
                "qa_pairs": qa_pairs,
            })
            logger.info(
                "[ReportWriter] Initial write | title=%s | findings=%d | caveats=%d",
                result.title[:60],
                len(result.key_findings),
                len(result.caveats),
            )

        return result.model_dump()
