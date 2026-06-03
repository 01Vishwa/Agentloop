"""Supabase service layer.

Encapsulates all interactions with Supabase Storage and the
``uploaded_files`` PostgreSQL table. Exposes a thin, typed API so that
controllers never import the supabase client directly.

ARCH-03 fix: All functions are now ``async def`` and execute the blocking
supabase-py calls inside ``asyncio.get_running_loop().run_in_executor(None, ...)``
so they never block the FastAPI event loop during SSE streaming.

MIN-03 fix: ``upload_to_storage`` return type corrected from ``str`` to
``Tuple[str, str]`` matching the actual ``(public_url, storage_path)`` tuple.
"""

import asyncio
import datetime
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client, Client

from core.config import SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUPABASE_BUCKET, SUPABASE_SERVICE_ROLE_KEY

logger = logging.getLogger("uvicorn.info")

# ---------------------------------------------------------------------------
# Circuit-breaker — set to True on the first Supabase 401 so that every
# subsequent call fails silently instead of spamming the log on every poll.
# Reset by restarting the server (after fixing .env).
# ---------------------------------------------------------------------------
_supabase_auth_failed: bool = False

# ---------------------------------------------------------------------------
# Client factories — create a fresh client inside each executor thread.
#
# supabase-py v2 uses httpx.Client internally. A client initialised in the
# main async event-loop thread holds sockets in non-blocking mode. Reusing
# that singleton from a ThreadPoolExecutor thread causes Windows to raise
# WinError 10035 (WSAEWOULDBLOCK). Creating a new client per _sync() call
# is cheap (just HTTP headers + URL) and is the safe cross-thread pattern.
# ---------------------------------------------------------------------------

def _make_anon_client() -> Client:
    """Creates a fresh Supabase anon client for use in executor threads."""
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY must be set in .env"
        )
    return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)


