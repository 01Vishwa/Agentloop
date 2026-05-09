"""Agent SSE controller.

Handles streaming responses for the /api/agent/run endpoint. Delegates
to the DsStarOrchestrator and serialises each AgentEvent as an SSE JSON line.

Fixes applied:
- run_id always uses a fresh uuid4 (never empty string) to avoid PK collisions.
- session_id stored separately as a column (not used as PK).
- Emits a ``run_started`` SSE event so the frontend can display run_id.
- All Supabase persistence is fully non-blocking (try/except, warns on failure).
- Gap 1: session_id threaded into orchestrator so executor sees only this
  session's files.
- ARCH-01: DsStarOrchestrator instances are cached at module level by
  (model, coder_model, temperature) key to eliminate per-request agent
  construction overhead (8 agents + locks + chains per request → zero).
- PERF-03: Client disconnection is detected with an asyncio.Event set by a
  lightweight background monitor task (polling every 1 s) rather than
  awaiting http_request.is_disconnected() on every single SSE event.
- ARCH-03: All Supabase helper functions are now awaited (async callers).
"""

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

from fastapi import Request

from eval.eval_logger import EvalLogger

from core.config import MAX_AGENT_ROUNDS
from core.ds_star_orchestrator import DsStarOrchestrator
from services.upload_service import clear_file_cache

logger = logging.getLogger("uvicorn.info")


# ---------------------------------------------------------------------------
# ARCH-01: Module-level orchestrator cache
# ---------------------------------------------------------------------------

# Cache key: (model, coder_model, temperature)
_OrchestratorKey = Tuple[Optional[str], Optional[str], Optional[float]]
_orchestrator_cache: Dict[_OrchestratorKey, DsStarOrchestrator] = {}


def _get_orchestrator(
    max_rounds: Optional[int],
    model: Optional[str],
    coder_model: Optional[str],
    temperature: Optional[float],
) -> DsStarOrchestrator:
    """Returns a cached DsStarOrchestrator for the given LLM configuration.

    Orchestrators are keyed by (model, coder_model, temperature) since those
    determine which ChatNVIDIA instances are built inside. ``max_rounds`` is
    not part of the key because it is per-request configurable and is applied
    to the orchestrator's ``_max_rounds`` attribute directly.

    DsStarOrchestrator.run() stores all per-run state in local variables, so
    sharing instances across requests is safe.

    Args:
        max_rounds: Per-request round limit.
        model: Reasoning LLM model override.
        coder_model: Code-generation LLM model override.
        temperature: Sampling temperature override.

    Returns:
        A ready-to-use DsStarOrchestrator instance.
    """
    cache_key: _OrchestratorKey = (model, coder_model, temperature)
    if cache_key not in _orchestrator_cache:
        _orchestrator_cache[cache_key] = DsStarOrchestrator(
            max_rounds=max_rounds,
            model=model,
            coder_model=coder_model,
            temperature=temperature,
        )
        logger.info(
            "[AgentController] Orchestrator created — model=%s, coder=%s, temp=%s",
            model, coder_model, temperature,
        )
    orchestrator = _orchestrator_cache[cache_key]
    # Apply per-request max_rounds without invalidating the cache
    orchestrator._max_rounds = max_rounds or MAX_AGENT_ROUNDS
    return orchestrator


# ---------------------------------------------------------------------------
# PERF-03: Background disconnect monitor
# ---------------------------------------------------------------------------

