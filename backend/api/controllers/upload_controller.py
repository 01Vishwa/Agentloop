"""Upload controller.

Handles request-level orchestration for file upload:
validates metadata, delegates persistence to upload_service,
writes workspace_files rows to Supabase, and returns a
MultiFileContext with inferred join candidates.

Multi-file changes (Phase 9):
  - Sequential per-file processing with upload_order tracking.
  - Parser-based schema_json + row_count extraction per file.
  - workspace_files DB insert after each accepted file.
  - schema_merger called after all files to build MultiFileContext.
  - DELETE handler for workspace file removal with schema refresh.
"""

import logging
import math
import os
from typing import Any, Dict, List, Optional

from fastapi import UploadFile

from core.validation import validate_file_metadata
from models.schemas import UploadResponse, FileStatusItem
from services.upload_service import save_upload_file

logger = logging.getLogger("uvicorn.error")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_parser_for(file_type: str):
    """Returns the appropriate synchronous parser function for a file type.

    Args:
        file_type: Lowercase file extension string (e.g. 'csv', 'xlsx').

    Returns:
        A callable ``parse_*(file_name, file_content_bytes) -> dict`` or None
        if no structured parser is available for this type.
    """
    if file_type == "csv":
        from services.parsers.csv_parser import parse_csv  # pylint: disable=import-outside-toplevel
        return parse_csv
    if file_type in ("xlsx", "xls"):
        from services.parsers.excel_parser import parse_excel  # pylint: disable=import-outside-toplevel
        return parse_excel
    if file_type == "parquet":
        from services.parsers.parquet_parser import parse_parquet  # pylint: disable=import-outside-toplevel
        return parse_parquet
    if file_type == "json":
        from services.parsers.json_parser import parse_json  # pylint: disable=import-outside-toplevel
        return parse_json
    if file_type in ("md", "markdown"):
        from services.parsers.md_parser import parse_md  # pylint: disable=import-outside-toplevel
        return parse_md
    return None


