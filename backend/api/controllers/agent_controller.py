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
- WS-01: Workspace auto-hydration: when context is empty and workspace_id is
  present, files are loaded from disk into the session cache so the orchestrator
  always has data — fixing the root cause of the "files disappear" bug where
  workspace files shown in the UI (fetched from Supabase) were never put through
  /process and were therefore invisible to the backend session cache.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, Optional, Tuple

from fastapi import Request

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
    model: Optional[str],
    coder_model: Optional[str],
    temperature: Optional[float],
) -> DsStarOrchestrator:
    """Returns a cached DsStarOrchestrator for the given LLM configuration.

    Orchestrators are keyed by (model, coder_model, temperature) since those
    determine which ChatNVIDIA instances are built inside. ``max_rounds`` is
    NOT part of the key and NOT mutated on the cached instance — it is
    resolved at call-time and forwarded into ``orchestrator.run()`` directly
    (P1-01 fix: eliminates the shared-instance data race).

    DsStarOrchestrator.run() stores all per-run state in local variables, so
    sharing instances across requests is safe.

    Args:
        model: Reasoning LLM model override.
        coder_model: Code-generation LLM model override.
        temperature: Sampling temperature override.

    Returns:
        A ready-to-use DsStarOrchestrator instance.
    """
    cache_key: _OrchestratorKey = (model, coder_model, temperature)
    if cache_key not in _orchestrator_cache:
        _orchestrator_cache[cache_key] = DsStarOrchestrator(
            model=model,
            coder_model=coder_model,
            temperature=temperature,
        )
        logger.info(
            "[AgentController] Orchestrator created — model=%s, coder=%s, temp=%s",
            model, coder_model, temperature,
        )
    return _orchestrator_cache[cache_key]


# ---------------------------------------------------------------------------
# WS-01: Workspace auto-hydration helper
# ---------------------------------------------------------------------------