async def _monitor_disconnect(
    http_request: Request,
    disc_event: asyncio.Event,
    poll_interval: float = 1.0,
) -> None:
    """Sets ``disc_event`` when the HTTP client disconnects.

    Polls ``http_request.is_disconnected()`` once per ``poll_interval``
    seconds instead of awaiting it on every SSE event (PERF-03 fix).

    Args:
        http_request: FastAPI Request used to detect disconnection.
        disc_event: asyncio.Event set when disconnection is detected.
        poll_interval: Seconds between disconnection checks (default 1 s).
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

async def handle_agent_run(
    query: str,
    context: Dict[str, Any],
    session_id: str = "",
    max_rounds: Optional[int] = None,
    model: Optional[str] = None,
    coder_model: Optional[str] = None,
    temperature: Optional[float] = None,
    http_request: Optional[Request] = None,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Streams DS-STAR agent events as Server-Sent Events.

    Args:
        query: The user's natural language query.
        context: The processing context from /process.
        session_id: Optional client-provided session identifier.
        max_rounds: Override for MAX_AGENT_ROUNDS (1–10).
        model: Override for the reasoning LLM model.
        coder_model: Override for the code-generation LLM model.
        temperature: Override for the LLM sampling temperature (0.0–1.0).
        http_request: FastAPI Request object — used to detect early client
            disconnection via a background monitor task (PERF-03).
        user_id: Authenticated user ID.
        workspace_id: Optional workspace ID to scope the run.

    Yields:
        SSE-formatted ``data: <json>\\n\\n`` lines.
    """
    run_id = uuid.uuid4().hex
    _session_id = session_id or "__anon__"

    # ARCH-01: use cached orchestrator instead of creating a new one per request
    orchestrator = _get_orchestrator(max_rounds, model, coder_model, temperature)

    # Persist new run row — non-blocking
    await _try_create_run(run_id, _session_id, query, context, workspace_id, user_id)

    # Eval sidecar — passive observer, zero orchestration changes
    eval_logger = EvalLogger(run_id=run_id, query=query)

    # Emit run_id to the frontend immediately
    yield f"data: {json.dumps({'event': 'run_started', 'payload': {'run_id': run_id}})}\n\n"

    # PERF-03: Set up disconnect monitoring via asyncio.Event
    disc_event = asyncio.Event()
    monitor_task: Optional[asyncio.Task] = None
    if http_request is not None:
        monitor_task = asyncio.create_task(
            _monitor_disconnect(http_request, disc_event)
        )

    try:
        async for event in orchestrator.run(
            query,
            context,
            run_id=run_id,
            session_id=_session_id,
        ):
            # PERF-03: O(1) check — no await, no syscall per event
            if disc_event.is_set():
                logger.info(
                    "[AgentController] Client disconnected — aborting run_id=%s", run_id
                )
                await _try_update_run(run_id, {}, status="failed")
                return

            eval_logger.ingest(event)  # ← eval sidecar: passive observation

            payload = json.dumps(event, default=str)
            yield f"data: {payload}\n\n"

            event_type = event.get("event")
            event_payload = event.get("payload", {})

            if event_type == "completed":
                await _try_update_run(run_id, event_payload, status="completed")
            elif event_type == "error":
                await _try_update_run(run_id, event_payload, status="failed")
            elif event_type == "metrics":
                await _try_persist_metrics(run_id, event_payload)

    except Exception as exc:  # pylint: disable=broad-except
        error_event = json.dumps({
            "event": "error",
            "payload": {"message": str(exc)},
        })
        yield f"data: {error_event}\n\n"
        await _try_update_run(run_id, {"message": str(exc)}, status="failed")
        logger.error("[AgentController] Stream error for run_id=%s: %s", run_id, exc)

    finally:
        if monitor_task is not None:
            monitor_task.cancel()
        await eval_logger.finalize()  # ← eval sidecar: flush to Supabase
        clear_file_cache(_session_id)  # FIX 3: free uploaded file bytes on stream close
        yield "data: {\"event\": \"stream_end\", \"payload\": {}}\n\n"


# ---------------------------------------------------------------------------
# Supabase persistence helpers (async, non-blocking, warn on failure)
# ---------------------------------------------------------------------------

async def _try_create_run(
    run_id: str,
    session_id: str,
    query: str,
    context: Dict[str, Any],
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Attempts to create an agent_runs row in Supabase.

    Args:
        run_id: Unique run identifier (uuid4).
        session_id: Client-provided session identifier (stored separately).
        query: User query.
        context: Processing context.
        workspace_id: Optional workspace scope.
    """
    try:
        from services.supabase_service import create_agent_run  # pylint: disable=import-outside-toplevel
        file_names = list(context.get("combined_extractions", {}).keys())
        await create_agent_run(
            run_id=run_id,
            session_id=session_id,
            user_id=user_id,
            query=query,
            file_names=file_names,
            workspace_id=workspace_id,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("[AgentController] Could not persist run start: %s", exc)


async def _try_update_run(
    run_id: str,
    payload: Dict[str, Any],
    status: str = "completed",
) -> None:
    """Attempts to update the agent_runs row with the final result.

    Args:
        run_id: Unique run identifier.
        payload: Completed or error event payload dict.
        status: Final run status — ``"completed"`` or ``"failed"``.
    """
    try:
        from services.supabase_service import update_agent_run  # pylint: disable=import-outside-toplevel
        await update_agent_run(
            run_id=run_id,
            plan_steps=payload.get("plan_steps", []),
            final_code=payload.get("code", {}).get("Python", ""),
            rounds=payload.get("rounds", 0),
            insights=payload.get("insights", {}),
            execution_logs=payload.get("execution_logs", []),
            status=status,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("[AgentController] Could not persist run result: %s", exc)


async def _try_persist_metrics(run_id: str, metrics_payload: Dict[str, Any]) -> None:
    """Attempts to persist run evaluation metrics to Supabase.

    Args:
        run_id: Unique run identifier.
        metrics_payload: The ``payload`` dict from the ``metrics`` SSE event.
    """
    try:
        from services.supabase_service import update_agent_run_metrics  # pylint: disable=import-outside-toplevel
        await update_agent_run_metrics(
            run_id=run_id,
            metrics=metrics_payload.get("metrics", {}),
            total_run_ms=metrics_payload.get("total_run_ms", 0),
            complexity=metrics_payload.get("complexity", "easy"),
        )
        logger.info(
            "[AgentController] Metrics persisted for run_id=%s, complexity=%s, total_ms=%d",
            run_id,
            metrics_payload.get("complexity", "easy"),
            metrics_payload.get("total_run_ms", 0),
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("[AgentController] Could not persist run metrics: %s", exc)
