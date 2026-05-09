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

from core.deep_research_orchestrator import DeepResearchOrchestrator

logger = logging.getLogger("uvicorn.info")


# ---------------------------------------------------------------------------
# ARCH-01: Module-level orchestrator cache
# ---------------------------------------------------------------------------

_ResearchOrchestratorKey = Tuple[Optional[str], Optional[str], Optional[float], Optional[int]]
_research_orchestrator_cache: Dict[_ResearchOrchestratorKey, DeepResearchOrchestrator] = {}


def _get_research_orchestrator(
    max_rounds: Optional[int],
    model: Optional[str],
    coder_model: Optional[str],
    temperature: Optional[float],
    max_workers: Optional[int],
) -> DeepResearchOrchestrator:
    """Returns a cached DeepResearchOrchestrator for the given configuration.

    Keyed by (model, coder_model, temperature, max_workers). Per-request
    ``max_rounds`` is applied directly to the cached instance's ``_max_rounds``
    attribute without invalidating the cache.

    DeepResearchOrchestrator.run() stores all per-run state in local variables,
    so sharing instances across requests is safe.

    Args:
        max_rounds: Per-request round limit for sub-question DS-STAR runs.
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
            max_rounds=max_rounds,
            model=model,
            coder_model=coder_model,
            temperature=temperature,
            max_workers=max_workers,
        )
        logger.info(
            "[ResearchController] Orchestrator created — model=%s, workers=%s",
            model, max_workers,
        )
    orchestrator = _research_orchestrator_cache[cache_key]
    orchestrator._max_rounds = max_rounds or DS_STAR_PLUS_MAX_ROUNDS
    return orchestrator


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

    Yields:
        SSE-formatted ``data: <json>\\n\\n`` lines.
    """
    report_id = uuid.uuid4().hex
    _session_id = session_id or "__anon__"

    # ARCH-01: use cached orchestrator
    orchestrator = _get_research_orchestrator(
        max_rounds, model, coder_model, temperature, max_workers
    )

    # Persist report row before streaming starts
    await _try_create_report(report_id, _session_id, query, context, workspace_id, user_id)

    # Emit report_id to frontend immediately
    yield f"data: {json.dumps({'event': 'report_started', 'payload': {'report_id': report_id}})}\n\n"

    sub_questions_created = False

    try:
        # ARCH-05: pass session_id into run() so sub-question executors use correct bucket
        async for event in orchestrator.run(
            query, context, report_id=report_id, session_id=_session_id
        ):
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
