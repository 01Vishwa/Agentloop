"""Research API controller — DS-STAR+ deep research endpoint handler.

Handles streaming responses for the /api/research endpoint.
Delegates to DeepResearchOrchestrator and serialises each AgentEvent as SSE.

Fixes applied:
- ARCH-01: DeepResearchOrchestrator instances are cached at module level by
  (model, coder_model, temperature, max_workers) key to eliminate per-request
  construction overhead (analyzer + subq_agent + report_writer + retriever +
  Retriever embedding model load on every request → zero).
- ARCH-05: session_id is now forwarded into orchestrator.run() so all
  sub-question DS-STAR runs use the correct session bucket.
- ARCH-03: All Supabase helper functions are now awaited (async callers).
- Persists a new ``reports`` row before the loop starts.
- Persists ``sub_questions`` rows for each generated sub-question.
- Persists final report content when research_complete is emitted.
- Marks report as ``failed`` if an error event terminates the stream.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from fastapi import Request

from core.deep_research_orchestrator import DeepResearchOrchestrator

logger = logging.getLogger("uvicorn.info")


# ---------------------------------------------------------------------------
# ARCH-01: Module-level orchestrator cache
# ---------------------------------------------------------------------------

_ResearchOrchestratorKey = Tuple[Optional[str], Optional[str], Optional[float], Optional[int]]
_research_orchestrator_cache: Dict[_ResearchOrchestratorKey, DeepResearchOrchestrator] = {}


def _get_research_orchestrator(
    model: Optional[str],
    coder_model: Optional[str],
    temperature: Optional[float],
    max_workers: Optional[int],
) -> DeepResearchOrchestrator:
    """Returns a cached DeepResearchOrchestrator for the given configuration.

    Keyed by (model, coder_model, temperature, max_workers). Per-request
    ``max_rounds`` is forwarded into ``orchestrator.run()`` rather than
    mutated on the cached instance (P1-01 fix: eliminates data race).

    Args:
        model: Reasoning LLM model override.
        coder_model: Code-generation LLM model override.
        temperature: Sampling temperature override.
        max_workers: Max parallel DS-STAR sub-runs override.

    Returns:
        A ready-to-use DeepResearchOrchestrator instance.
    """
    from core.config import DS_STAR_PLUS_MAX_ROUNDS  # pylint: disable=import-outside-toplevel
    cache_key: _ResearchOrchestratorKey = (model, coder_model, temperature, max_workers)
    if cache_key not in _research_orchestrator_cache:
        _research_orchestrator_cache[cache_key] = DeepResearchOrchestrator(
            model=model,
            coder_model=coder_model,
            temperature=temperature,
            max_workers=max_workers,
        )
        logger.info(
            "[ResearchController] Orchestrator created — model=%s, workers=%s",
            model, max_workers,
        )
    return _research_orchestrator_cache[cache_key]


# ---------------------------------------------------------------------------
# PERF-03: Background disconnect monitor (mirrors agent_controller)
# ---------------------------------------------------------------------------

async def _monitor_disconnect(
    http_request: Request,
    disc_event: asyncio.Event,
    poll_interval: float = 1.0,
) -> None:
    """Sets ``disc_event`` when the HTTP client disconnects.

    P2-02 fix: deep-research runs can now be aborted on client disconnect
    the same way DS-STAR runs are handled in agent_controller.
    """
    while not disc_event.is_set():
        try:
            if await http_request.is_disconnected():
                disc_event.set()
                return
        except Exception:  # pylint: disable=broad-except
            return  # Transport gone — treat as disconnected
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------

async def handle_research_run(
    query: str,
    context: Dict[str, Any],
    session_id: str = "",
    max_rounds: Optional[int] = None,
    model: Optional[str] = None,
    coder_model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_workers: Optional[int] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    http_request: Optional[Request] = None,
) -> AsyncGenerator[str, None]:
    """Streams DS-STAR+ research events as Server-Sent Events.

    Args:
        query: The user's open-ended research query.
        context: Processing context from /process endpoint.
        session_id: Optional client-provided session identifier.
        max_rounds: Override for MAX_AGENT_ROUNDS per sub-question.
        model: Override for the reasoning LLM model.
        coder_model: Override for the code-generation LLM model.
        temperature: Override for the LLM sampling temperature (0.0–1.0).
        max_workers: Override for max parallel DS-STAR sub-runs.
        user_id: Authenticated user ID.
        workspace_id: Optional workspace scope.
        http_request: FastAPI Request object — used to detect early client
            disconnection via a background monitor task (P2-02 fix).

    Yields:
        SSE-formatted ``data: <json>\\n\\n`` lines.
    """
    report_id = uuid.uuid4().hex
    _session_id = session_id or "__anon__"
    from core.config import DS_STAR_PLUS_MAX_ROUNDS  # pylint: disable=import-outside-toplevel
    _max_rounds = max_rounds or DS_STAR_PLUS_MAX_ROUNDS

    # ARCH-01: use cached orchestrator
    # P1-01 fix: max_rounds forwarded to run(), NOT mutated on cached instance.
    orchestrator = _get_research_orchestrator(
        model, coder_model, temperature, max_workers
    )

    # WS-01: Auto-hydrate context from workspace disk when the session cache is
    # empty.  This mirrors the same fix in agent_controller.py — without it,
    # DS-STAR+ deep research runs silently get empty context when files were
    # uploaded via the workspace path and the session cache was evicted.
    _combined = context.get("combined_extractions", {})
    if not _combined and workspace_id:
        logger.info(
            "[ResearchController] WS-01: context is empty but workspace_id=%s — hydrating.",
            workspace_id[:8],
        )
        from api.controllers.agent_controller import _hydrate_context_from_workspace  # pylint: disable=import-outside-toplevel
        hydrated = await _hydrate_context_from_workspace(workspace_id, _session_id)
        if hydrated:
            context = {
                **context,
                "combined_extractions": hydrated.get("combined_extractions", {}),
                "files_processed": hydrated.get("files_processed", 0),
            }
            logger.info(
                "[ResearchController] WS-01: context hydrated with %d file(s).",
                context.get("files_processed", 0),
            )

    # Persist report row before streaming starts
    await _try_create_report(report_id, _session_id, query, context, workspace_id, user_id)

    # Emit report_id to frontend immediately
    yield f"data: {json.dumps({'event': 'report_started', 'payload': {'report_id': report_id}})}\n\n"

    # BUG 4 fix: Emit parse_warnings from /process as SSE warning events before
    # any sub-question orchestration begins.  Mirrors agent_controller behaviour.
    for pw in context.get("parse_warnings", []):
        warn_payload = json.dumps({
            "event": "warning",
            "payload": {
                "message": pw.get("message", "A file produced minimal extractable content."),
                "filename": pw.get("filename", ""),
                "source": "parse_warning",
            },
        })
        yield f"data: {warn_payload}\n\n"

    # P2-02 fix: Set up disconnect monitoring via asyncio.Event
    disc_event = asyncio.Event()
    monitor_task: Optional[asyncio.Task] = None
    if http_request is not None:
        monitor_task = asyncio.create_task(
            _monitor_disconnect(http_request, disc_event)
        )

    sub_questions_created = False

    try:
        # ARCH-05: pass session_id into run() so sub-question executors use correct bucket
        async for event in orchestrator.run(
            query, context, report_id=report_id, session_id=_session_id,
            max_rounds=_max_rounds,
        ):
            # P2-02: O(1) disconnect check before each SSE event
            if disc_event.is_set():
                logger.info(
                    "[ResearchController] Client disconnected — aborting report_id=%s",
                    report_id,
                )
                await _try_fail_report(report_id)
                return
            payload = json.dumps(event, default=str)
            yield f"data: {payload}\n\n"

            event_type = event.get("event")
            event_payload = event.get("payload", {})

            if event_type == "subquestions_ready" and not sub_questions_created:
                sub_questions = event_payload.get("sub_questions", [])
                await _try_create_subquestions(report_id, sub_questions)
                sub_questions_created = True

            elif event_type == "subquestion_started":
                sub_run_id = event_payload.get("sub_run_id")
                question = event_payload.get("question")
                if sub_run_id and question:
                    await _try_create_run(
                        run_id=sub_run_id,
                        session_id=_session_id,
                        query=question,
                        context=context,
                        workspace_id=workspace_id,
                        user_id=user_id,
                    )

            elif event_type == "subquestion_complete":
                sub_run_id = event_payload.get("sub_run_id", "")
                # 1. Update the sub-run's agent_runs row
                await _try_update_run(
                    run_id=sub_run_id,
                    payload=event_payload.get("result", {}),
                    status=event_payload.get("status", "completed"),
                )
                # 2. Link it safely in sub_questions
                await _try_update_subquestion(
                    report_id=report_id,
                    index=event_payload.get("index", 0),
                    status=event_payload.get("status", "completed"),
                    result_run_id=sub_run_id,
                )

            elif event_type == "research_complete":
                await _try_update_report(
                    report_id=report_id,
                    event_payload=event_payload,
                    status="completed",
                )

            elif event_type == "error":
                await _try_fail_report(report_id)

    except asyncio.CancelledError:
        # P2-01 fix: CancelledError inherits from BaseException, not Exception.
        # Must be caught explicitly so the report is marked failed on disconnect.
        logger.info(
            "[ResearchController] Stream cancelled (client disconnect?) — report_id=%s",
            report_id,
        )
        await _try_fail_report(report_id)
        raise

    except Exception as exc:  # pylint: disable=broad-except
        error_event = json.dumps({
            "event": "error",
            "payload": {"message": str(exc)},
        })
        yield f"data: {error_event}\n\n"
        await _try_fail_report(report_id)
        logger.error(
            "[ResearchController] Stream error for report_id=%s: %s",
            report_id,
            exc,
        )

    finally:
        if monitor_task is not None:
            monitor_task.cancel()
        yield "data: {\"event\": \"stream_end\", \"payload\": {}}\n\n"


# ---------------------------------------------------------------------------
# Supabase persistence helpers (async, non-blocking, warn on failure)
# ---------------------------------------------------------------------------

async def _try_create_report(
    report_id: str,
    session_id: str,
    query: str,
    context: Dict[str, Any],
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Creates a new reports row with status=running."""
    try:
        from services.supabase_service import create_report_run  # pylint: disable=import-outside-toplevel
        file_names = list(context.get("combined_extractions", {}).keys())
        await create_report_run(
            report_id=report_id,
            query=query,
            file_names=file_names,
            session_id=session_id,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "[ResearchController] Could not persist report start: %s", exc
        )


