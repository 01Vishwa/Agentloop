"""Tests for the workspace upload and delete controller functions.

Covers:
  - handle_workspace_upload: accepted + rejected files
  - handle_workspace_upload: parser failure is non-fatal
  - handle_workspace_upload: empty file list returns empty MultiFileContext
  - handle_delete_workspace_file: success path returns updated context
  - handle_delete_workspace_file: raises ValueError for unknown file_id
  - handle_upload (legacy): backward-compat path unchanged
"""

import io
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import UploadFile
from httpx import Headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_upload_file(filename: str, content: bytes = b"col_a,col_b\n1,2\n") -> UploadFile:
    """Creates a minimal UploadFile mock that behaves like a real upload."""
    file_like = io.BytesIO(content)
    # FastAPI's UploadFile expects a SpooledTemporaryFile; BytesIO is sufficient for tests.
    uf = MagicMock(spec=UploadFile)
    uf.filename = filename
    uf.read = AsyncMock(return_value=content)
    uf.seek = AsyncMock()
    return uf


# ---------------------------------------------------------------------------
# validate_file_metadata stub — always returns None (no issue)
# ---------------------------------------------------------------------------

_VALIDATE_PATCH = "api.controllers.upload_controller.validate_file_metadata"
_SAVE_PATCH     = "api.controllers.upload_controller.save_upload_file"
_INSERT_PATCH   = "services.supabase_service.insert_workspace_file"
_COUNT_PATCH    = "services.supabase_service.count_workspace_files"
_LIST_PATCH     = "services.supabase_service.list_workspace_files"
_MERGE_PATCH    = "api.controllers.upload_controller.merge_schemas"
_PARSER_PATCH   = "api.controllers.upload_controller._get_parser_for"


# ---------------------------------------------------------------------------
# handle_workspace_upload tests
# ---------------------------------------------------------------------------

class TestHandleWorkspaceUpload:
    """Tests for the multi-file workspace upload controller."""

    @pytest.mark.asyncio
    async def test_single_file_accepted(self, tmp_path):
        """A valid single CSV upload should appear in accepted_files."""
        from api.controllers.upload_controller import handle_workspace_upload

        file = _make_upload_file("sales.csv")

        mock_mfc = MagicMock()
        mock_mfc.join_candidates = []
        mock_mfc.model_dump.return_value = {"files": [], "join_candidates": []}

        with (
            patch(_VALIDATE_PATCH, new=AsyncMock(return_value=None)),
            patch(_SAVE_PATCH, new=AsyncMock()),
            patch(_COUNT_PATCH, new=AsyncMock(return_value=0)),
            patch(_INSERT_PATCH, new=AsyncMock()),
            patch(_LIST_PATCH, new=AsyncMock(return_value=[{"id": "abc", "file_name": "sales.csv"}])),
            patch(_MERGE_PATCH, new=AsyncMock(return_value=mock_mfc)),
            patch(_PARSER_PATCH, return_value=None),
            patch("os.makedirs"),
            patch("builtins.open", MagicMock()),
        ):
            result = await handle_workspace_upload(
                files=[file],
                workspace_id="ws-001",
                user_id="user-001",
                session_id="sess-001",
            )

        assert len(result["accepted_files"]) == 1
        assert result["accepted_files"][0].filename == "sales.csv"
        assert result["rejected_files"] == []

    @pytest.mark.asyncio
    async def test_rejected_on_validation_failure(self):
        """A file that fails MIME validation should land in rejected_files."""
        from api.controllers.upload_controller import handle_workspace_upload

        file = _make_upload_file("malicious.exe")

        mock_mfc = MagicMock()
        mock_mfc.join_candidates = []
        mock_mfc.model_dump.return_value = {"files": [], "join_candidates": []}

        with (
            patch(_VALIDATE_PATCH, new=AsyncMock(return_value="Unsupported file type: exe")),
            patch(_LIST_PATCH, new=AsyncMock(return_value=[])),
            patch(_MERGE_PATCH, new=AsyncMock(return_value=mock_mfc)),
        ):
            result = await handle_workspace_upload(
                files=[file],
                workspace_id="ws-001",
                user_id="user-001",
            )

        assert len(result["rejected_files"]) == 1
        assert "exe" in result["rejected_files"][0].reason.lower()
        assert result["accepted_files"] == []

    @pytest.mark.asyncio
    async def test_parser_failure_is_nonfatal(self):
        """If the parser raises an exception, the file is still accepted (schema_json empty)."""
        from api.controllers.upload_controller import handle_workspace_upload

        file = _make_upload_file("data.csv")

        def _exploding_parser(filename, content):
            raise RuntimeError("Parser exploded")

        mock_mfc = MagicMock()
        mock_mfc.join_candidates = []
        mock_mfc.model_dump.return_value = {"files": [], "join_candidates": []}

        with (
            patch(_VALIDATE_PATCH, new=AsyncMock(return_value=None)),
            patch(_SAVE_PATCH, new=AsyncMock()),
            patch(_COUNT_PATCH, new=AsyncMock(return_value=0)),
            patch(_INSERT_PATCH, new=AsyncMock()),
            patch(_LIST_PATCH, new=AsyncMock(return_value=[])),
            patch(_MERGE_PATCH, new=AsyncMock(return_value=mock_mfc)),
            patch(_PARSER_PATCH, return_value=_exploding_parser),
            patch("os.makedirs"),
            patch("builtins.open", MagicMock()),
        ):
            result = await handle_workspace_upload(
                files=[file],
                workspace_id="ws-001",
                user_id="user-001",
            )

        # File should still be accepted despite parser failure
        assert len(result["accepted_files"]) == 1

    @pytest.mark.asyncio
    async def test_empty_file_list_returns_empty(self):
        """Uploading no files should return empty lists and an empty MultiFileContext."""
        from api.controllers.upload_controller import handle_workspace_upload

        mock_mfc = MagicMock()
        mock_mfc.join_candidates = []
        mock_mfc.model_dump.return_value = {"files": [], "join_candidates": []}

        with (
            patch(_LIST_PATCH, new=AsyncMock(return_value=[])),
            patch(_MERGE_PATCH, new=AsyncMock(return_value=mock_mfc)),
        ):
            result = await handle_workspace_upload(
                files=[],
                workspace_id="ws-001",
                user_id="user-001",
            )

        assert result["accepted_files"] == []
        assert result["rejected_files"] == []

    @pytest.mark.asyncio
    async def test_multi_file_upload_order_increments(self):
        """upload_order should be 1 for first file and 2 for second in the same call."""
        from api.controllers.upload_controller import handle_workspace_upload

        inserted_orders = []

        async def _fake_insert(record):
            inserted_orders.append(record["upload_order"])

        count_call = 0
        async def _fake_count(**kwargs):
            nonlocal count_call
            count_call += 1
            # Simulate: 0 existing before first file, 1 before second
            return count_call - 1

        mock_mfc = MagicMock()
        mock_mfc.join_candidates = []
        mock_mfc.model_dump.return_value = {"files": [], "join_candidates": []}

        file1 = _make_upload_file("a.csv")
        file2 = _make_upload_file("b.csv")

        with (
            patch(_VALIDATE_PATCH, new=AsyncMock(return_value=None)),
            patch(_SAVE_PATCH, new=AsyncMock()),
            patch(_COUNT_PATCH, new=AsyncMock(side_effect=_fake_count)),
            patch("services.supabase_service.insert_workspace_file", new=AsyncMock(side_effect=_fake_insert)),
            patch(_LIST_PATCH, new=AsyncMock(return_value=[])),
            patch(_MERGE_PATCH, new=AsyncMock(return_value=mock_mfc)),
            patch(_PARSER_PATCH, return_value=None),
            patch("os.makedirs"),
            patch("builtins.open", MagicMock()),
        ):
            await handle_workspace_upload(
                files=[file1, file2],
                workspace_id="ws-001",
                user_id="user-001",
            )

        assert inserted_orders == [1, 2], f"Expected [1, 2], got {inserted_orders}"