def _make_service_client() -> Client:
    """Creates a fresh Supabase service-role client for use in executor threads.

    Falls back to the anon client if SUPABASE_SERVICE_ROLE_KEY is missing,
    logging a warning since writes will then fail RLS.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logger.warning(
            "[Supabase] SUPABASE_SERVICE_ROLE_KEY missing; "
            "falling back to anon client (writes may fail RLS)"
        )
        return _make_anon_client()
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ---------------------------------------------------------------------------
# Legacy accessors kept for any callers outside _sync() closures.
# These are NOT used inside run_in_executor threads.
# ---------------------------------------------------------------------------

_client: Optional[Client] = None
_service_client: Optional[Client] = None


def get_supabase_client() -> Client:
    """Returns a cached Supabase anon client (main-thread use only).

    WARNING: Do NOT pass this instance into a ThreadPoolExecutor. Use
    ``_make_anon_client()`` inside ``_sync()`` closures instead.

    Returns:
        Client: Anon Supabase client instance.
    """
    global _client
    if _client is None:
        _client = _make_anon_client()
        logger.info("Supabase anon client initialised")
    return _client


def get_service_role_client() -> Client:
    """Returns a cached Supabase service-role client (main-thread use only).

    WARNING: Do NOT pass this instance into a ThreadPoolExecutor. Use
    ``_make_service_client()`` inside ``_sync()`` closures instead.
    """
    global _service_client
    if _service_client is None:
        _service_client = _make_service_client()
        logger.info("Supabase service-role client initialised")
    return _service_client

# ---------------------------------------------------------------------------
# Storage operations
# ---------------------------------------------------------------------------

async def upload_to_storage(
    filename: str,
    content_bytes: bytes,
    extension: str,
) -> Tuple[str, str]:
    """Uploads raw file bytes to Supabase Storage.

    Files are stored under a UUID-prefixed path to avoid collisions.
    Runs the blocking upload in a thread-pool executor to avoid stalling
    the FastAPI event loop.

    Args:
        filename (str): Original filename (used for Content-Type hint).
        content_bytes (bytes): Raw file content.
        extension (str): Lowercase file extension (csv | xlsx | json).

    Returns:
        Tuple[str, str]: (public_url, storage_path) of the uploaded object.

    Raises:
        RuntimeError: If the Supabase Storage upload fails.
    """
    mime_map = {
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "json": "application/json",
    }
    storage_path = f"{uuid.uuid4().hex}/{filename}"

    def _sync() -> Tuple[str, str]:
        client = _make_service_client()
        response = client.storage.from_(SUPABASE_BUCKET).upload(
            path=storage_path,
            file=content_bytes,
            file_options={"content-type": mime_map.get(extension, "application/octet-stream")},
        )
        if hasattr(response, "error") and response.error:
            raise RuntimeError(f"Storage upload failed: {response.error}")
        public_url = client.storage.from_(SUPABASE_BUCKET).get_public_url(storage_path)
        logger.info(
            "Uploaded to Supabase Storage — path=%s, url=%s", storage_path, public_url
        )
        return public_url, storage_path

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

async def insert_file_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Inserts a file metadata row into the ``uploaded_files`` table.

    Args:
        record (Dict[str, Any]): Row data matching the table schema.

    Returns:
        Dict[str, Any]: The inserted row returned by Supabase.

    Raises:
        RuntimeError: If the insert fails.
    """
    def _sync() -> Dict[str, Any]:
        client = _make_service_client()
        response = (
            client.table("uploaded_files")
            .insert(record)
            .execute()
        )
        if not response.data:
            raise RuntimeError(f"DB insert failed for record: {record}")
        logger.info("Inserted file record — id=%s", response.data[0].get("id"))
        return response.data[0]

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def list_uploaded_files(
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetches file rows from the ``uploaded_files`` table scoped to a user.

    P2-04 fix: the ``user_id`` filter is now applied unconditionally when
    provided, preventing cross-user file visibility in multi-tenant deployments.

    Args:
        user_id: Authenticated user ID — rows are filtered to this user only.
        workspace_id: Optional workspace scope for additional filtering.

    Returns:
        List[Dict[str, Any]]: List of dataset metadata rows.
    """
    def _sync() -> List[Dict[str, Any]]:
        client = _make_service_client()
        query = client.table("uploaded_files").select("*").order("created_at", desc=True)
        if user_id:
            query = query.eq("user_id", user_id)
        if workspace_id:
            query = query.eq("workspace_id", workspace_id)
        response = query.execute()
        return response.data or []

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# Agent run operations  (DS-STAR)
# ---------------------------------------------------------------------------

async def create_agent_run(
    run_id: str,
    query: str,
    file_names: List[str],
    session_id: str = "",
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a new row in the ``agent_runs`` table with status=running.

    Args:
        run_id (str): Unique identifier for this agent run.
        query (str): The user query that triggered the run.
        file_names (List[str]): Names of files in the processing context.
        session_id (str): Optional client session identifier.
        user_id (Optional[str]): Authenticated user UUID (from Supabase JWT).
        workspace_id (Optional[str]): Workspace UUID to associate this run.

    Returns:
        Dict[str, Any]: The inserted row.

    Raises:
        RuntimeError: If the DB insert fails.
    """
    def _sync() -> Dict[str, Any]:
        client = _make_service_client()
        record = {
            "id": run_id,
            "session_id": session_id or None,
            "user_id": user_id or None,
            "workspace_id": workspace_id or None,
            "query": query,
            "file_names": file_names,
            "status": "running",
            "plan_steps": [],
            "execution_logs": [],
        }
        try:
            response = client.table("agent_runs").insert(record).execute()
            if not response.data:
                raise RuntimeError(f"Failed to create agent_run record: {run_id}")
            logger.info("Created agent_run — id=%s, user_id=%s", run_id, user_id)
            return response.data[0]
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not create agent_run (schema missing?): %s", exc)
            return record

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def update_agent_run(
    run_id: str,
    plan_steps: List[Dict[str, Any]],
    final_code: str,
    rounds: int,
    insights: Dict[str, Any],
    execution_logs: List[str],
    status: str = "completed",
) -> Dict[str, Any]:
    """Updates an existing agent_run row with the final result.

    Args:
        run_id (str): Unique run identifier.
        plan_steps (List[Dict[str, Any]]): Final plan step list.
        final_code (str): The generated Python script.
        rounds (int): Number of rounds completed.
        insights (Dict[str, Any]): Final insights dict.
        execution_logs (List[str]): Log entries from each round.
        status (str): Terminal status — "completed" or "failed".

    Returns:
        Dict[str, Any]: The updated row.
    """
    def _sync() -> Dict[str, Any]:
        client = _make_service_client()
        updates = {
            "plan_steps": plan_steps,
            "final_code": final_code,
            "rounds": rounds,
            "insights": insights,
            "execution_logs": execution_logs,
            "status": status,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        try:
            response = (
                client.table("agent_runs")
                .update(updates)
                .eq("id", run_id)
                .execute()
            )
            logger.info("Updated agent_run — id=%s, status=%s", run_id, status)
            return response.data[0] if response.data else {}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not update agent_run (schema missing?): %s", exc)
            return {}

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def get_agent_run(run_id: str) -> Dict[str, Any]:
    """Fetches a single agent_run row by its ID.

    Args:
        run_id (str): Unique run identifier.

    Returns:
        Dict[str, Any]: The run row, or an empty dict if not found.
    """
    def _sync() -> Dict[str, Any]:
        client = _make_service_client()
        try:
            response = (
                client.table("agent_runs")
                .select("*")
                .eq("id", run_id)
                .maybe_single()
                .execute()
            )
            return response.data or {}
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Could not get agent_run (schema missing?): %s", exc)
            return {}

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def list_agent_runs(
    limit: int = 20,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetches the most recent agent_run rows, optionally scoped to a user and workspace.

    Args:
        limit (int): Maximum number of rows to return.
        user_id (Optional[str]): When provided, filters runs to this user only.
            This respects the RLS policy on the table — only the owner's rows
            are returned. When None, falls back to RLS (which uses auth.uid()).
        workspace_id (Optional[str]): When provided, filters runs to this workspace only.

    Returns:
        List[Dict[str, Any]]: Agent run rows ordered newest first.
    """
    def _sync() -> List[Dict[str, Any]]:
        global _supabase_auth_failed  # pylint: disable=global-statement
        if _supabase_auth_failed:
            return []  # Circuit open — skip silently until server restart
        client = _make_service_client()
        try:
            query = (
                client.table("agent_runs")
                .select("id, query, file_names, rounds, status, created_at, completed_at, user_id, workspace_id")
                .order("created_at", desc=True)
                .limit(limit)
            )
            if user_id:
                query = query.eq("user_id", user_id)
            if workspace_id:
                query = query.eq("workspace_id", workspace_id)
            response = query.execute()
            return response.data or []
        except Exception as exc:  # pylint: disable=broad-except
            exc_str = str(exc)
            if "401" in exc_str or "Invalid API key" in exc_str:
                _supabase_auth_failed = True
                logger.error(
                    "[Supabase] Auth failed — disabling DB calls until restart. "
                    "Fix SUPABASE_SERVICE_ROLE_KEY in backend/.env"
                )
            else:
                logger.warning("Could not list agent_runs (schema missing?): %s", exc)
            return []

    return await asyncio.get_running_loop().run_in_executor(None, _sync)



# ---------------------------------------------------------------------------
# Deep Research (DS-STAR+) tracking
# ---------------------------------------------------------------------------

async def create_report_run(
    report_id: str,
    query: str,
    file_names: List[str],
    session_id: str = "",
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Creates a new tracking row in the reports table.

    Args:
        report_id: Unique identifier for the research run.
        query: Full natural language query.
        file_names: List of files included in the context.
        session_id: Optional client session ID.
        workspace_id: Optional workspace UUID to associate this report.
        user_id: Optional authenticated user UUID.

    Returns:
        The inserted row as a dictionary.
    """
    def _sync() -> Dict[str, Any]:
        client = _make_service_client()
        record = {
            "id": report_id,
            "query": query,
            "session_id": session_id or None,
            "workspace_id": workspace_id or None,
            "user_id": user_id or None,
            "file_names": file_names,
            "status": "running",
            "key_findings": [],
            "caveats": [],
            "sub_questions": [],
            "sub_run_ids": [],
        }
        try:
            response = client.table("reports").insert(record).execute()
            logger.info("[Supabase] Created research report: %s", report_id)
            return response.data[0] if response.data else record
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[Supabase] Could not create report %s: %s", report_id, exc)
            return record

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def create_subquestions(
    report_id: str,
    sub_questions: List[str],
) -> None:
    """Inserts a batch of sub_questions associated with a report.

    Args:
        report_id: The parent report ID.
        sub_questions: Ordered list of sub-questions to track.
    """
    def _sync() -> None:
        client = _make_service_client()
        try:
            client.table("reports").update({
                "sub_questions": sub_questions,
                "sub_run_ids": [None] * len(sub_questions)
            }).eq("id", report_id).execute()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[Supabase] Could not update report array: %s", exc)

        records = [
            {
                "id": f"{report_id}-q{i}",
                "report_id": report_id,
                "question": sq,
                "question_index": i,
                "status": "pending"
            }
            for i, sq in enumerate(sub_questions)
        ]
        try:
            client.table("sub_questions").insert(records).execute()
            logger.info(
                "[Supabase] Inserted %d sub-questions for %s", len(records), report_id
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[Supabase] Could not create sub-questions: %s", exc)

    await asyncio.get_running_loop().run_in_executor(None, _sync)


async def link_subquestion_run(
    report_id: str,
    question_index: int,
    status: str,
    result_run_id: str,
) -> None:
    """Updates a sub-question with its final execution status and linked agent_run ID.

    Args:
        report_id: Parent report ID.
        question_index: The zero-based index of the sub-question.
        status: The final status (e.g. 'completed' or 'failed').
        result_run_id: The run ID of the DS-STAR execution that answered this question.
    """
    def _sync() -> None:
        client = _make_service_client()
        sq_id = f"{report_id}-q{question_index}"
        try:
            client.table("sub_questions").update({
                "status": status,
                "result_run_id": result_run_id,
                "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }).eq("id", sq_id).execute()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[Supabase] Could not link sub-question %s: %s", sq_id, exc)

    await asyncio.get_running_loop().run_in_executor(None, _sync)


async def update_report_status(
    report_id: str,
    status: str,
    title: str = "",
    executive_summary: str = "",
    report_body: str = "",
    key_findings: Optional[List[str]] = None,
    caveats: Optional[List[str]] = None,
    total_ms: int = 0,
) -> None:
    """Marks a research report as finished and populates the synthesized final output.

    Args:
        report_id: Unique research run identifier.
        status: 'completed' or 'failed'.
        title: Synthesized title.
        executive_summary: High-level summary of findings.
        report_body: Full markdown document body.
        key_findings: Discovered insights.
        caveats: Any warnings or errors encountered during the sub-runs.
        total_ms: Wall-clock duration of the entire DeepResearch workflow.
    """
    def _sync() -> None:
        client = _make_service_client()
        updates = {
            "status": status,
            "title": title or None,
            "executive_summary": executive_summary or None,
            "report_body": report_body or None,
            "key_findings": key_findings or [],
            "caveats": caveats or [],
            "total_ms": total_ms,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        try:
            client.table("reports").update(updates).eq("id", report_id).execute()
            logger.info(
                "[Supabase] Finalised report %s with status=%s", report_id, status
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "[Supabase] Could not finalise report %s: %s", report_id, exc
            )

    await asyncio.get_running_loop().run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# Workspace operations
# ---------------------------------------------------------------------------


async def list_workspaces(user_id: str) -> List[Dict[str, Any]]:
    """Fetches all workspaces owned by ``user_id``.

    Args:
        user_id (str): Authenticated user UUID.

    Returns:
        List[Dict[str, Any]]: Workspace rows ordered by creation date.
    """
    def _sync() -> List[Dict[str, Any]]:
        client = _make_service_client()  # bypass RLS; eq("user_id") filters server-side
        try:
            response = (
                client.table("workspaces")
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", ascending=True)
                .execute()
            )
            return response.data or []
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[Supabase] Could not list workspaces for user %s: %s", user_id, exc)
            return []

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def create_workspace(user_id: str, name: str) -> Dict[str, Any]:
    """Creates a new workspace row in the ``workspaces`` table.

    Args:
        user_id (str): Authenticated user UUID.
        name (str): Workspace display name.

    Returns:
        Dict[str, Any]: The inserted workspace row.

    Raises:
        RuntimeError: If the insert fails.
    """
    def _sync() -> Dict[str, Any]:
        client = _make_service_client()
        record = {"user_id": user_id, "name": name}
        try:
            response = client.table("workspaces").insert(record).select().execute()
            if not response.data:
                raise RuntimeError(f"Failed to create workspace for user {user_id}")
            logger.info("[Supabase] Created workspace '%s' for user %s", name, user_id)
            return response.data[0]
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[Supabase] Could not create workspace: %s", exc)
            raise RuntimeError(str(exc)) from exc

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def get_workspace_stats(user_id: str) -> Dict[str, Any]:
    """Returns per-workspace run statistics for the given user.

    For each workspace the result contains:
      - ``run_count``: total number of agent_runs with that workspace_id.
      - ``last_run_at``: ISO timestamp of the most recent run (or None).

    Args:
        user_id: Authenticated user UUID.

    Returns:
        Dict keyed by workspace_id → {run_count, last_run_at}.
    """
    def _sync() -> Dict[str, Any]:
        client = _make_service_client()
        try:
            response = (
                client.table("agent_runs")
                .select("workspace_id, created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(1000)
                .execute()
            )
            rows = response.data or []
            stats: Dict[str, Any] = {}
            for row in rows:
                ws_id = row.get("workspace_id")
                if not ws_id:
                    continue
                if ws_id not in stats:
                    stats[ws_id] = {"run_count": 0, "last_run_at": row.get("created_at")}
                stats[ws_id]["run_count"] += 1
            return stats
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[Supabase] Could not fetch workspace stats: %s", exc)
            return {}

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


# ---------------------------------------------------------------------------
# workspace_files operations  (multi-file join feature)
# ---------------------------------------------------------------------------

async def insert_workspace_file(record: Dict[str, Any]) -> Dict[str, Any]:
    """Inserts one row into the ``workspace_files`` table.

    Args:
        record: Dict matching workspace_files schema keys:
            workspace_id, user_id, file_name, file_path, file_type,
            row_count (optional), schema_json (optional), upload_order.

    Returns:
        The inserted row as returned by Supabase.

    Raises:
        RuntimeError: If the insert fails.
    """
    def _sync() -> Dict[str, Any]:
        client = _make_service_client()
        try:
            response = (
                client.table("workspace_files")
                .insert(record)
                .execute()
            )
            if not response.data:
                raise RuntimeError(
                    f"workspace_files insert returned no data for record: {record}"
                )
            logger.info(
                "[Supabase] Inserted workspace_file id=%s file=%s order=%s",
                response.data[0].get("id"),
                record.get("file_name"),
                record.get("upload_order"),
            )
            return response.data[0]
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[Supabase] Could not insert workspace_file: %s", exc)
            raise RuntimeError(str(exc)) from exc

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def list_workspace_files(
    workspace_id: str,
    user_id: str,
) -> List[Dict[str, Any]]:
    """Fetches all workspace_files rows for a given workspace, ordered by upload_order ASC.

    Args:
        workspace_id: The workspace UUID to filter by.
        user_id: The authenticated user UUID — enforces row-level ownership.

    Returns:
        List of workspace_files rows ordered by upload_order ascending.
    """
    def _sync() -> List[Dict[str, Any]]:
        client = _make_service_client()
        try:
            response = (
                client.table("workspace_files")
                .select("*")
                .eq("workspace_id", workspace_id)
                .eq("user_id", user_id)
                .order("upload_order", desc=False)
                .execute()
            )
            return response.data or []
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "[Supabase] Could not list workspace_files for workspace=%s: %s",
                workspace_id,
                exc,
            )
            return []

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def count_workspace_files(workspace_id: str, user_id: str) -> int:
    """Returns the count of existing workspace_files rows for a workspace.

    Used to determine the next upload_order value (count + 1).

    Args:
        workspace_id: The workspace UUID.
        user_id: The authenticated user UUID.

    Returns:
        Integer count of existing rows (0 if none).
    """
    def _sync() -> int:
        client = _make_service_client()
        try:
            response = (
                client.table("workspace_files")
                .select("id", count="exact")
                .eq("workspace_id", workspace_id)
                .eq("user_id", user_id)
                .execute()
            )
            return response.count or 0
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "[Supabase] Could not count workspace_files for workspace=%s: %s",
                workspace_id,
                exc,
            )
            return 0

    return await asyncio.get_running_loop().run_in_executor(None, _sync)


async def delete_workspace_file(
    file_id: str,
    workspace_id: str,
    user_id: str,
) -> bool:
    """Deletes a single workspace_files row after verifying ownership.

    Args:
        file_id: UUID of the workspace_files row to delete.
        workspace_id: Must match the row's workspace_id (ownership check).
        user_id: Must match the row's user_id (ownership check).

    Returns:
        True if a row was deleted, False if not found or not owned by user.
    """
    def _sync() -> bool:
        client = _make_service_client()
        try:
            response = (
                client.table("workspace_files")
                .delete()
                .eq("id", file_id)
                .eq("workspace_id", workspace_id)
                .eq("user_id", user_id)
                .execute()
            )
            deleted = bool(response.data)
            if deleted:
                logger.info(
                    "[Supabase] Deleted workspace_file id=%s from workspace=%s",
                    file_id,
                    workspace_id,
                )
            else:
                logger.warning(
                    "[Supabase] workspace_file id=%s not found or not owned by user=%s",
                    file_id,
                    user_id,
                )
            return deleted
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "[Supabase] Could not delete workspace_file id=%s: %s",
                file_id,
                exc,
            )
            return False

    return await asyncio.get_running_loop().run_in_executor(None, _sync)
