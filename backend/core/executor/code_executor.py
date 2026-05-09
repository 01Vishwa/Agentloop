"""CodeExecutor — async sandboxed Python code runner.

Executes generated Python scripts in a temporary directory with a timeout.
Captures stdout, stderr, return code, and any artifact files written to
the ``./outputs/`` sub-directory.

Security fixes applied:
- Subprocess runs with a MINIMAL sanitised environment — no credentials leaked.
- Uses ``asyncio.run_in_executor`` to avoid blocking the FastAPI event loop.
- File cache accessed via session-scoped ``get_session_files()`` — no global
  singleton reads, preventing cross-user file leakage (Gap 1 fix).
- Optional Docker sandbox path controlled by DOCKER_SANDBOX_ENABLED env flag
  (Gap 2 fix).  FAIL-CLOSED: raises RuntimeError when Docker is enabled but
  unavailable — no subprocess fallback is permitted in production.

Artifact MIME types extended to cover image formats and ML model files.
"""

import asyncio
import base64
import logging
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict

from core.config import (
    DOCKER_CPU_QUOTA,
    DOCKER_MEMORY_LIMIT,
    DOCKER_SANDBOX_ENABLED,
    DOCKER_SANDBOX_IMAGE,
    EXECUTION_TIMEOUT_SECONDS,
)

logger = logging.getLogger("uvicorn.info")

_ANON_SESSION = "__anon__"

# ---------------------------------------------------------------------------
# Minimal allowed environment variables (credential-safe)
# ---------------------------------------------------------------------------

_SAFE_ENV_KEYS = frozenset({
    "PATH", "PYTHONPATH", "PYTHONHOME",
    "HOME", "USERPROFILE",
    "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP",
    "LANG", "LC_ALL", "LC_CTYPE",
    # Virtual-environment / Conda indicators — required so the subprocess
    # running sys.executable can resolve pip-installed packages (e.g. pandas,
    # matplotlib). Without these the sandbox gets ModuleNotFoundError even
    # though the packages ARE installed in the active environment.
    "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
})


def _safe_env() -> Dict[str, str]:
    """Returns a sanitised copy of os.environ with no credentials."""
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLBACKEND"] = "Agg"   # matplotlib non-interactive backend
    return env


# ---------------------------------------------------------------------------
# Extended MIME type map
# ---------------------------------------------------------------------------

_MIME_BY_EXT: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".json": "application/json",
    ".pkl": "application/octet-stream",
    ".joblib": "application/octet-stream",
    ".parquet": "application/octet-stream",
}


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