# ---------------------------------------------------------------------------
# handle_delete_workspace_file tests
# ---------------------------------------------------------------------------

class TestHandleDeleteWorkspaceFile:
    """Tests for the file deletion controller."""

    @pytest.mark.asyncio
    async def test_delete_success_returns_updated_context(self):
        """Deleting an existing file returns deleted=True and the refreshed context."""
        from api.controllers.upload_controller import handle_delete_workspace_file

        mock_mfc = MagicMock()
        mock_mfc.join_candidates = []
        mock_mfc.model_dump.return_value = {"files": [], "join_candidates": []}

        _LIST_SIDE_EFFECTS = [
            # First call (pre-delete): one file exists
            [{"id": "file-123", "file_name": "orders.csv", "file_path": "/tmp/orders.csv"}],
            # Second call (post-delete): workspace is empty
            [],
        ]

        with (
            patch("api.controllers.upload_controller.list_workspace_files",
                  new=AsyncMock(side_effect=_LIST_SIDE_EFFECTS)),
            patch("api.controllers.upload_controller.delete_workspace_file",
                  new=AsyncMock(return_value=True)),
            patch("api.controllers.upload_controller.merge_schemas",
                  new=AsyncMock(return_value=mock_mfc)),
            patch("os.path.isfile", return_value=False),  # skip physical delete
        ):
            result = await handle_delete_workspace_file(
                file_id="file-123",
                workspace_id="ws-001",
                user_id="user-001",
            )

        assert result["deleted"] is True
        assert "multi_file_context" in result

    @pytest.mark.asyncio
    async def test_delete_unknown_file_raises_value_error(self):
        """Attempting to delete a file_id not in the workspace raises ValueError."""
        from api.controllers.upload_controller import handle_delete_workspace_file

        with (
            patch("api.controllers.upload_controller.list_workspace_files",
                  new=AsyncMock(return_value=[])),  # empty workspace
        ):
            with pytest.raises(ValueError, match="not found"):
                await handle_delete_workspace_file(
                    file_id="nonexistent-id",
                    workspace_id="ws-001",
                    user_id="user-001",
                )


# ---------------------------------------------------------------------------
# handle_upload (legacy backward-compat) tests
# ---------------------------------------------------------------------------

class TestHandleUploadLegacy:
    """Ensures the legacy /upload path is still functional."""

    @pytest.mark.asyncio
    async def test_legacy_upload_returns_accepted(self):
        from api.controllers.upload_controller import handle_upload

        file = _make_upload_file("legacy.csv")

        with (
            patch(_VALIDATE_PATCH, new=AsyncMock(return_value=None)),
            patch(_SAVE_PATCH, new=AsyncMock()),
        ):
            response = await handle_upload(
                files=[file],
                session_id="sess-legacy",
                user_id="user-001",
            )

        assert len(response.accepted_files) == 1
        assert response.accepted_files[0].filename == "legacy.csv"
        assert response.rejected_files == []

    @pytest.mark.asyncio
    async def test_legacy_upload_rejects_on_validation_error(self):
        from api.controllers.upload_controller import handle_upload

        file = _make_upload_file("bad.exe")

        with patch(_VALIDATE_PATCH, new=AsyncMock(return_value="Unsupported type")):
            response = await handle_upload(
                files=[file],
                session_id="sess-legacy",
                user_id="user-001",
            )

        assert len(response.rejected_files) == 1
        assert response.accepted_files == []
