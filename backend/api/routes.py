"""API router — thin endpoint declarations.

Each route delegates immediately to its matching controller.
No business logic lives here.

Gap fixes applied:
- _session_contexts is now session-keyed (Dict[session_id, context]) to
  prevent multi-tenancy corruptions where two concurrent users overwrite each other.
- /process now merges files into an existing session context instead of overwriting.
- /upload now accepts a session_id query param so files are scoped per session.
- /clear now passes session_id to the file cache so only one session is wiped.
- /agent/run now receives the FastAPI Request object so handle_agent_run can detect
  client disconnection and stop the server-side loop (Gap 5 fix).

ARCH-02 fix:
- _session_contexts is bounded by TTL eviction (SESSION_TTL_SECONDS, default 3600 s)
  and a MAX_SESSIONS size cap (default 500). Both are env-configurable via config.py.
  Eviction runs lazily on every session write to avoid background threads.
- _set_session() is the single write path; it calls _evict_stale_sessions() first.

ARCH-03 fix:
- /agent/runs and /agent/runs/{run_id} now await the async Supabase service functions.

AUTH fix:
- Protected endpoints now require a valid Supabase JWT via Depends(get_current_user).
- Optional auth (get_optional_user) is used where anonymous access is still valid.
- user_id from the authenticated token is forwarded to service layers.
"""

import asyncio
import time as _time
import io
import zipfile
import re
from typing import Any, Dict, List, Optional
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel

from api.controllers.agent_controller import handle_agent_run
from api.controllers.process_controller import handle_clear, handle_process
from api.controllers.upload_controller import handle_upload, handle_workspace_upload, handle_delete_workspace_file
from api.controllers.research_controller import handle_research_run
from core.deep_research_orchestrator import is_open_ended
from core.config import SESSION_TTL_SECONDS, MAX_SESSIONS
from middleware.auth import AuthUser, get_current_user
from models.schemas import AgentRunRequest, UploadResponse
from services.upload_service import clear_file_cache

logger = logging.getLogger("uvicorn.error")

router = APIRouter()

# ---------------------------------------------------------------------------
# ARCH-02: Session context store with TTL eviction and size cap
# ---------------------------------------------------------------------------

# Primary store: session_id → context dict
_session_contexts: Dict[str, Dict[str, Any]] = {}
# Companion timestamps: session_id → monotonic time of last write
_session_timestamps: Dict[str, float] = {}




def _evict_stale_sessions() -> None:
    """Removes sessions older than SESSION_TTL_SECONDS and enforces MAX_SESSIONS cap.

    Called lazily on every session write to avoid background threads.
    """
    now = _time.monotonic()

    stale = [
        k for k, ts in _session_timestamps.items()
        if now - ts > SESSION_TTL_SECONDS
    ]
    for k in stale:
        _session_contexts.pop(k, None)
        _session_timestamps.pop(k, None)
        clear_file_cache(k)  # free uploaded file bytes for this session
    if stale:
        logger.info("[Router] Evicted %d stale session(s) (TTL=%ds)", len(stale), SESSION_TTL_SECONDS)

    while len(_session_contexts) >= MAX_SESSIONS:
        oldest_key = min(_session_timestamps, key=lambda k: _session_timestamps[k])
        _session_contexts.pop(oldest_key, None)
        _session_timestamps.pop(oldest_key, None)
        clear_file_cache(oldest_key)  # free uploaded file bytes for evicted session
        logger.warning(
            "[Router] Session cap (%d) reached — evicted oldest session: %s",
            MAX_SESSIONS, oldest_key,
        )


def _set_session(key: str, data: Dict[str, Any]) -> None:
    """Writes ``data`` into the session store with eviction and timestamp tracking.

    Args:
        key: Session identifier.
        data: Context dict to store.
    """
    _evict_stale_sessions()
    _session_contexts[key] = data
    _session_timestamps[key] = _time.monotonic()


# B2 fix: @router.on_event("startup") silently no-ops on APIRouter — the
# eviction loop is registered on the FastAPI app instance in main.py instead.
# See: _start_session_eviction_loop() in main.py.


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ProcessRequest(BaseModel):
    """Request body for the /process endpoint."""

    files: List[str]
    session_id: Optional[str] = None


class CreateWorkspaceRequest(BaseModel):
    """Request body for the POST /workspaces endpoint.

    P2-05 fix: replaced raw Dict[str, Any] with a typed Pydantic model so
    OpenAPI clients get a proper schema and FastAPI returns 422 (not 500)
    when required fields are missing.
    """

    name: str
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Upload / Process / Query
# ---------------------------------------------------------------------------