async def _try_create_subquestions(
    report_id: str,
    sub_questions: List[str],
) -> None:
    """Creates sub_questions rows for each generated question."""
    try:
        from services.supabase_service import create_subquestions  # pylint: disable=import-outside-toplevel
        await create_subquestions(report_id=report_id, sub_questions=sub_questions)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "[ResearchController] Could not persist sub_questions: %s", exc
        )


async def _try_update_subquestion(
    report_id: str,
    index: int,
    status: str,
    result_run_id: str,
) -> None:
    """Updates a single sub_question row with its DS-STAR result."""
    try:
        from services.supabase_service import link_subquestion_run  # pylint: disable=import-outside-toplevel
        await link_subquestion_run(
            report_id=report_id,
            question_index=index,
            status=status,
            result_run_id=result_run_id,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "[ResearchController] Could not update sub_question idx=%d: %s",
            index,
            exc,
        )


async def _try_update_report(
    report_id: str,
    event_payload: Dict[str, Any],
    status: str = "completed",
) -> None:
    """Persists the final report content to the reports table."""
    try:
        from services.supabase_service import update_report_status  # pylint: disable=import-outside-toplevel
        await update_report_status(
            report_id=report_id,
            status=status,
            title=event_payload.get("title", ""),
            executive_summary=event_payload.get("executive_summary", ""),
            report_body=event_payload.get("report_body", ""),
            key_findings=event_payload.get("key_findings", []),
            caveats=event_payload.get("caveats", []),
            total_ms=event_payload.get("total_ms", 0),
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "[ResearchController] Could not persist report result: %s", exc
        )


