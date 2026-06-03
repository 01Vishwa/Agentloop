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
- BUG 1 (HIGH) fix: _run_popen_capped() now runs with:
    * start_new_session=True so the entire child process group can be killed
      atomically on timeout via os.killpg, preventing orphaned processes.
    * _apply_resource_limits() as a preexec_fn that sets RLIMIT_CPU and
      RLIMIT_AS (Unix only) to prevent runaway CPU/memory consumption.
    * SandboxViolationError raised for any constraint breach, keeping raw
      OS/subprocess details away from the LLM error context.

Artifact MIME types extended to cover image formats and ML model files.
"""

import asyncio
import threading
import base64
import logging
import os
import signal
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional

from core.config import (
    DOCKER_CPU_QUOTA,
    DOCKER_MEMORY_LIMIT,
    DOCKER_SANDBOX_ENABLED,
    DOCKER_SANDBOX_IMAGE,
    EXECUTION_TIMEOUT_SECONDS,
    SANDBOX_CPU_TIME_LIMIT_SECONDS,
    SANDBOX_MEMORY_LIMIT_BYTES,
)

logger = logging.getLogger("uvicorn.info")

_ANON_SESSION = "__anon__"


# ---------------------------------------------------------------------------
# Custom exception for sandbox constraint violations  (BUG 1 fix)
# ---------------------------------------------------------------------------

class SandboxViolationError(RuntimeError):
    """Raised when a sandboxed script breaches a resource or path constraint.

    Keeping violations as a typed exception allows the orchestrator to handle
    them differently from ordinary execution errors (e.g. not retrying on RCE).
    """


# ---------------------------------------------------------------------------
# Resource-limit preexec helper  (BUG 1 fix — Unix only)
# ---------------------------------------------------------------------------

def _apply_resource_limits() -> None:  # pragma: no cover
    """Called by the child process before exec() to install resource caps.

    This function runs *inside the forked child* on Unix systems.  It sets:
      - RLIMIT_CPU: max CPU time in seconds (soft=limit, hard=limit+5)
      - RLIMIT_AS:  max virtual address space (soft=limit, hard=limit)

    On Windows ``resource`` is not available; we log a warning and continue
    so that the rest of the sandbox machinery (timeout kill, env sanitisation)
    still works.
    """
    try:
        import resource  # pylint: disable=import-outside-toplevel
        cpu_soft = SANDBOX_CPU_TIME_LIMIT_SECONDS
        cpu_hard = cpu_soft + 5  # kernel sends SIGXCPU at soft, SIGKILL at hard
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_soft, cpu_hard))

        mem = SANDBOX_MEMORY_LIMIT_BYTES
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    except ImportError:
        # Windows — resource limits via preexec_fn are not supported
        pass
    except Exception as exc:  # pylint: disable=broad-except
        # Swallow here — we are inside the child; a raised exception would
        # cause confusing spawn errors instead of a clean timeout kill.
        import sys as _sys  # pylint: disable=import-outside-toplevel
        print(f"[Sandbox] WARNING: resource limits not applied: {exc}", file=_sys.stderr)


# ---------------------------------------------------------------------------
# Output size cap (P1 OOM fix)
# ---------------------------------------------------------------------------

# Hard limit on captured stdout + stderr per execution.  A script that prints
# in an infinite loop (e.g. `while True: print("A" * 1024)`) would otherwise
# exhaust the host OS memory before the timeout fires, crashing the FastAPI
# thread pool.  2 MB per stream is generous for legitimate analytics output.
_MAX_OUTPUT_BYTES: int = 2 * 1024 * 1024  # 2 MB
_OUTPUT_TRUNCATED_SENTINEL = (
    "\n\n[TRUNCATED] Output exceeded 2 MB limit and was cut off."
)


def _read_capped(stream, max_bytes: int = _MAX_OUTPUT_BYTES) -> str:
    """Reads up to *max_bytes* from a binary stream and decodes to str.

    Designed to be called from a background thread concurrently with the other
    stream so neither pipe blocks the process (pipe-deadlock prevention).  Any
    data beyond the cap is silently drained so the child doesn't stall on a
    full pipe buffer.  A sentinel string is appended when truncation occurs so
    the LLM knows the output ended early rather than cleanly.

    Args:
        stream: A readable binary file-like object (subprocess PIPE).
        max_bytes: Maximum bytes to read before truncating.

    Returns:
        Decoded string, with a truncation notice when the cap was reached.
    """
    chunks: list = []
    total = 0
    truncated = False
    try:
        while True:
            chunk = stream.read(65536)  # 64 KB reads
            if not chunk:
                break
            remaining = max_bytes - total
            if remaining <= 0:
                truncated = True
                # Keep draining so the child doesn't block on a full pipe
                continue
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                total += remaining
                truncated = True
            else:
                chunks.append(chunk)
                total += len(chunk)
    except Exception:  # pylint: disable=broad-except
        pass
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if truncated:
        text += _OUTPUT_TRUNCATED_SENTINEL
        logger.warning(
            "[Executor] Output truncated at %d bytes — possible infinite print loop.",
            max_bytes,
        )
    return text


def _run_popen_capped(
    args: list,
    cwd: str,
    env: dict,
    timeout: int,
    allowed_cwd: str = "",
) -> "tuple[str, str, int]":
    """Runs a subprocess with concurrent capped pipe readers (deadlock-safe).

    BUG 1 (HIGH) fix — additional hardening over the original:
      * ``start_new_session=True`` puts the child in its own process group so
        that ``os.killpg`` can reliably kill the *entire* process tree on
        timeout, not just the top-level PID.
      * ``preexec_fn=_apply_resource_limits`` installs RLIMIT_CPU / RLIMIT_AS
        inside the forked child before exec (Unix only).
      * Path whitelist check: if ``allowed_cwd`` is provided, any attempt by
        the args list to reference a path outside it raises
        ``SandboxViolationError`` before the process is even spawned.
      * Timeout kill uses ``os.killpg`` (SIGKILL to the whole group) then
        ``proc.wait()`` to reap zombies; raw exceptions are wrapped in
        ``SandboxViolationError`` to prevent leaking OS internals to callers.

    Args:
        args: Command + arguments list for subprocess.
        cwd: Working directory for the subprocess.
        env: Environment variables dict.
        timeout: Wall-clock timeout in seconds.
        allowed_cwd: Optional absolute path prefix; any arg that is an
            absolute path outside this prefix raises SandboxViolationError.

    Returns:
        Tuple of (stdout_text, stderr_text, returncode).

    Raises:
        SandboxViolationError: On timeout, resource breach, or path violation.
    """
    # ── Path whitelist check (before spawn) ──────────────────────────────────
    if allowed_cwd:
        _allowed = os.path.realpath(allowed_cwd)
        for i, arg in enumerate(args):
            # Exempt the executable itself (args[0]) from the sandbox constraint
            if i == 0:
                continue
            if isinstance(arg, str) and os.path.isabs(arg):
                if not os.path.realpath(arg).startswith(_allowed):
                    raise SandboxViolationError(
                        f"Path constraint violated: '{arg}' is outside sandbox '{_allowed}'. "
                        "Execution aborted."
                    )

    # ── Spawn child in its own session (process group) ───────────────────────
    popen_kwargs: Dict[str, Any] = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    _is_unix = hasattr(os, "killpg")
    if _is_unix:
        # start_new_session=True creates a new process group leader so we
        # can send SIGKILL to every child spawned by the script.
        popen_kwargs["start_new_session"] = True
        popen_kwargs["preexec_fn"] = _apply_resource_limits

    try:
        proc = subprocess.Popen(args, **popen_kwargs)
    except OSError as exc:
        raise SandboxViolationError(
            f"Failed to spawn sandbox subprocess: {exc}"
        ) from exc

    stdout_holder: list = [""]
    stderr_holder: list = [""]

    t_out = threading.Thread(
        target=lambda: stdout_holder.__setitem__(0, _read_capped(proc.stdout)),
        daemon=True,
    )
    t_err = threading.Thread(
        target=lambda: stderr_holder.__setitem__(0, _read_capped(proc.stderr)),
        daemon=True,
    )
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill entire process group (Unix) or just the process (Windows)
        if _is_unix:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # Process already exited — nothing to kill
        else:
            proc.kill()
        proc.wait()  # reap zombie
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        raise SandboxViolationError(
            f"Execution timed out after {timeout} seconds. "
            "The process tree was killed."
        )

    t_out.join()
    t_err.join()
    return stdout_holder[0], stderr_holder[0], proc.returncode


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
    ".html": "text/html",
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
        workspace_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Writes code to a temp file and executes it asynchronously.

        Args:
            code: Python source code to execute.
            session_id: Session identifier used to fetch only that session's
                uploaded files into the sandbox (Gap 1 isolation fix).
            workspace_id: Optional workspace UUID. When the in-memory session
                cache is empty, files are loaded from the workspace disk
                directory as a fallback (fixes file-disappearance bug).

        Returns:
            ExecutionResult with captured outputs and any artifacts.
        """
        loop = asyncio.get_running_loop()
        if DOCKER_SANDBOX_ENABLED:
            result = await loop.run_in_executor(
                None, self._run_in_docker, code, session_id, workspace_id
            )
        else:
            result = await loop.run_in_executor(
                None, self._run_sync, code, session_id, workspace_id
            )
        return result

    # ── Subprocess path (default / local dev) ────────────────────────────────

    def _run_sync(
        self,
        code: str,
        session_id: str = _ANON_SESSION,
        workspace_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Synchronous subprocess execution — runs in a thread pool worker.

        Args:
            code: Python source code to execute.
            session_id: Session whose uploaded files to inject into the sandbox.
            workspace_id: Optional workspace UUID used as a disk-file fallback
                when the in-memory session cache is empty.

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
            session_files = get_session_files(session_id)

            # Workspace disk-file fallback: if the in-memory cache is empty
            # (e.g. server restart, or workspace-upload path keyed differently)
            # copy files from the workspace directory on disk.
            if not session_files and workspace_id:
                workspace_dir = os.path.join(
                    os.environ.get("WORKSPACE_FILES_DIR", "/workspace"),
                    workspace_id,
                )
                if os.path.isdir(workspace_dir):
                    for fname in os.listdir(workspace_dir):
                        fpath = os.path.join(workspace_dir, fname)
                        if os.path.isfile(fpath):
                            try:
                                with open(fpath, "rb") as fh:
                                    session_files[fname] = fh.read()
                            except Exception as exc:  # pylint: disable=broad-except
                                logger.warning(
                                    "[Executor] Could not read workspace file %s: %s",
                                    fname, exc,
                                )
                    if session_files:
                        logger.info(
                            "[Executor] Loaded %d file(s) from workspace disk fallback "
                            "(workspace_id=%s).",
                            len(session_files),
                            workspace_id,
                        )

            for filename, content in session_files.items():
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
                # BUG 1 fix: pass allowed_cwd=tmpdir so any absolute path
                # outside the temp sandbox is caught before process spawn.
                # _run_popen_capped now uses start_new_session + killpg for
                # reliable process-tree teardown, and raises SandboxViolationError
                # on timeout/constraint breach instead of raw exceptions.
                stdout_text, stderr_text, returncode = _run_popen_capped(
                    args=[sys.executable, script_path],
                    cwd=tmpdir,
                    env=_safe_env(),
                    timeout=EXECUTION_TIMEOUT_SECONDS,
                    allowed_cwd=tmpdir,
                )
                result = ExecutionResult(
                    stdout=stdout_text,
                    stderr=stderr_text,
                    returncode=returncode,
                )
            except SandboxViolationError as exc:
                # Surface sandbox breaches clearly — do NOT retry these
                logger.error("[Executor] Sandbox violation: %s", exc)
                result = ExecutionResult(
                    stdout="",
                    stderr=f"Sandbox violation: {exc}",
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
        workspace_id: Optional[str] = None,
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
            workspace_id: Optional workspace UUID; used as disk-file fallback
                when the in-memory session cache is empty.

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
            session_files = get_session_files(session_id)

            # Workspace disk-file fallback (mirrors _run_sync logic)
            if not session_files and workspace_id:
                workspace_dir = os.path.join(
                    os.environ.get("WORKSPACE_FILES_DIR", "/workspace"),
                    workspace_id,
                )
                if os.path.isdir(workspace_dir):
                    for fname in os.listdir(workspace_dir):
                        fpath = os.path.join(workspace_dir, fname)
                        if os.path.isfile(fpath):
                            try:
                                with open(fpath, "rb") as fh:
                                    session_files[fname] = fh.read()
                            except Exception as exc:  # pylint: disable=broad-except
                                logger.warning(
                                    "[Executor] Could not read workspace file %s: %s",
                                    fname, exc,
                                )

            for filename, content in session_files.items():
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
