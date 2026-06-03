"""Application configuration — environment-driven settings.

Loads all credentials and tuning parameters from the .env file.

Gap fixes applied:
- ANALYSIS_MODE_ALLOWED_FORMATS expanded to include ALL supported formats.
  DS-STAR paper explicitly requires heterogeneous data (CSV + PDF + JSON together).
- Added PARQUET to ALLOWED_MIME_TYPES.
- MAX_TOKENS_PER_RUN enforced via TokenTracker in the orchestrator.
- DS-STAR+ model routing: NIM_MODEL_PRO for reasoning-heavy agents;
  NIM_MODEL_FLASH for high-throughput / summarisation agents.
- MAX_DEBUGGER_RETRIES caps the Debugger → Code loop per round.
- Docker sandbox flags added for Gap 2 (subprocess → container isolation).
"""

import os
from typing import Dict, Set

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))

# ---------------------------------------------------------------------------
# File size limit
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB

# ---------------------------------------------------------------------------
# MIME type mappings — used by metadata validator
# ---------------------------------------------------------------------------

ALLOWED_MIME_TYPES: Dict[str, str] = {
    "csv":     "text/csv",
    "txt":     "text/plain",
    "xlsx":    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf":     "application/pdf",
    "json":    "application/json",
    "md":      "text/markdown",
    "parquet": "application/octet-stream",
}

# Gap fix: Expanded to allow all formats for DS-STAR agent runs.
# The paper's key innovation is handling HETEROGENEOUS data (CSV + PDF + JSON).
# The previous restriction to csv/xlsx/json prevented hard-task benchmarks.
ANALYSIS_MODE_ALLOWED_FORMATS: Set[str] = set(ALLOWED_MIME_TYPES.keys())

# ---------------------------------------------------------------------------
# Supabase credentials
# ---------------------------------------------------------------------------

SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_PUBLISHABLE_KEY: str = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
SUPABASE_BUCKET: str = os.getenv("SUPABASE_BUCKET", "agentloop-uploads")

# JWT secret for server-side token verification.
# Find at: Supabase Dashboard → Settings → API → JWT Settings → JWT Secret
SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")
SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")



# ---------------------------------------------------------------------------
# NVIDIA NIM configuration
# ---------------------------------------------------------------------------

NVIDIA_API_KEY: str = os.getenv("NVIDIA_API_KEY", "")

# Reasoning / planning / verification model (fast, large context)
NIM_MODEL_DEFAULT: str = os.getenv(
    "NIM_MODEL_DEFAULT", "meta/llama-3.1-70b-instruct"
)

# Code-generation model (stronger for producing runnable Python)
NIM_MODEL_CODER: str = os.getenv(
    "NIM_MODEL_CODER", "meta/llama-3.3-70b-instruct"
)

# ---------------------------------------------------------------------------
# DS-STAR Model Routing (Pro = reasoning-heavy, Flash = fast throughput)
# ---------------------------------------------------------------------------
# Routing table:
#   Planner      → Pro    Coder        → Pro
#   Verifier     → Pro    Debugger     → Pro
#   SubQuestion  → Flash  ReportWriter → Flash
#   Analyzer     → Flash
# ---------------------------------------------------------------------------

# Pro model: highest reasoning capability for planning/coding/verification
NIM_MODEL_PRO: str = os.getenv("NIM_MODEL_PRO", NIM_MODEL_DEFAULT)

# Flash model: fast, cost-efficient for sub-questions and summaries
NIM_MODEL_FLASH: str = os.getenv("NIM_MODEL_FLASH", NIM_MODEL_DEFAULT)

# ---------------------------------------------------------------------------
# DS-STAR Agent tuning
# ---------------------------------------------------------------------------

MAX_AGENT_ROUNDS: int = int(os.getenv("MAX_AGENT_ROUNDS", "10"))
EXECUTION_TIMEOUT_SECONDS: int = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "60"))
MAX_TOKENS_PER_RUN: int = int(os.getenv("MAX_TOKENS_PER_RUN", "50000"))

# Max times the Debugger→Code sub-loop fires per orchestrator round
MAX_DEBUGGER_RETRIES: int = int(os.getenv("MAX_DEBUGGER_RETRIES", "3"))

# DS-STAR+ concurrency: parallel DS-STAR runs for sub-questions
DS_STAR_PLUS_MAX_WORKERS: int = int(os.getenv("DS_STAR_PLUS_MAX_WORKERS", "3"))

# DS-STAR+ per-sub-question round cap (narrower scope → fewer rounds needed).
# Kept deliberately lower than MAX_AGENT_ROUNDS to avoid a 5×10-round runaway.
DS_STAR_PLUS_MAX_ROUNDS: int = int(os.getenv("DS_STAR_PLUS_MAX_ROUNDS", "3"))

# ---------------------------------------------------------------------------
# Docker sandbox configuration (Gap 2)
# ---------------------------------------------------------------------------
# Set DOCKER_SANDBOX_ENABLED=true in production to run generated code inside
# a Docker container with --network none and memory/CPU caps.
# When false (default) the executor uses a subprocess with a sanitised env.

DOCKER_SANDBOX_ENABLED: bool = (
    os.getenv("DOCKER_SANDBOX_ENABLED", "false").lower() == "true"
)
DOCKER_SANDBOX_IMAGE: str = os.getenv(
    "DOCKER_SANDBOX_IMAGE", "python:3.11-slim"
)
DOCKER_MEMORY_LIMIT: str = os.getenv("DOCKER_MEMORY_LIMIT", "512m")
DOCKER_CPU_QUOTA: float = float(os.getenv("DOCKER_CPU_QUOTA", "0.5"))

# ---------------------------------------------------------------------------
# Subprocess sandbox resource limits (BUG 1 fix)
# ---------------------------------------------------------------------------
# Applied via RLIMIT_CPU / RLIMIT_AS inside the forked child on Unix.
# Keeps a runaway / malicious script from exhausting host CPU or RAM.

# Max CPU seconds the sandbox script may consume before SIGXCPU / SIGKILL.
SANDBOX_CPU_TIME_LIMIT_SECONDS: int = int(
    os.getenv("SANDBOX_CPU_TIME_LIMIT_SECONDS", "30")
)

# Max virtual address space bytes (512 MB default) — prevents fork-bombs and
# memory-hungry numpy/pandas operations from OOM-killing the host process.
SANDBOX_MEMORY_LIMIT_BYTES: int = int(
    os.getenv("SANDBOX_MEMORY_LIMIT_BYTES", str(512 * 1024 * 1024))
)

# ---------------------------------------------------------------------------
# Token budget reserve fraction (BUG 3 fix)
# ---------------------------------------------------------------------------
# Fraction of MAX_TOKENS_PER_RUN that must remain *after* context injection
# for the agent stages (Planner + Coder + Verifier).  If context consumes
# more than (1 - reserve), the schema is truncated and a warning is emitted.

CONTEXT_BUDGET_RESERVE_FRACTION: float = float(
    os.getenv("CONTEXT_BUDGET_RESERVE_FRACTION", "0.20")
)

# ---------------------------------------------------------------------------
# Session context management (ARCH-02)
# ---------------------------------------------------------------------------
# TTL after which an idle session context is evicted from memory (seconds)
SESSION_TTL_SECONDS: int = int(os.getenv("SESSION_TTL_SECONDS", "3600"))
# Maximum number of concurrent session contexts held in memory
MAX_SESSIONS: int = int(os.getenv("MAX_SESSIONS", "500"))