@router.post("/upload", response_model=UploadResponse)
async def upload_files(
    files: List[UploadFile] = File(...),
    session_id: Optional[str] = Query(default=None),
    workspace_id: Optional[str] = Query(default=None),
    auth: AuthUser = Depends(get_current_user),
) -> UploadResponse:
    """Validates and persists uploaded files into the session-scoped cache.

    Accepts only authenticated uploads; the user_id is available for downstream scoping.
    """
    user_id = auth.user_id
    file_names = [f.filename for f in files]
    logger.info(
        "[Upload] user=%s session=%s workspace=%s files=%s (%d)",
        str(user_id)[:8],
        (session_id or str(user_id))[:8],
        (workspace_id or "—")[:8],
        ", ".join(file_names),
        len(file_names),
    )
    return await handle_upload(files, session_id=session_id or str(user_id), user_id=user_id, workspace_id=workspace_id)


@router.get("/files")
async def list_files(
    workspace_id: Optional[str] = Query(default=None),
    auth: AuthUser = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Lists files uploaded by the current user, optionally scoped to a workspace.

    P2-04 fix: passes user_id so the Supabase query filters by the
    authenticated user, preventing cross-user file visibility.
    """
    try:
        from services.supabase_service import list_uploaded_files  # pylint: disable=import-outside-toplevel
        return await list_uploaded_files(user_id=auth.user_id, workspace_id=workspace_id)
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/process")
async def process_batch(
    request: ProcessRequest,
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Processes cached files into normalised in-memory context.

    Merges results into the session's existing context rather than replacing
    it wholesale, so multiple /process calls accumulate files correctly.

    BUG 4 fix: Includes ``parse_warnings`` in the response so the frontend
    can alert the user to near-empty files before they start an agent run.
    """
    session_key = request.session_id or str(auth.user_id)
    logger.info(
        "[Process] user=%s session=%s files=%d",
        str(auth.user_id)[:8],
        session_key[:8],
        len(request.files),
    )
    # B7 fix: handle_process is a blocking sync function (CSV/PDF parsing).
    # Run it in a thread-pool executor so it doesn't stall the event loop
    # and block SSE heartbeats from other concurrent requests.
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, handle_process, request.files, session_key)

    existing = _session_contexts.get(session_key, {})
    new_details = result.get("details", {})

    merged_extractions = {
        **existing.get("combined_extractions", {}),
        **new_details.get("combined_extractions", {}),
    }

    # BUG 4: accumulate parse_warnings across multiple /process calls
    existing_warnings = existing.get("parse_warnings", [])
    new_warnings = new_details.get("parse_warnings", [])
    merged_warnings = existing_warnings + new_warnings

    # Log each warning so server-side monitoring catches sparse files
    for pw in new_warnings:
        logger.warning(
            "[Process] ParseWarning for '%s': %s",
            pw.get("filename", "?"),
            pw.get("message", ""),
        )

    merged = {
        **new_details,
        "combined_extractions": merged_extractions,
        "files_processed": len(merged_extractions),
        "parse_warnings": merged_warnings,
    }
    _set_session(session_key, merged)

    # Surface parse_warnings in the HTTP response so the React frontend
    # can show them immediately (before any agent run is started).
    result["parse_warnings"] = new_warnings
    return result



# ---------------------------------------------------------------------------
# Agent run — SSE streaming
# ---------------------------------------------------------------------------