def _extract_schema_json(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Extracts the schema_json payload from a UnifiedDocumentContext dict.

    We store the ``metadata`` block as schema_json because it contains the
    column list, dtypes, and sample rows that schema_merger needs.

    Args:
        parsed: Dict returned by a parser function.

    Returns:
        The metadata sub-dict, or empty dict if not present.
    """
    return parsed.get("metadata") or {}


def _resolve_file_path(workspace_id: str, filename: str) -> str:
    """Builds the canonical on-disk path for an uploaded workspace file.

    Args:
        workspace_id: Workspace UUID used as the directory name.
        filename: Original filename (sanitised by save_upload_file).

    Returns:
        Absolute path string.
    """
    base_dir = os.environ.get("WORKSPACE_FILES_DIR", "/workspace")
    return os.path.join(base_dir, workspace_id, filename)


def _json_safe(obj: Any) -> Any:
    """Recursively replaces non-JSON-compliant floats (NaN, Inf) with None.

    Acts as a safety net at the DB-insert boundary in case a parser
    produces dicts containing ``float('nan')`` or ``float('inf')``.
    The JSON spec does not permit these values and supabase-py's
    ``json.dumps()`` will raise ``ValueError`` if they are present.

    Args:
        obj: Any Python object (dict, list, scalar).

    Returns:
        The same structure with offending floats replaced by None.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Multi-file workspace upload handler
# ---------------------------------------------------------------------------

async def handle_workspace_upload(
    files: List[UploadFile],
    workspace_id: str,
    user_id: str,
    session_id: str = "__anon__",
) -> Dict[str, Any]:
    """Orchestrates validation, persistence, and schema inference for a batch of files.

    Processing is sequential (not concurrent) so upload_order is strictly
    monotonic.  For each accepted file:
      1. Validate extension / MIME / magic bytes.
      2. Save to disk at /workspace/{workspace_id}/{filename}.
      3. Parse to extract schema_json and row_count.
      4. INSERT into workspace_files with the next upload_order.

    After all files are processed:
      5. Fetch all workspace_files rows for the workspace.
      6. Run schema_merger to build a MultiFileContext.

    Args:
        files: Multipart file payloads from the request.
        workspace_id: Target workspace UUID.
        user_id: Authenticated user UUID (used for ownership columns).
        session_id: Session identifier for the in-memory file cache.

    Returns:
        Dict with keys:
          - ``accepted_files``: List of FileStatusItem dicts (successes).
          - ``rejected_files``: List of FileStatusItem dicts (failures).
          - ``multi_file_context``: MultiFileContext dict (join candidates etc.)
    """
    from services.supabase_service import (  # pylint: disable=import-outside-toplevel
        insert_workspace_file,
        count_workspace_files,
        list_workspace_files,
    )
    from services.schema_merger import merge_schemas  # pylint: disable=import-outside-toplevel

    accepted: List[FileStatusItem] = []
    rejected: List[FileStatusItem] = []

    for file in files:
        if not file.filename:
            continue

        # Extension, MIME & magic-byte validation
        meta_issue = await validate_file_metadata(file)
        if meta_issue:
            rejected.append(
                FileStatusItem(filename=file.filename, status="error", reason=meta_issue)
            )
            continue

        file_ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        # Normalise xlsx variants
        file_type = "xlsx" if file_ext == "xls" else file_ext

        try:
            # Read file bytes (seek back to 0 after validation consumed the stream)
            await file.seek(0)
            file_bytes = await file.read()

            # Save to session-scoped in-memory cache (for FileAnalyzerAgent LLM path)
            await file.seek(0)
            await save_upload_file(
                file,
                session_id=session_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )

            # Resolve the on-disk path used by the Coder agent
            file_path = _resolve_file_path(workspace_id, file.filename)

            # Save bytes to disk (create workspace dir if needed)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "wb") as fh:
                fh.write(file_bytes)

            # Parse to extract schema_json and row_count
            parser = _get_parser_for(file_type)
            schema_json: Dict[str, Any] = {}
            row_count: int = 0

            if parser is not None:
                try:
                    parsed = parser(file.filename, file_bytes)
                    schema_json = _extract_schema_json(parsed)
                    row_count = schema_json.get("row_count") or 0
                except Exception as parse_exc:  # pylint: disable=broad-except
                    logger.warning(
                        "[UploadController] Parser failed for %s (%s); "
                        "schema_json will be empty.",
                        file.filename,
                        parse_exc,
                    )

            # Determine upload_order = existing row count + 1
            existing_count = await count_workspace_files(
                workspace_id=workspace_id,
                user_id=user_id,
            )
            upload_order = existing_count + 1

            # Insert workspace_files row
            db_record = {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "file_name": file.filename,
                "file_path": file_path,
                "file_type": file_type,
                "file_size": len(file_bytes),
                "row_count": row_count if row_count > 0 else None,
                "schema_json": _json_safe(schema_json),
                "upload_order": upload_order,
            }
            try:
                await insert_workspace_file(db_record)
            except Exception as db_exc:  # pylint: disable=broad-except
                logger.warning(
                    "[UploadController] workspace_files insert failed for %s: %s",
                    file.filename,
                    db_exc,
                )
                # Don't abort — file is on disk and in cache; the session still works.

            accepted.append(
                FileStatusItem(
                    filename=file.filename,
                    status="success",
                    reason=f"Uploaded (order={upload_order}, rows={row_count}).",
                )
            )

        except ValueError as ve:
            rejected.append(
                FileStatusItem(filename=file.filename, status="error", reason=str(ve))
            )
        except Exception as exc:  # pylint: disable=broad-except
            rejected.append(
                FileStatusItem(
                    filename=file.filename,
                    status="error",
                    reason=f"Stream error: {str(exc)}",
                )
            )

    # Build MultiFileContext from ALL current workspace_files rows
    all_metas = await list_workspace_files(workspace_id=workspace_id, user_id=user_id)
    multi_file_context = await merge_schemas(all_metas)

    logger.info(
        "[UploadController] workspace=%s accepted=%d rejected=%d files_total=%d candidates=%d",
        workspace_id[:8],
        len(accepted),
        len(rejected),
        len(all_metas),
        len(multi_file_context.join_candidates),
    )

    return {
        "accepted_files": accepted,
        "rejected_files": rejected,
        "multi_file_context": multi_file_context.model_dump(),
    }


# ---------------------------------------------------------------------------
# Legacy single-file upload handler (backward compat — unchanged)
# ---------------------------------------------------------------------------

async def handle_upload(
    files: List[UploadFile],
    session_id: str = "__anon__",
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> UploadResponse:
    """Orchestrates validation and persistence for a batch of uploaded files.

    This is the existing /upload endpoint handler kept for backward
    compatibility.  It saves files to the session-scoped in-memory cache
    only and does NOT write to workspace_files or return a MultiFileContext.

    Args:
        files: Multipart file payloads from the request.
        session_id: Session identifier for the in-memory cache.
        user_id: Optional authenticated user identifier.
        workspace_id: Optional workspace UUID (passed to save_upload_file
            for storage path but does NOT trigger workspace_files inserts).

    Returns:
        UploadResponse: Lists of accepted and rejected FileStatusItems.
    """
    accepted: List[FileStatusItem] = []
    rejected: List[FileStatusItem] = []

    for file in files:
        if not file.filename:
            continue

        meta_issue = await validate_file_metadata(file)
        if meta_issue:
            rejected.append(
                FileStatusItem(filename=file.filename, status="error", reason=meta_issue)
            )
            continue

        try:
            await save_upload_file(
                file,
                session_id=session_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            accepted.append(
                FileStatusItem(
                    filename=file.filename,
                    status="success",
                    reason="Uploaded and correctly sanitized.",
                )
            )
        except ValueError as ve:
            rejected.append(
                FileStatusItem(filename=file.filename, status="error", reason=str(ve))
            )
        except Exception as exc:  # pylint: disable=broad-except
            rejected.append(
                FileStatusItem(
                    filename=file.filename,
                    status="error",
                    reason=f"Stream error: {str(exc)}",
                )
            )

    return UploadResponse(accepted_files=accepted, rejected_files=rejected)


# ---------------------------------------------------------------------------
# DELETE workspace file handler
# ---------------------------------------------------------------------------

async def handle_delete_workspace_file(
    file_id: str,
    workspace_id: str,
    user_id: str,
) -> Dict[str, Any]:
    """Removes a file from the workspace and refreshes the join context.

    Steps:
      1. Fetch the workspace_files row to get file_path (ownership check
         is embedded in the Supabase delete call via eq(user_id)).
      2. Delete the row from workspace_files.
      3. Delete the physical file from disk (best-effort).
      4. Re-run schema_merger on remaining files.
      5. Return the updated MultiFileContext.

    Args:
        file_id: UUID of the workspace_files row to delete.
        workspace_id: Workspace UUID for ownership verification.
        user_id: Authenticated user UUID.

    Returns:
        Dict with keys:
          - ``deleted``: bool — True if a row was removed.
          - ``multi_file_context``: Updated MultiFileContext dict.

    Raises:
        ValueError: If the file_id is not found or not owned by user.
    """
    from services.supabase_service import (  # pylint: disable=import-outside-toplevel
        list_workspace_files,
        delete_workspace_file,
    )
    from services.schema_merger import merge_schemas  # pylint: disable=import-outside-toplevel

    # Fetch current rows to get file_path before deletion
    all_rows = await list_workspace_files(workspace_id=workspace_id, user_id=user_id)
    target_row = next((r for r in all_rows if r.get("id") == file_id), None)

    if not target_row:
        raise ValueError(
            f"workspace_file '{file_id}' not found in workspace '{workspace_id}' "
            f"or not owned by this user."
        )

    # Delete from DB
    deleted = await delete_workspace_file(
        file_id=file_id,
        workspace_id=workspace_id,
        user_id=user_id,
    )

    # Best-effort physical file deletion
    file_path = target_row.get("file_path", "")
    if file_path and os.path.isfile(file_path):
        try:
            os.remove(file_path)
            logger.info(
                "[UploadController] Deleted physical file: %s",
                file_path,
            )
        except OSError as os_exc:
            logger.warning(
                "[UploadController] Could not delete physical file %s: %s",
                file_path,
                os_exc,
            )

    # Refresh MultiFileContext from remaining rows
    remaining_rows = await list_workspace_files(workspace_id=workspace_id, user_id=user_id)
    multi_file_context = await merge_schemas(remaining_rows)

    logger.info(
        "[UploadController] Deleted file_id=%s from workspace=%s; "
        "%d file(s) remaining, %d candidate(s).",
        file_id[:8],
        workspace_id[:8],
        len(remaining_rows),
        len(multi_file_context.join_candidates),
    )

    return {
        "deleted": deleted,
        "multi_file_context": multi_file_context.model_dump(),
    }