class ExecutionResult:
    """Result of a sandboxed code execution.

    Attributes:
        stdout: Captured standard output.
        stderr: Captured standard error.
        success: True if the process exited with code 0.
        returncode: The raw subprocess return code.
        artifacts: Dict mapping filename → base64-encoded content for every
            file written to the script's ``./outputs/`` directory.
    """

    def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.success = returncode == 0
        self.artifacts: Dict[str, str] = {}

    def combined_output(self) -> str:
        """Returns stdout and stderr concatenated for LLM consumption."""
        parts = []
        if self.stdout.strip():
            parts.append(f"[STDOUT]\n{self.stdout.strip()}")
        if self.stderr.strip():
            parts.append(f"[STDERR]\n{self.stderr.strip()}")
        if not parts:
            return "(no output)"
        return "\n\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """Serialises the result to a plain dictionary.

        Returns:
            Serialisable execution result (artifacts excluded for brevity).
        """
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "success": self.success,
            "returncode": self.returncode,
            "artifact_count": len(self.artifacts),
        }


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class CodeExecutor:
    """Runs Python scripts in a sandboxed subprocess with a strict timeout.

    Creates an ``outputs/`` sub-directory inside the temp working directory
    so generated code can write plots and CSVs there.  After execution, all
    files in ``outputs/`` are base64-encoded and attached to
    ``ExecutionResult.artifacts``.

    When ``DOCKER_SANDBOX_ENABLED=true`` the code is executed inside a Docker
    container with ``--network none`` and memory/CPU caps, providing stronger
    isolation than a bare subprocess.  FAIL-CLOSED: if Docker is enabled but
    the SDK is missing or the daemon is unreachable, a ``RuntimeError`` is
    raised immediately — no subprocess fallback is permitted.

    The execution body runs via ``asyncio.run_in_executor`` so it never blocks
    the FastAPI event loop.
    """

    async def run(
        self,
        code: str,
        session_id: str = _ANON_SESSION,
    ) -> ExecutionResult:
        """Writes code to a temp file and executes it asynchronously.

        Args:
            code: Python source code to execute.
            session_id: Session identifier used to fetch only that session's
                uploaded files into the sandbox (Gap 1 isolation fix).

        Returns:
            ExecutionResult with captured outputs and any artifacts.
        """
        loop = asyncio.get_running_loop()
        if DOCKER_SANDBOX_ENABLED:
            result = await loop.run_in_executor(
                None, self._run_in_docker, code, session_id
            )
        else:
            result = await loop.run_in_executor(
                None, self._run_sync, code, session_id
            )
        return result

    # ── Subprocess path (default / local dev) ────────────────────────────────

    def _run_sync(
        self,
        code: str,
        session_id: str = _ANON_SESSION,
    ) -> ExecutionResult:
        """Synchronous subprocess execution — runs in a thread pool worker.

        Args:
            code: Python source code to execute.
            session_id: Session whose uploaded files to inject into the sandbox.

        Returns:
            ExecutionResult with captured outputs.
        """
        from services.upload_service import get_session_files  # pylint: disable=import-outside-toplevel

        with tempfile.TemporaryDirectory() as tmpdir:
            # Pre-create outputs/ so generated code can always write there
            outputs_dir = os.path.join(tmpdir, "outputs")
            os.makedirs(outputs_dir, exist_ok=True)

            # Write only THIS session's files into the sandbox tmpdir.
            # get_session_files() returns a snapshot copy — safe to iterate.
            for filename, content in get_session_files(session_id).items():
                file_path = os.path.join(tmpdir, filename)
                try:
                    with open(file_path, "wb") as fh:
                        fh.write(content)
                except Exception as exc:  # pylint: disable=broad-except
                    logger.warning(
                        "[Executor] Could not write cached file %s: %s", filename, exc
                    )

            # Record files present before execution
            exclude_files = {"ds_star_script.py"}
            for root, _, files in os.walk(tmpdir):
                for fname in files:
                    exclude_files.add(os.path.relpath(os.path.join(root, fname), tmpdir))

            script_path = os.path.join(tmpdir, "ds_star_script.py")
            # Prepend a sys.path injection so the subprocess inherits the
            # exact same package search paths as the parent FastAPI process.
            # This is the definitive fix for ModuleNotFoundError (pandas, etc.)
            # when running inside a virtualenv or conda environment.
            sys_path_preamble = (
                "import sys as _sys\n"
                f"_sys.path[:0] = {sys.path!r}\n\n"
            )
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(sys_path_preamble)
                fh.write(code)

            session_file_count = len(get_session_files(session_id))
            logger.info(
                "[Executor] Running script (%d chars) in %s | session=%s | files=%d",
                len(code),
                tmpdir,
                session_id,
                session_file_count,
            )

            try:
                proc = subprocess.run(
                    [sys.executable, script_path],
                    capture_output=True,
                    text=True,
                    timeout=EXECUTION_TIMEOUT_SECONDS,
                    cwd=tmpdir,
                    env=_safe_env(),   # SANITISED environment — no credentials
                )
                result = ExecutionResult(
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    returncode=proc.returncode,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "[Executor] Script timed out after %ds.", EXECUTION_TIMEOUT_SECONDS
                )
                result = ExecutionResult(
                    stdout="",
                    stderr=(
                        f"Execution timed out after {EXECUTION_TIMEOUT_SECONDS} seconds."
                    ),
                    returncode=1,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("[Executor] Unexpected error: %s", exc)
                result = ExecutionResult(
                    stdout="",
                    stderr=f"Executor error: {str(exc)}",
                    returncode=1,
                )

            # Collect artifact files from tmpdir
            result.artifacts = _collect_artifacts(tmpdir, exclude_files)

            logger.info(
                "[Executor] Done — success=%s, stdout=%d chars, stderr=%d chars, artifacts=%d",
                result.success,
                len(result.stdout),
                len(result.stderr),
                len(result.artifacts),
            )
            return result

    # ── Docker path (production / DOCKER_SANDBOX_ENABLED=true) ───────────────

    def _run_in_docker(
        self,
        code: str,
        session_id: str = _ANON_SESSION,
    ) -> ExecutionResult:
        """Executes code inside a Docker container with network/resource limits.

        The container has:
          - ``--network none`` — no outbound internet access
          - mem_limit from ``DOCKER_MEMORY_LIMIT`` (default 512 MB)
          - nano_cpus from ``DOCKER_CPU_QUOTA`` (default 0.5 cores)

        Falls back transparently to ``_run_sync`` when Docker is unavailable
        so that local development environments without Docker Desktop are
        unaffected.

        Args:
            code: Python source code to execute.
            session_id: Session whose uploaded files to inject into the sandbox.

        Returns:
            ExecutionResult with captured outputs and any artifacts.
        """
        try:
            import docker  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            raise RuntimeError(
                "[Executor] FAIL-CLOSED: docker SDK not installed but "
                "DOCKER_SANDBOX_ENABLED=true. Run: pip install docker"
            ) from exc

        from services.upload_service import get_session_files  # pylint: disable=import-outside-toplevel

        try:
            client = docker.from_env(timeout=10)
        except Exception as exc:  # pylint: disable=broad-except
            raise RuntimeError(
                f"[Executor] FAIL-CLOSED: Docker daemon unreachable ({exc}). "
                "Ensure Docker is running or set DOCKER_SANDBOX_ENABLED=false for local dev."
            ) from exc

        with tempfile.TemporaryDirectory() as tmpdir:
            outputs_dir = os.path.join(tmpdir, "outputs")
            os.makedirs(outputs_dir, exist_ok=True)

            # Write session files to tmpdir so Docker can mount them
            for filename, content in get_session_files(session_id).items():
                with open(os.path.join(tmpdir, filename), "wb") as fh:
                    fh.write(content)

            # Record files present before execution
            exclude_files = {"ds_star_script.py"}
            for root, _, files in os.walk(tmpdir):
                for fname in files:
                    exclude_files.add(os.path.relpath(os.path.join(root, fname), tmpdir))

            sys_path_preamble = (
                "import sys as _sys\n"
                f"_sys.path[:0] = {sys.path!r}\n\n"
            )
            script_path = os.path.join(tmpdir, "ds_star_script.py")
            with open(script_path, "w", encoding="utf-8") as fh:
                fh.write(sys_path_preamble)
                fh.write(code)

            logger.info(
                "[Executor] Running in Docker | image=%s | session=%s",
                DOCKER_SANDBOX_IMAGE,
                session_id,
            )

            try:
                output = client.containers.run(
                    image=DOCKER_SANDBOX_IMAGE,
                    command=["python", "/workspace/ds_star_script.py"],
                    volumes={tmpdir: {"bind": "/workspace", "mode": "rw"}},
                    working_dir="/workspace",
                    network_mode="none",
                    mem_limit=DOCKER_MEMORY_LIMIT,
                    nano_cpus=int(DOCKER_CPU_QUOTA * 1e9),
                    environment={"MPLBACKEND": "Agg"},
                    remove=True,
                    stdout=True,
                    stderr=True,
                    detach=False,
                )
                stdout = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
                stderr = ""
                returncode = 0
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("[Executor] Docker run error: %s", exc)
                stdout = ""
                stderr = str(exc)
                returncode = 1

            result = ExecutionResult(stdout=stdout, stderr=stderr, returncode=returncode)
            result.artifacts = _collect_artifacts(tmpdir, exclude_files)

            logger.info(
                "[Executor] Docker done — success=%s, artifacts=%d",
                result.success,
                len(result.artifacts),
            )
            return result


# ---------------------------------------------------------------------------
# Artifact collection
# ---------------------------------------------------------------------------

def _collect_artifacts(base_dir: str, exclude_files: set) -> Dict[str, str]:
    """Reads every new file in ``base_dir`` and base64-encodes it.

    Args:
        base_dir: Absolute path to the working directory.
        exclude_files: Set of relative file paths to ignore.

    Returns:
        Dict mapping filename (no path) → base64-encoded string.
    """
    artifacts: Dict[str, str] = {}
    if not os.path.isdir(base_dir):
        return artifacts

    for root, _, files in os.walk(base_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            relpath = os.path.relpath(fpath, base_dir)
            if relpath in exclude_files:
                continue
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath, "rb") as fh:
                    # Most callers expect artifact keys to be just the filename
                    # (not "outputs/<name>"). Normalize to basename while keeping
                    # path separators out of the key.
                    safe_name = os.path.basename(relpath).replace(os.sep, "/")
                    artifacts[safe_name] = base64.b64encode(fh.read()).decode("utf-8")
                logger.info("[Executor] Collected artifact: %s", safe_name)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning(
                    "[Executor] Could not read artifact %s: %s", safe_name, exc
                )

    return artifacts


def mime_for_artifact(filename: str) -> str:
    """Returns the MIME type for an artifact filename.

    Args:
        filename: The artifact filename.

    Returns:
        MIME type string, defaulting to ``application/octet-stream``.
    """
    _, ext = os.path.splitext(filename.lower())
    return _MIME_BY_EXT.get(ext, "application/octet-stream")