@router.post("/agent/run")
async def agent_run(
    request: AgentRunRequest,
    http_request: Request,
    auth: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    """Streams DS-STAR or DS-STAR+ agent events as Server-Sent Events.

    Automatically routes open-ended queries to the DS-STAR+ deep research loop.
    Authenticated requests carry user_id for run-level scoping in Supabase.
    """
    session_key = request.session_id or str(auth.user_id)
    context = _session_contexts.get(session_key, {})
    user_id = auth.user_id

    if is_open_ended(request.query):
        logger.info(
            "[AgentRun] user=%s session=%s mode=DS-STAR+ query=%.60r",
            str(user_id)[:8], session_key[:8], request.query,
        )
        stream_handler = handle_research_run(
            query=request.query,
            context=context,
            session_id=session_key,
            max_rounds=request.max_rounds,
            model=request.model,
            coder_model=request.coder_model,
            temperature=request.temperature,
            user_id=user_id,
            workspace_id=request.workspace_id,
            http_request=http_request,  # P2-02: enables disconnect monitoring
        )
    else:
        logger.info(
            "[AgentRun] user=%s session=%s mode=DS-STAR query=%.60r",
            str(user_id)[:8], session_key[:8], request.query,
        )
        stream_handler = handle_agent_run(
            query=request.query,
            context=context,
            session_id=session_key,
            max_rounds=request.max_rounds,
            model=request.model,
            coder_model=request.coder_model,
            temperature=request.temperature,
            http_request=http_request,
            user_id=user_id,
            workspace_id=request.workspace_id,
        )

    return StreamingResponse(
        stream_handler,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# History endpoints
# ---------------------------------------------------------------------------

@router.get("/agent/runs")
async def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    workspace_id: Optional[str] = Query(default=None),
    auth: AuthUser = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Lists past agent runs from Supabase, scoped to the authenticated user and workspace."""
    try:
        from services.supabase_service import list_agent_runs  # pylint: disable=import-outside-toplevel
        user_id = auth.user_id
        logger.info(
            "[Runs] user=%s workspace=%s limit=%d",
            str(user_id)[:8],
            (workspace_id or "—")[:8],
            limit,
        )
        return await list_agent_runs(limit=limit, user_id=user_id, workspace_id=workspace_id)
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/agent/runs/{run_id}")
async def get_run(
    run_id: str,
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Retrieves a single agent run by its ID."""
    try:
        from services.supabase_service import get_agent_run  # pylint: disable=import-outside-toplevel
        run = await get_agent_run(run_id)
        if not run:
            raise HTTPException(
                status_code=404, detail=f"Run '{run_id}' not found."
            )
        if run.get("user_id") and run["user_id"] != auth.user_id:
            raise HTTPException(
                status_code=403, detail="Not authorized to view this run."
            )
        return run
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/agent/runs/{run_id}/download")
async def download_run(
    run_id: str,
    auth: AuthUser = Depends(get_current_user),
) -> Response:
    """Downloads a ZIP archive containing the code, results, and generated graphs for a specific run."""
    try:
        from services.supabase_service import get_agent_run  # pylint: disable=import-outside-toplevel
        run = await get_agent_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        
        if run.get("user_id") and run["user_id"] != auth.user_id:
            raise HTTPException(status_code=403, detail="Not authorized to download this run.")
            
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            final_code = run.get("final_code") or ""
            if final_code:
                zip_file.writestr("code.py", final_code)
                
            md_content = f"# Run Results: {run_id}\n\n"

            query = (run.get("query") or "").strip()
            if query:
                md_content += f"## Query\n{query}\n\n"

            insights = run.get("insights") or {}
            if isinstance(insights, dict) and insights:
                md_content += "## Insights\n"
                summary = (insights.get("summary") or "").strip()
                bullets = insights.get("bullets") or []
                if summary:
                    md_content += f"{summary}\n\n"
                if isinstance(bullets, list) and bullets:
                    for bullet in bullets:
                        text = str(bullet).strip()
                        if text:
                            md_content += f"- {text}\n"
                    md_content += "\n"
            elif isinstance(insights, list) and insights:
                # Backward compatibility for older run shapes.
                md_content += "## Insights\n"
                for insight in insights:
                    text = str(insight).strip()
                    if text:
                        md_content += f"- {text}\n"
                md_content += "\n"
                
            plan_steps = run.get("plan_steps") or []
            if plan_steps:
                md_content += "## Plan Steps\n"
                for step in plan_steps:
                    if isinstance(step, dict):
                        idx = step.get("index")
                        desc = step.get("description", "")
                        status = step.get("status", "pending")
                        if isinstance(idx, int):
                            md_content += f"- Step {idx + 1} [{status}]: {desc}\n"
                        else:
                            md_content += f"- [{status}]: {desc}\n"
                    else:
                        md_content += f"- {step}\n"
                md_content += "\n"
                
            execution_logs = run.get("execution_logs") or []
            if execution_logs:
                md_content += "## Execution Logs\n"
                for log in execution_logs:
                    md_content += f"{log}\n"
                md_content += "\n"
                
            zip_file.writestr("results.md", md_content)
            
            # B1 fix: "\n".join(dict) only iterates over keys, not values.
            # Flatten insights into searchable text regardless of its shape.
            if isinstance(insights, dict):
                _parts: List[str] = []
                if insights.get("summary"):
                    _parts.append(str(insights["summary"]))
                for b in (insights.get("bullets") or []):
                    _parts.append(str(b))
                insights_text = "\n".join(_parts)
            elif isinstance(insights, list):
                insights_text = "\n".join(str(i) for i in insights)
            else:
                insights_text = ""
            all_text = "\n".join(execution_logs) + "\n" + insights_text
            base64_pattern = re.compile(r"data:image/(png|jpeg|jpg);base64,([A-Za-z0-9+/=]+)")
            matches = base64_pattern.findall(all_text)
            
            import base64
            for i, match in enumerate(matches):
                ext, b64_data = match
                try:
                    image_data = base64.b64decode(b64_data)
                    zip_file.writestr(f"graph_{i+1}.{ext}", image_data)
                except Exception:
                    pass
            
        zip_buffer.seek(0)
        
        headers = {
            "Content-Disposition": f'attachment; filename="agentloop-run-{run_id}.zip"'
        }
        return Response(content=zip_buffer.getvalue(), media_type="application/zip", headers=headers)
        
    except HTTPException:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Workspace endpoints
# ---------------------------------------------------------------------------

@router.get("/workspaces")
async def list_workspaces(
    auth: AuthUser = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Lists all workspaces owned by the authenticated user."""
    try:
        from services.supabase_service import list_workspaces as _list  # pylint: disable=import-outside-toplevel
        return await _list(user_id=auth.user_id)
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/workspaces/stats")
async def workspace_stats(
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Returns per-project run statistics (run_count, last_run_at) keyed by workspace_id."""
    try:
        from services.supabase_service import get_workspace_stats  # pylint: disable=import-outside-toplevel
        return await get_workspace_stats(user_id=auth.user_id)
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/workspaces")
async def create_workspace(
    payload: CreateWorkspaceRequest,
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Creates a new workspace for the authenticated user.

    P2-05 fix: accepts a typed Pydantic body instead of raw Dict; field
    validation (name required, max length) is handled by the model.
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Workspace name cannot be blank.")
    try:
        from services.supabase_service import create_workspace as _create  # pylint: disable=import-outside-toplevel
        return await _create(user_id=auth.user_id, name=name)
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/upload")
async def workspace_upload_files(
    workspace_id: str,
    files: List[UploadFile] = File(...),
    session_id: Optional[str] = Query(default=None),
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Uploads one or more files into a workspace session.

    Saves each file to disk at /workspace/{workspace_id}/{filename},
    extracts schema_json and row_count via the appropriate parser,
    inserts a workspace_files row, then returns a MultiFileContext
    with all current workspace files and inferred join candidates.

    Args:
        workspace_id: Target workspace UUID (from path).
        files: Multipart file upload(s).
        session_id: Optional session identifier for in-memory cache scoping.
        auth: Authenticated user (from JWT).

    Returns:
        JSON with accepted_files, rejected_files, and multi_file_context.
    """
    effective_session = session_id or str(auth.user_id)
    file_names = [f.filename for f in files]
    logger.info(
        "[WorkspaceUpload] workspace=%s user=%s files=%s (%d)",
        workspace_id[:8],
        str(auth.user_id)[:8],
        ", ".join(file_names),
        len(file_names),
    )
    try:
        return await handle_workspace_upload(
            files=files,
            workspace_id=workspace_id,
            user_id=str(auth.user_id),
            session_id=effective_session,
        )
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/workspaces/{workspace_id}/files/{file_id}")
async def delete_workspace_file(
    workspace_id: str,
    file_id: str,
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Removes a single file from a workspace and returns the refreshed MultiFileContext.

    Deletes the workspace_files row (verified to belong to the authenticated user),
    removes the physical file from disk (best-effort), then re-runs schema_merger
    on the remaining files to produce an updated MultiFileContext.

    Args:
        workspace_id: Workspace UUID (from path).
        file_id: UUID of the workspace_files row to delete.
        auth: Authenticated user (from JWT).

    Returns:
        JSON with deleted (bool) and multi_file_context.
    """
    logger.info(
        "[WorkspaceFileDelete] workspace=%s file=%s user=%s",
        workspace_id[:8],
        file_id[:8],
        str(auth.user_id)[:8],
    )
    try:
        return await handle_delete_workspace_file(
            file_id=file_id,
            workspace_id=workspace_id,
            user_id=str(auth.user_id),
        )
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve)) from ve
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/workspaces/{workspace_id}/files")
async def list_workspace_files_endpoint(
    workspace_id: str,
    auth: AuthUser = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """Lists files belonging to a workspace, formatted for the frontend upload panel.

    Returns workspace_files rows with keys the frontend expects:
    id, filename, file_size, file_url, file_type, schema_json, row_count.

    Args:
        workspace_id: Workspace UUID (from path).
        auth: Authenticated user (from JWT).

    Returns:
        List of file metadata dicts.
    """
    try:
        from services.supabase_service import list_workspace_files  # pylint: disable=import-outside-toplevel
        rows = await list_workspace_files(
            workspace_id=workspace_id,
            user_id=str(auth.user_id),
        )
        # Reshape for frontend compatibility
        return [
            {
                "id": r.get("id"),
                "filename": r.get("file_name", ""),
                "file_size": r.get("file_size") or 0,
                "file_url": r.get("file_url") or "",
                "file_type": r.get("file_type", ""),
                "schema_json": r.get("schema_json"),
                "row_count": r.get("row_count"),
            }
            for r in rows
        ]
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

@router.delete("/clear")
async def clear_cache(
    session_id: Optional[str] = Query(default=None),
    auth: AuthUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Wipes the internal session processing context and byte caches."""
    session_key = session_id or str(auth.user_id)
    _session_contexts.pop(session_key, None)
    _session_timestamps.pop(session_key, None)
    return handle_clear(session_id=session_key)