async def _try_fail_report(report_id: str) -> None:
    """Marks a report as failed in Supabase."""
    try:
        from services.supabase_service import update_report_status  # pylint: disable=import-outside-toplevel
        await update_report_status(report_id=report_id, status="failed")
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "[ResearchController] Could not mark report as failed: %s", exc
        )


async def _try_create_run(
    run_id: str,
    session_id: str,
    query: str,
    context: Dict[str, Any],
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Attempts to create an agent_runs row in Supabase."""
    try:
        from services.supabase_service import create_agent_run  # pylint: disable=import-outside-toplevel
        file_names = list(context.get("combined_extractions", {}).keys())
        await create_agent_run(
            run_id=run_id,
            session_id=session_id,
            query=query,
            file_names=file_names,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("[ResearchController] Could not persist run start: %s", exc)


async def _try_update_run(
    run_id: str,
    payload: Dict[str, Any],
    status: str = "completed",
) -> None:
    """Attempts to update the agent_runs row with the final result."""
    try:
        from services.supabase_service import update_agent_run  # pylint: disable=import-outside-toplevel
        await update_agent_run(
            run_id=run_id,
            plan_steps=payload.get("plan_steps", []),
            final_code=payload.get("code", {}).get("Python", "") if isinstance(payload.get("code"), dict) else payload.get("code", ""),
            rounds=payload.get("rounds", 0),
            insights=payload.get("insights", {}),
            execution_logs=payload.get("execution_logs", []),
            status=status,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("[ResearchController] Could not persist run result: %s", exc)