async def _hydrate_context_from_workspace(
    workspace_id: str,
    session_id: str,
) -> Dict[str, Any]:
    """Loads workspace files from disk into the session cache and returns context.

    Called when ``handle_agent_run`` receives an empty context but a
    ``workspace_id`` is set.  This happens when the frontend loads files from
    Supabase (workspace view) and starts a run without going through ``/process``
    — the in-memory session cache is empty even though files exist on disk.

    Strategy:
    1. Scan ``/workspace/{workspace_id}/`` for data files.
    2. Read each file's bytes into the session cache (``_FILE_CACHE``).
    3. Run the synchronous ``process_documents`` to extract schema context.
    4. Return the resulting ``combined_extractions`` dict.

    Args:
        workspace_id: UUID of the workspace whose files to load.
        session_id: Session bucket to populate in the file cache.

    Returns:
        A context dict compatible with what ``/process`` would return,
        or an empty dict if no files are found.
    """
    import os  # pylint: disable=import-outside-toplevel
    from services.upload_service import _FILE_CACHE, _CACHE_LOCK  # pylint: disable=import-outside-toplevel
    from services.process_service import process_documents  # pylint: disable=import-outside-toplevel

    workspace_base = os.environ.get("WORKSPACE_FILES_DIR", "/workspace")
    workspace_dir = os.path.join(workspace_base, workspace_id)

    if not os.path.isdir(workspace_dir):
        logger.warning(
            "[AgentController] Workspace dir not found: %s — cannot auto-hydrate.",
            workspace_dir,
        )
        return {}

    # Gather all files from the workspace directory
    loaded_names = []
    for filename in os.listdir(workspace_dir):
        file_path = os.path.join(workspace_dir, filename)
        if not os.path.isfile(file_path):
            continue
        try:
            with open(file_path, "rb") as fh:
                content = fh.read()
            with _CACHE_LOCK:
                _FILE_CACHE.setdefault(session_id, {})[filename] = content
            loaded_names.append(filename)
            logger.info(
                "[AgentController] WS-01: hydrated %s (%d bytes) → session=%s",
                filename, len(content), session_id[:8],
            )
        except OSError as exc:
            logger.warning(
                "[AgentController] WS-01: could not read %s: %s", file_path, exc
            )

    if not loaded_names:
        return {}

    # Build processing context synchronously (CSV/PDF parsing is blocking)
    loop = asyncio.get_running_loop()
    try:
        context = await loop.run_in_executor(
            None, process_documents, loaded_names, session_id
        )
        logger.info(
            "[AgentController] WS-01: auto-hydrated %d file(s) from workspace %s.",
            len(loaded_names), workspace_id[:8],
        )
        return context
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "[AgentController] WS-01: process_documents failed during hydration: %s", exc
        )
        return {}


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
    _max_rounds = max_rounds or MAX_AGENT_ROUNDS

    # WS-01: Auto-hydrate context from workspace disk when the session cache is
    # empty.  This happens when the frontend loads workspace files from Supabase
    # and starts a run without going through /process (files are shown in the UI
    # but never put in the server-side session cache).
    _combined = context.get("combined_extractions", {})
    if not _combined and workspace_id:
        logger.info(
            "[AgentController] WS-01: context is empty but workspace_id=%s — hydrating.",
            workspace_id[:8],
        )
        hydrated = await _hydrate_context_from_workspace(workspace_id, _session_id)
        if hydrated:
            context = {
                **context,
                "combined_extractions": hydrated.get("combined_extractions", {}),
                "files_processed": hydrated.get("files_processed", 0),
            }
            logger.info(
                "[AgentController] WS-01: context hydrated with %d file(s).",
                context.get("files_processed", 0),
            )

    # ARCH-01: use cached orchestrator instead of creating a new one per request
    # P1-01: max_rounds is resolved here and passed into run(), NOT mutated on
    # the shared cached instance, eliminating the concurrent-request data race.
    orchestrator = _get_orchestrator(model, coder_model, temperature)

    # Persist new run row — non-blocking
    await _try_create_run(run_id, _session_id, query, context, workspace_id, user_id)

    # Emit run_id to the frontend immediately
    yield f"data: {json.dumps({'event': 'run_started', 'payload': {'run_id': run_id}})}\n\n"

    # BUG 4 fix: Emit any parse_warnings accumulated during /process as SSE
    # warning events *before* the orchestrator loop starts.  This gives the
    # user visibility into near-empty files (blank PDFs, image-only scans)
    # at the earliest possible moment — before any LLM call is made.
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
            max_rounds=_max_rounds,
            workspace_id=workspace_id,
        ):
            # PERF-03: O(1) check — no await, no syscall per event
            if disc_event.is_set():
                logger.info(
                    "[AgentController] Client disconnected — aborting run_id=%s", run_id
                )
                await _try_update_run(run_id, {}, status="failed")
                return

            payload = json.dumps(event, default=str)
            yield f"data: {payload}\n\n"

            event_type = event.get("event")
            event_payload = event.get("payload", {})

            if event_type == "completed":
                await _try_update_run(run_id, event_payload, status="completed")
            elif event_type == "error":
                await _try_update_run(run_id, event_payload, status="failed")

    except asyncio.CancelledError:
        # P2-01 fix: CancelledError inherits from BaseException, not Exception.
        # FastAPI cancels the streaming task on client disconnect — we must
        # catch it explicitly to mark the run as failed before re-raising.
        logger.info(
            "[AgentController] Stream cancelled (client disconnect?) — run_id=%s", run_id
        )
        await _try_update_run(run_id, {}, status="failed")
        raise  # re-raise so FastAPI can clean up the response properly

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
        # File cache is retained across runs — users can run multiple queries on the
        # same uploaded data without re-uploading. Cleanup is handled by:
        #   • Explicit DELETE /api/clear (user action via handleClearAll)
        #   • TTL eviction (_evict_stale_sessions every 60 s, ARCH-02)
        #   • MAX_SESSIONS cap eviction
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
