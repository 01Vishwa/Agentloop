"""DeepResearchOrchestrator — DS-STAR+ hierarchical research loop.

Implements the DS-STAR+ extension that handles open-ended research queries
by decomposing them into atomic sub-questions, running a full DS-STAR loop
for each sub-question in parallel, then aggregating the results into a
structured research report.

Flow:
    1. Detect open-ended query (or always run in research mode when called).
    2. FileAnalyzerAgent: build data description.
    3. SubQuestionGeneratorAgent: decompose query into atomic sub-questions.
    4. Parallel DS-STAR runs (max_workers=3, concurrency-controlled).
    5. ReportWriterAgent: aggregate into structured markdown report.
    6. Emit SSE events throughout for frontend streaming.
    7. Persist report + sub-question links to Supabase.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from core.analyzer.file_analyzer import FileAnalyzerAgent
from core.ds_star_orchestrator import DsStarOrchestrator, _event
from core.ds_star_plus.subquestion_agent import SubQuestionGeneratorAgent
from core.ds_star_plus.report_writer_agent import ReportWriterAgent
from core.retrieval.retriever import Retriever
from core.config import DS_STAR_PLUS_MAX_WORKERS, DS_STAR_PLUS_MAX_ROUNDS

logger = logging.getLogger("uvicorn.info")

def _make_ds_star_orchestrator(
    model: Optional[str],
    coder_model: Optional[str],
    temperature: Optional[float],
) -> DsStarOrchestrator:
    """Instantiates a **fresh** DsStarOrchestrator for a single sub-question run.

    P0 fix: the previous module-level cache (_ds_star_cache) shared one
    DsStarOrchestrator instance across all concurrent sub-question tasks.
    Because DsStarOrchestrator.coder (CoderAgent) carries mutable run-level
    state (``_raw_mode_engaged``), a single schema failure in any parallel
    task would call ``force_raw_completion()`` and permanently corrupt the
    shared instance for ALL concurrent and future sub-questions.

    Instantiating a fresh orchestrator per sub-question is the only safe
    option: each object's agents are stateless between instantiation and the
    end of ``run()``, so the 8-agent construction cost (~1 ms, no I/O) is
    negligible compared to the LLM round-trip latency.
    """
    logger.debug(
        "[DeepResearch] Creating fresh DsStarOrchestrator — model=%s, coder=%s",
        model, coder_model,
    )
    return DsStarOrchestrator(
        model=model,
        coder_model=coder_model,
        temperature=temperature,
    )


# ---------------------------------------------------------------------------
# Open-ended query classifier
# ---------------------------------------------------------------------------

_RESEARCH_KEYWORDS = frozenset({
    "report", "research", "summarise", "summarize", "overview", "analyse",
    "analyze", "explore", "investigate", "comprehensive", "deep dive",
    "findings", "insights", "trends", "patterns", "compare", "contrast",
    "relationship", "correlation", "across", "between", "profile",
})


def is_open_ended(query: str) -> bool:
    """Returns True if the query is open-ended and suited for DS-STAR+.

    Uses keyword heuristics only. The word-count heuristic has been removed
    because it incorrectly routed ordinary analytical questions (>15 words)
    into the expensive parallel DS-STAR+ pipeline. Research mode can be
    forced explicitly by calling the research endpoint directly, or via the
    UI research-mode toggle (AgentSettings).

    Args:
        query: The user's natural language query.

    Returns:
        True if the query contains research-oriented keywords.
    """
    lower = query.lower()
    words = set(lower.split())
    return bool(words & _RESEARCH_KEYWORDS)


# ---------------------------------------------------------------------------
# Sub-question DS-STAR runner helper
# ---------------------------------------------------------------------------

async def _run_single_ds_star(
    question: str,
    context: Dict[str, Any],
    orchestrator: DsStarOrchestrator,
    max_rounds: int,
    sub_run_id: str,
    session_id: str = "__anon__",
) -> Dict[str, Any]:
    """Runs a complete DS-STAR loop for one sub-question and returns its result.

    P1-06 fix: accepts a pre-built, cached ``DsStarOrchestrator`` instead of
    instantiating one per sub-question. ``max_rounds`` is forwarded into
    ``orchestrator.run()`` (not mutated on the shared instance).

    Args:
        question: The atomic sub-question to answer.
        context: Processing context passed from the research endpoint.
        orchestrator: Shared cached DsStarOrchestrator instance.
        max_rounds: Maximum orchestrator rounds per sub-question.
        sub_run_id: Unique run ID for this sub-question run.
        session_id: Client session identifier — scopes executor file access so
            sub-runs see only this session's uploaded files, not __anon__ bucket.

    Returns:
        Dict containing status, execution_output, insights, code, rounds, run_id.
    """
    result: Dict[str, Any] = {
        "status": "failed",
        "execution_output": "",
        "insights": {},
        "code": "",
        "rounds": 0,
        "run_id": sub_run_id,
    }

    try:
        async for event in orchestrator.run(
            question,
            context,
            run_id=sub_run_id,
            session_id=session_id,
            max_rounds=max_rounds,
        ):
            event_type = event.get("event")
            payload = event.get("payload", {})
            if event_type == "completed":
                result["status"] = "completed"
                result["insights"] = payload.get("insights", {})
                result["code"] = payload.get("code", {}).get("Python", "")
                result["rounds"] = payload.get("rounds", 0)
                result["plan_steps"] = payload.get("plan_steps", [])
                result["execution_logs"] = payload.get("execution_logs", [])
                exec_out = payload.get("insights", {}).get("summary", "")
                result["execution_output"] = exec_out
            elif event_type == "execution_result":
                # Capture raw stdout from the last successful execution
                if payload.get("success"):
                    result["execution_output"] = payload.get("stdout", "")
            elif event_type == "metrics":
                final_status = payload.get("metrics", {}).get("final_status", "")
                if final_status == "max_rounds_reached":
                    result["status"] = "max_rounds_reached"
            elif event_type == "error":
                result["status"] = "failed"
    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "[DeepResearch] Sub-question DS-STAR failed | run_id=%s: %s",
            sub_run_id,
            exc,
        )
        result["status"] = "failed"
        result["execution_output"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Deep Research Orchestrator
# ---------------------------------------------------------------------------

class DeepResearchOrchestrator:
    """Runs the DS-STAR+ deep research loop and streams progress events.

    Decomposes an open-ended research query into atomic sub-questions,
    executes a DS-STAR loop per sub-question (concurrently, up to
    ``max_workers``), then synthesises results via ReportWriterAgent.

    Attributes:
        analyzer: FileAnalyzerAgent for building data descriptions.
        subq_agent: SubQuestionGeneratorAgent for query decomposition.
        report_writer: ReportWriterAgent for report synthesis.
        retriever: Retriever for top-K file selection on large corpora.
        _max_rounds: Max orchestrator rounds for each sub-question DS-STAR run.
        _max_workers: Max parallel DS-STAR runs.
    """

    def __init__(
        self,
        max_rounds: Optional[int] = None,
        model: Optional[str] = None,
        coder_model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_workers: Optional[int] = None,
    ) -> None:
        """Initialises the deep research orchestrator.

        Args:
            max_rounds: Maximum DS-STAR rounds per sub-question.
            model: LLM model identifier for Pro-tier agents.
            coder_model: LLM model identifier for code generation.
            temperature: LLM sampling temperature.
            max_workers: Maximum parallel DS-STAR sub-executions.
        """
        self._max_rounds = max_rounds or DS_STAR_PLUS_MAX_ROUNDS
        self._model = model
        self._coder_model = coder_model
        self._temperature = temperature
        self._max_workers = max_workers or DS_STAR_PLUS_MAX_WORKERS
        self.analyzer = FileAnalyzerAgent()
        self.subq_agent = SubQuestionGeneratorAgent()
        self.report_writer = ReportWriterAgent()
        self.retriever = Retriever()

    async def run(
        self,
        query: str,
        context: Dict[str, Any],
        report_id: str = "",
        session_id: str = "__anon__",
        max_rounds: Optional[int] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Executes the DS-STAR+ research loop and yields SSE events.

        Args:
            query: The user's open-ended research query.
            context: Processing context from /process endpoint,
                including ``combined_extractions`` and ``files_processed``.
            report_id: Unique report identifier for Supabase persistence.
            session_id: Client session identifier — forwarded to all
                sub-question DS-STAR runs so their executors access the
                correct session file bucket instead of ``__anon__``.
            max_rounds: Per-call round limit for sub-question runs.
                When provided, overrides ``self._max_rounds`` for this
                invocation only (P1-01 fix: no mutation of shared state).

        Yields:
            AgentEvent dicts for SSE streaming.
        """
        # Resolve effective rounds locally — never write to self._max_rounds
        _effective_max_rounds: int = (
            max_rounds if max_rounds is not None else self._max_rounds
        )
        run_t0 = time.monotonic()
        report_id = report_id or uuid.uuid4().hex
        combined = context.get("combined_extractions", {})

        yield _event(
            "research_started",
            message="DS-STAR+ deep research mode initiated.",
            report_id=report_id,
        )

        # ── Stage 1: File Analysis ────────────────────────────────────────────
        yield _event("analyzing", message="Analyzing data files for research context…")
        try:
            data_description = await self.analyzer.analyze(combined, session_id=session_id)
            yield _event(
                "analysis_complete",
                message="Data analysis complete.",
                data_description=data_description,
            )
        except Exception as exc:  # pylint: disable=broad-except
            data_description = f"Data description unavailable: {exc}"
            yield _event("warning", message=f"File analysis error: {exc}")

        # Retrieval: filter to top-K relevant files for large corpora
        filtered_combined = self.retriever.retrieve_combined_extractions(
            query=query,
            combined_extractions=combined,
        )
        filtered_count = len(filtered_combined)
        if filtered_count < len(combined):
            yield _event(
                "retrieval_complete",
                message=(
                    f"Retrieval filtered {len(combined)} → {filtered_count} "
                    f"most relevant files."
                ),
                selected_files=list(filtered_combined.keys()),
            )
            # Use filtered context for sub-question runs
            context = {**context, "combined_extractions": filtered_combined}

        # ── Stage 2: Sub-Question Generation ─────────────────────────────────
        yield _event(
            "generating_subquestions",
            message="Decomposing query into atomic sub-questions…",
        )
        try:
            sub_questions: List[str] = await self.subq_agent.generate(
                query=query,
                data_summary=data_description,
            )
            yield _event(
                "subquestions_ready",
                message=f"Generated {len(sub_questions)} sub-questions.",
                sub_questions=sub_questions,
                count=len(sub_questions),
            )
            logger.info(
                "[DeepResearch] report_id=%s | %d sub-questions generated",
                report_id,
                len(sub_questions),
            )
        except Exception as exc:  # pylint: disable=broad-except
            yield _event("error", message=f"SubQuestion generation failed: {exc}")
            return

        # ── Stage 3: Parallel DS-STAR Runs ────────────────────────────────────
        yield _event(
            "running_subquestions",
            message=(
                f"Running {len(sub_questions)} DS-STAR analyses "
                f"(max_workers={self._max_workers})…"
            ),
            total=len(sub_questions),
        )

        sub_run_ids = [uuid.uuid4().hex for _ in sub_questions]

        # Semaphore-controlled concurrency
        semaphore = asyncio.Semaphore(self._max_workers)

        async def _run_with_semaphore(i: int, question: str) -> Dict[str, Any]:
            # Build the started event BEFORE acquiring the semaphore so
            # its data is ready, but it is returned alongside the result
            # for the outer loop to yield in order.
            started_event = _event(
                "subquestion_started",
                message=f"[Q{i + 1}] Running: {question[:80]}",
                index=i,
                question=question,
                sub_run_id=sub_run_ids[i],
            )
            async with semaphore:
                # P1-03 fix: event is now created BEFORE the await so it
                # accurately represents when the task entered execution.
                logger.info(
                    "[DeepResearch] Q%d started | run_id=%s", i + 1, sub_run_ids[i]
                )
                # P0 fix: always construct a fresh orchestrator per sub-question.
                # Sharing a single instance causes CoderAgent._raw_mode_engaged
                # to leak across parallel tasks on schema failure.
                sub_orchestrator = _make_ds_star_orchestrator(
                    self._model, self._coder_model, self._temperature
                )
                result = await _run_single_ds_star(
                    question=question,
                    context=context,
                    orchestrator=sub_orchestrator,
                    max_rounds=_effective_max_rounds,
                    sub_run_id=sub_run_ids[i],
                    session_id=session_id,
                )
                logger.info(
                    "[DeepResearch] Q%d done | status=%s | run_id=%s",
                    i + 1,
                    result["status"],
                    sub_run_ids[i],
                )
                return started_event, result

        # Run all sub-questions concurrently under semaphore
        tasks = [
            asyncio.create_task(_run_with_semaphore(i, q))
            for i, q in enumerate(sub_questions)
        ]

        results: List[Dict[str, Any]] = [None] * len(sub_questions)  # type: ignore[assignment]
        for coro in asyncio.as_completed(tasks):
            start_event, sub_result = await coro
            idx = sub_run_ids.index(sub_result["run_id"])
            results[idx] = sub_result

            # Emit start event (may be slightly delayed but ordering is acceptable)
            yield start_event
            yield _event(
                "subquestion_complete",
                message=(
                    f"Sub-question '{sub_questions[idx][:60]}…' — "
                    f"status: {sub_result['status']}"
                ),
                index=idx,
                status=sub_result["status"],
                sub_run_id=sub_result["run_id"],
                result=sub_result,
            )

        yield _event(
            "all_subquestions_complete",
            message=f"All {len(sub_questions)} sub-questions completed.",
            statuses=[r["status"] for r in results],
        )

        # ── Stage 4: Initial Report Writing ──────────────────────────────────
        yield _event("writing_report", message="Synthesising initial research report…")
        initial_report: dict = {}
        try:
            initial_report = await self.report_writer.write(
                query=query,
                sub_questions=sub_questions,
                results=results,
            )
            yield _event(
                "initial_report_ready",
                message="Initial report drafted — starting refinement round…",
                title=initial_report.get("title", ""),
                key_findings_count=len(initial_report.get("key_findings", [])),
            )
            logger.info(
                "[DeepResearch] report_id=%s | Initial report ready — %d findings",
                report_id,
                len(initial_report.get("key_findings", [])),
            )
        except Exception as exc:  # pylint: disable=broad-except
            yield _event("error", message=f"Initial ReportWriter failed: {exc}")
            logger.error("[DeepResearch] report_id=%s | Initial write error: %s", report_id, exc)
            return

        # ── Stage 5: Iterative Refinement (DS-STAR+ paper §2) ────────────────
        # Re-engage SubQuestion generator with the draft to identify gaps,
        # run supplementary DS-STAR analyses, then call the writer in
        # refinement mode to integrate the new evidence into the final report.
        final_report = initial_report
        draft_body = initial_report.get("report_body", "")

        yield _event(
            "refining_report",
            message="Identifying informational gaps for iterative refinement…",
        )
        try:
            supplementary_questions: List[str] = await self.subq_agent.generate(
                query=query,
                data_summary=data_description,
                draft_report=draft_body,
            )

            if supplementary_questions:
                yield _event(
                    "supplementary_subquestions_ready",
                    message=(
                        f"Generated {len(supplementary_questions)} supplementary "
                        f"questions to fill gaps."
                    ),
                    sub_questions=supplementary_questions,
                    count=len(supplementary_questions),
                )
                logger.info(
                    "[DeepResearch] report_id=%s | %d supplementary questions",
                    report_id,
                    len(supplementary_questions),
                )

                # Run supplementary DS-STAR analyses (semaphore-controlled)
                sup_run_ids = [uuid.uuid4().hex for _ in supplementary_questions]
                sup_semaphore = asyncio.Semaphore(self._max_workers)

                async def _sup_run(i: int, question: str) -> Dict[str, Any]:
                    async with sup_semaphore:
                        # P0 fix: fresh orchestrator per supplementary question —
                        # same race-condition isolation as the primary run above.
                        sup_orchestrator = _make_ds_star_orchestrator(
                            self._model, self._coder_model, self._temperature
                        )
                        return await _run_single_ds_star(
                            question=question,
                            context=context,
                            orchestrator=sup_orchestrator,
                            max_rounds=_effective_max_rounds,
                            sub_run_id=sup_run_ids[i],
                            session_id=session_id,
                        )

                sup_tasks = [
                    asyncio.create_task(_sup_run(i, q))
                    for i, q in enumerate(supplementary_questions)
                ]
                supplementary_results: List[Dict[str, Any]] = [None] * len(supplementary_questions)  # type: ignore[assignment]
                for coro in asyncio.as_completed(sup_tasks):
                    sup_result = await coro
                    idx = sup_run_ids.index(sup_result["run_id"])
                    supplementary_results[idx] = sup_result
                    yield _event(
                        "supplementary_subquestion_complete",
                        message=f"[Supplementary Q{idx + 1}] done — status: {sup_result['status']}",
                        index=idx,
                        status=sup_result["status"],
                    )

                # Refine the report by integrating supplementary evidence
                yield _event("writing_report", message="Integrating supplementary evidence into final report…")
                final_report = await self.report_writer.write(
                    query=query,
                    sub_questions=sub_questions,
                    results=results,
                    draft_report=draft_body,
                    supplementary_questions=supplementary_questions,
                    supplementary_results=supplementary_results,
                )
                logger.info(
                    "[DeepResearch] report_id=%s | Refinement complete — %d findings",
                    report_id,
                    len(final_report.get("key_findings", [])),
                )
            else:
                logger.info(
                    "[DeepResearch] report_id=%s | No supplementary questions generated — skipping refinement.",
                    report_id,
                )

        except Exception as ref_exc:  # pylint: disable=broad-except
            # Refinement failure is non-fatal — fall back to initial report
            logger.warning(
                "[DeepResearch] report_id=%s | Refinement error (using initial report): %s",
                report_id,
                ref_exc,
            )
            yield _event("warning", message=f"Refinement skipped (non-fatal): {ref_exc}")
            final_report = initial_report

        # ── Stage 6: Emit final report ────────────────────────────────────────
        total_ms = int((time.monotonic() - run_t0) * 1000)
        yield _event(
            "research_complete",
            message="DS-STAR+ research report ready.",
            report_id=report_id,
            title=final_report.get("title", ""),
            executive_summary=final_report.get("executive_summary", ""),
            report_body=final_report.get("report_body", ""),
            key_findings=final_report.get("key_findings", []),
            caveats=final_report.get("caveats", []),
            sub_questions=sub_questions,
            sub_run_ids=sub_run_ids,
            total_ms=total_ms,
        )
        logger.info(
            "[DeepResearch] report_id=%s complete | %d findings | %d caveats | %dms",
            report_id,
            len(final_report.get("key_findings", [])),
            len(final_report.get("caveats", [])),
            total_ms,
        )
