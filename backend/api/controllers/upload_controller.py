"""Upload controller.

Handles request-level orchestration for file upload:
validates metadata, delegates persistence to upload_service,
and builds the structured UploadResponse.
"""

from typing import List, Optional

from fastapi import UploadFile

from core.validation import validate_file_metadata
from models.schemas import UploadResponse, FileStatusItem
from services.upload_service import save_upload_file


async def handle_upload(
    files: List[UploadFile],
    session_id: str = "__anon__",
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> UploadResponse:
    """Orchestrates validation and persistence for a batch of uploaded files.

    Args:
        files: Multipart file payloads from the request.
        session_id: Session identifier used to bucket files in the in-memory
            cache.  Ensures files from different users never collide.
        user_id: Optional authenticated user identifier.

    Returns:
        UploadResponse: Lists of accepted and rejected FileStatusItems.
    """
    accepted: List[FileStatusItem] = []
    rejected: List[FileStatusItem] = []

    for file in files:
        if not file.filename:
            continue

        # Extension, MIME & magic-byte validation (MIN-04: now async)
        meta_issue = await validate_file_metadata(file)
        if meta_issue:
            rejected.append(
                FileStatusItem(filename=file.filename, status="error", reason=meta_issue)
            )
            continue

        try:
            await save_upload_file(file, session_id=session_id, user_id=user_id, workspace_id=workspace_id)
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
        except Exception as e:  # pylint: disable=broad-except
            rejected.append(
                FileStatusItem(
                    filename=file.filename,
                    status="error",
                    reason=f"Stream error: {str(e)}",
                )
            )

    return UploadResponse(accepted_files=accepted, rejected_files=rejected)
