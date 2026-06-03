"""Process controller.

Handles request-level orchestration for the /process endpoint:
resolves file paths and delegates to the process service.

Multi-file extension:
  ``get_schema_context`` queries the workspace_files table and runs
  schema_merger to produce a MultiFileContext for the agent pipeline.
  This is called by the orchestrator when a workspace_id is available.
"""

from typing import Dict, Any, List

from services.process_service import process_documents
from services.upload_service import clear_file_cache


def handle_process(
    filenames: List[str],
    session_id: str = "__anon__",
) -> Dict[str, Any]:
    """Resolves upload references and triggers document processing in-memory.

    Args:
        filenames: Filenames of previously uploaded documents.
        session_id: Session identifier used to look up files from the
            session-scoped cache.

    Returns:
        Dict[str, Any]: Structured process result with status and details.
    """
    context = process_documents(filenames, session_id=session_id)

    return {
        "status": "success",
        "message": f"Successfully processed {context.get('files_processed')} files.",
        "details": context,
    }


def handle_clear(session_id: str = "__anon__") -> Dict[str, Any]:
    """Triggers the clearing of the session's byte cache.

    Args:
        session_id: Session to wipe.  Only that session's files are removed.

    Returns:
        Dict[str, Any]: Response status.
    """
    clear_file_cache(session_id=session_id)
    return {"status": "success", "message": "In-memory cache completely wiped."}


async def get_schema_context(workspace_id: str, user_id: str):
    """Builds a MultiFileContext from the workspace_files table.

    Fetches all workspace_files rows for the given workspace and user,
    ordered by upload_order ASC, then delegates to schema_merger to infer
    join candidates from the stored schema_json metadata.

    Args:
        workspace_id: UUID of the target workspace.
        user_id: Authenticated user UUID for RLS enforcement.

    Returns:
        MultiFileContext with FileSchema objects and join_candidates.
        If no files are found, returns an empty MultiFileContext.
    """
    from services.supabase_service import list_workspace_files  # pylint: disable=import-outside-toplevel
    from services.schema_merger import merge_schemas              # pylint: disable=import-outside-toplevel

    file_metas = await list_workspace_files(workspace_id=workspace_id, user_id=user_id)

    if not file_metas:
        from models.multi_file_context import MultiFileContext  # pylint: disable=import-outside-toplevel
        return MultiFileContext(files=[], join_candidates=[])

    return await merge_schemas(file_metas)
