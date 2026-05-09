"""Pydantic schemas for unified API routing.

Enforces deep structural normalization across all analytical parsers, ensuring
that parsed output merges identically regardless of its source file origin.
Extended with AnalysisMode, DatasetUploadRecord for Supabase integration.
Extended further with DS-STAR agent schemas.
"""

from enum import Enum
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field


class AnalysisMode(str, Enum):
    """Runtime switch controlling pipeline routing.

    ON  → structured_dataset_pipeline (CSV, XLSX, JSON → Supabase)
    OFF → idp_pipeline (all formats → in-memory)
    """

    ON = "ON"
    OFF = "OFF"


class FileStatusItem(BaseModel):
    """Represents the status of an uploaded file."""

    filename: str
    status: str
    reason: str = ""


class DatasetUploadRecord(BaseModel):
    """Mirrors a row in the Supabase uploaded_files table."""

    id: Optional[str] = None
    filename: str
    file_path: str
    file_url: str
    file_size: int
    extension: str
    analysis_mode: bool = True
    workspace_id: Optional[str] = None
    created_at: Optional[str] = None


class UploadResponse(BaseModel):
    """Structured response for file uploads."""

    accepted_files: List[FileStatusItem]
    rejected_files: List[FileStatusItem]
    supabase_records: Optional[List[DatasetUploadRecord]] = None


class UnifiedDocumentContext(BaseModel):
    """Normalization standard for EVERY parsed document.

    All parsers must return this strict dictionary shape to unify
    downstream analytics effortlessly.
    """

    file_name: str
    source_type: str
    sanitized_content: str
    metadata: Dict[str, Any]


class ProcessRequest(BaseModel):
    """Request body for the /process endpoint."""

    files: List[str]
    analysis_mode: AnalysisMode = AnalysisMode.OFF


class ErrorResponse(BaseModel):
    """Structured unified error response."""

    status: str
    message: str


# ---------------------------------------------------------------------------
# DS-STAR Agent Schemas
# ---------------------------------------------------------------------------

class PlanStep(BaseModel):
    """A single step in the DS-STAR analysis plan."""

    index: int
    description: str
    status: str = "pending"


class AgentRunResult(BaseModel):
    """Full result payload from the DS-STAR orchestrator."""

    insights: Dict[str, Any]
    code: Dict[str, str]
    plan_steps: List[PlanStep]
    rounds: int
    execution_logs: List[str]


class AgentEvent(BaseModel):
    """A single SSE event emitted by the orchestrator during a run."""

    event: str
    payload: Dict[str, Any]


class AgentRunRequest(BaseModel):
    """Request body for the /agent/run endpoint."""

    query: str = Field(
        ...,
        min_length=1,
        max_length=5000,
        description="The analytical question or instruction for the DS-STAR agent.",
    )
    session_id: Optional[str] = None
    workspace_id: Optional[str] = None
    # ── Per-run LLM / agent customisation ─────────────────────────────────
    max_rounds: Optional[int] = Field(
        default=None,
        ge=1,
        le=10,
        description="Number of agent reasoning rounds (1–10). Overrides MAX_AGENT_ROUNDS.",
    )
    model: Optional[str] = None             # reasoning model override
    coder_model: Optional[str] = None       # code-generation model override
    temperature: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Sampling temperature (0.0–1.0).",
    )

