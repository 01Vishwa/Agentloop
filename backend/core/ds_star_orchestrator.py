"""DsStarOrchestrator — the main DS-STAR agent loop controller.

Implements the full Analyze → Plan → Code → Execute → [Debug loop] → Verify
→ [Finalize] iterative cycle. Yields AgentEvent objects at each stage for
SSE streaming to the frontend.

Gap fixes applied:
- Lambda capture bug fixed: coro_factory args now captured at call-site via
  functools.partial / default-argument binding instead of closure.
- Complexity classification now uses a heuristic (file count + query keywords)
  rather than a pure file-count label.
- REMOVE_STEPS router action now wired to PlannerAgent.remove_steps_from().
- VerifierAgent now receives artifact_names from ExecutionResult.
- RouterAgent.route() now receives execution_output (tracebacks) [GAP-01].
- CodeExecutor.run() is now awaited (it became async in the executor fix).
- Per-round timing, SSE events, and retry logic unchanged.
- [DS-STAR v2] DebuggerAgent wired into execution failure path (max 3 retries).
- [DS-STAR v2] FinalizerAgent wired into post-verification success path.
- [DS-STAR v3 / GAP-02] Sequential PlannerAgent mode wired into round 2+ ADD_STEP
  path so each new exploratory step is conditioned on actual execution output.
- [DS-STAR v3 / GAP-03] Debugger now receives _schema_hints not raw description.
- [DS-STAR v3 / GAP-06] Debugger LLM call has asyncio.wait_for timeout.
"""

import functools
import logging
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import tenacity

from core.analyzer.file_analyzer import FileAnalyzerAgent
from core.coder.coder_agent import CoderAgent, _parse_columns_from_schema_hints
from core.debugger.debugger_agent import DebuggerAgent
from core.executor.code_executor import CodeExecutor, ExecutionResult, mime_for_artifact
from core.finalizer.finalizer_agent import FinalizerAgent
from core.planner.planner_agent import PlannerAgent
from core.router.router_agent import RouterAgent
from core.verifier.verifier_agent import VerifierAgent
from core.config import MAX_AGENT_ROUNDS, MAX_DEBUGGER_RETRIES, MAX_TOKENS_PER_RUN, CONTEXT_BUDGET_RESERVE_FRACTION
from core.token_tracker import TokenTracker

logger = logging.getLogger("uvicorn.info")


# ---------------------------------------------------------------------------
# SSE event helper
# ---------------------------------------------------------------------------

def _event(event_type: str, **payload: Any) -> Dict[str, Any]:
    """Constructs a typed agent event dict.

    Args:
        event_type: Event name (e.g. ``"analyzing"``, ``"planning"``).
        **payload: Arbitrary keyword arguments included in the event payload.

    Returns:
        Serialisable event dict.
    """
    return {"event": event_type, "payload": payload}


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def _make_retry_before_sleep(event_type: str, events_sink: List[Dict]) -> Any:
    """Builds a tenacity ``before_sleep`` callback that appends a retrying event.

    Args:
        event_type: Name of the agent being retried.
        events_sink: Mutable list; the callback appends events here.

    Returns:
        Callable accepted by ``tenacity.retry(before_sleep=...)``.
    """
    def _callback(retry_state: tenacity.RetryCallState) -> None:
        events_sink.append(_event(
            "retrying",
            agent=event_type,
            attempt=retry_state.attempt_number,
            message=(
                f"{event_type}: attempt {retry_state.attempt_number} failed — retrying…"
            ),
        ))
    return _callback


async def _with_retry(
    coro_factory,
    agent_name: str,
    pending_events: List[Dict],
    max_attempts: int = 3,
):
    """Runs ``coro_factory()`` with exponential-backoff retry.

    Args:
        coro_factory: Zero-arg callable returning a coroutine.
        agent_name: Human-readable agent label for retry SSE events.
        pending_events: Mutable list; retry events are appended here.
        max_attempts: Maximum number of attempts before re-raising.

    Returns:
        Result of the coroutine on success.

    Raises:
        Exception: If all attempts fail.
    """
    retryer = tenacity.AsyncRetrying(
        wait=tenacity.wait_exponential(multiplier=1, min=2, max=10),
        stop=tenacity.stop_after_attempt(max_attempts),
        retry=tenacity.retry_if_exception_type(Exception),
        before_sleep=_make_retry_before_sleep(agent_name, pending_events),
        reraise=True,
    )
    async for attempt in retryer:
        with attempt:
            return await coro_factory()


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------

def _ms_since(t0: float) -> int:
    """Returns elapsed milliseconds since ``t0`` (from ``time.monotonic()``)."""
    return int((time.monotonic() - t0) * 1000)


def _collect_available_columns(
    combined: Dict[str, Any],
    data_description: str,
) -> List[str]:
    """Best-effort extraction of available columns from context/description.

    Returns lines in the form ``"<file>: [col1, col2, ...]"``.
    """
    hints: List[str] = []
    for fname, doc in combined.items():
        if not isinstance(doc, dict):
            continue
        metadata = doc.get("metadata", {}) if isinstance(doc.get("metadata"), dict) else {}
        cols = metadata.get("columns")
        if not cols and isinstance(metadata.get("sample_rows"), list) and metadata["sample_rows"]:
            first = metadata["sample_rows"][0]
            if isinstance(first, dict):
                cols = list(first.keys())
        if not cols and isinstance(doc.get("columns"), list):
            cols = doc.get("columns")
        if cols:
            hints.append(f"{fname}: {cols}")

    if hints:
        return hints

    # Fallback: parse analyzer text — handles BOTH the static format
    # ("Columns     : [...]") and the LLM-generated script output.
    # GAP-05 fix: avoids re.DOTALL which crosses file boundaries.
    if data_description:
        pattern_cols = re.compile(r"Columns\s*:\s*(\[[^\]]+\])", re.IGNORECASE)
        for match in pattern_cols.finditer(data_description):
            cols_str = match.group(1).strip()
            # Avoid duplicate hint lines for identical schemas
            if cols_str and not any(cols_str in h for h in hints):
                hints.append(f"(detected): {cols_str}")

    return hints


# ---------------------------------------------------------------------------
# Complexity classifier
# ---------------------------------------------------------------------------

_ML_KEYWORDS = frozenset({
    "predict", "train", "classify", "regression", "model",
    "accuracy", "precision", "recall", "f1", "auc",
})
_VIZ_KEYWORDS = frozenset({
    "plot", "chart", "graph", "visualize", "visualise",
    "histogram", "bar chart", "pie chart", "scatter",
})


def _classify_complexity(file_count: int, query: str) -> str:
    """Classifies task difficulty more accurately than pure file-count.

    Args:
        file_count: Number of distinct data files in the context.
        query: The user's natural language query.

    Returns:
        ``"easy"`` or ``"hard"``.
    """
    lower = query.lower()
    words = set(lower.split())
    # Hard if multi-file OR if ML/complex analytics requested
    if file_count >= 2 or bool(words & _ML_KEYWORDS):
        return "hard"
    return "easy"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class DsStarOrchestrator:
    """Runs the full DS-STAR iterative loop and streams progress events.

    Attributes:
        analyzer: FileAnalyzerAgent for building data descriptions.
        planner: PlannerAgent for creating and mutating plans.
        coder: CoderAgent for generating Python scripts.
        executor: CodeExecutor for sandboxed code execution.
        debugger: DebuggerAgent for intercepting and repairing failures.
        verifier: VerifierAgent for judging plan sufficiency.
        router: RouterAgent for deciding plan mutations.
        finalizer: FinalizerAgent for formatting verified outputs.
    """

    def __init__(
        self,
        max_rounds: Optional[int] = None,
        model: Optional[str] = None,
        coder_model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> None:
        self._max_rounds = max_rounds or MAX_AGENT_ROUNDS
        self._max_debugger_retries = MAX_DEBUGGER_RETRIES
        self.analyzer = FileAnalyzerAgent()
        self.planner = PlannerAgent(model=model, temperature=temperature)
        self.coder = CoderAgent(model=coder_model, temperature=temperature)
        self.executor = CodeExecutor()
        self.debugger = DebuggerAgent(model=model, temperature=temperature)
        self.verifier = VerifierAgent(model=model, temperature=temperature)
        self.router = RouterAgent(model=model, temperature=temperature)
        self.finalizer = FinalizerAgent(temperature=temperature)

    async def run(
        self,
        query: str,
        context: Dict[str, Any],
        run_id: str = "",
        session_id: str = "__anon__",
        max_rounds: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Executes the DS-STAR loop and yields SSE events.

        Args:
            query: The user's natural language query.
            context: Processing context from /process endpoint,
                including ``combined_extractions`` and ``files_processed``.
            run_id: Unique run identifier for metrics (passed from controller).
            session_id: Client session identifier — used to scope file cache
                access so the executor only sees this session's uploaded files.
            max_rounds: Per-call round limit. When provided, overrides
                ``self._max_rounds`` for this invocation only so concurrent
                requests with different limits share the cached instance safely
                (P1-01 fix: no mutation of shared state).
            workspace_id: Optional workspace UUID. Forwarded to the executor
                so it can load files from disk when the session cache is empty.

        Yields:
            AgentEvent dicts for SSE streaming.
        """
        # Resolve effective max_rounds locally — never write to self._max_rounds
        _effective_max_rounds: int = max_rounds if max_rounds is not None else self._max_rounds
        run_t0 = time.monotonic()
        execution_logs: List[str] = []
        combined = context.get("combined_extractions", {})
        pending_retry_events: List[Dict] = []

        file_count = len(combined)
        complexity = _classify_complexity(file_count, query)

        # Token budget tracker for this run (Gap 4)
        token_tracker = TokenTracker(budget=MAX_TOKENS_PER_RUN)

        # ── Stage 1: File Analysis ────────────────────────────────────────────
        yield _event("analyzing", message="Analyzing data files…")
        analyzer_t0 = time.monotonic()
        try:
            data_description = await self.analyzer.analyze(combined, session_id=session_id)
            yield _event(
                "analysis_complete",
                message="Data analysis complete.",
                data_description=data_description,
            )
        except Exception as exc:  # pylint: disable=broad-except
            data_description = f"Data description unavailable: {exc}"
            yield _event("warning", message=f"File analysis error: {exc}")
        analyzer_ms = _ms_since(analyzer_t0)

        execution_logs.append(
            f"[FileAnalyzer] {len(data_description)} chars of data description generated."
        )

        # ── BUG 3 fix: Context token-budget guard ────────────────────────────
        # Estimate token consumption of the schema context using the standard
        # ~4 chars-per-token heuristic.  If injecting the full context would
        # leave less than CONTEXT_BUDGET_RESERVE_FRACTION of the total budget
        # for downstream agent stages (Planner, Coder, Verifier, etc.) we
        # truncate data_description to the max char count that fits within the
        # safe window and surface a user-facing SSE warning instead of silently
        # proceeding and then hitting the hard cap mid-run.
        _CHARS_PER_TOKEN = 4  # conservative heuristic for mixed code/text
        _total_budget = MAX_TOKENS_PER_RUN
        _reserved_tokens = int(_total_budget * CONTEXT_BUDGET_RESERVE_FRACTION)
        _context_token_estimate = len(data_description) // _CHARS_PER_TOKEN
        _safe_context_tokens = _total_budget - _reserved_tokens

        if _context_token_estimate > _safe_context_tokens:
            _max_chars = _safe_context_tokens * _CHARS_PER_TOKEN
            data_description = data_description[:_max_chars]
            _trunc_msg = (
                f"Schema context was too large ({_context_token_estimate:,} estimated tokens). "
                f"Truncated to {_safe_context_tokens:,} tokens to preserve a "
                f"{int(CONTEXT_BUDGET_RESERVE_FRACTION * 100)}% budget reserve "
                f"({_reserved_tokens:,} tokens) for agent stages."
            )
            execution_logs.append(f"[BudgetGuard] {_trunc_msg}")
            logger.warning("[Orchestrator] %s", _trunc_msg)
            yield _event(
                "context_truncated",
                message=_trunc_msg,
                original_tokens=_context_token_estimate,
                truncated_to_tokens=_safe_context_tokens,
                reserved_tokens=_reserved_tokens,
            )

        # ── Stage 2: Initial Plan ─────────────────────────────────────────────
        yield _event("planning", message="Creating initial analysis plan…")
        planner_t0 = time.monotonic()
        try:
            # Use functools.partial to avoid lambda capture bug
            plan_steps = await _with_retry(
                functools.partial(
                    self.planner.create_plan, query, data_description,
                    token_tracker=token_tracker,
                ),
                "PlannerAgent",
                pending_retry_events,
            )
            for ev in pending_retry_events:
                yield ev
            pending_retry_events.clear()
        except Exception as exc:  # pylint: disable=broad-except
            for ev in pending_retry_events:
                yield ev
            pending_retry_events.clear()
            plan_steps = [
                {
                    "index": 0,
                    "description": "Load and inspect available data.",
                    "status": "pending",
                },
                {
                    "index": 1,
                    "description": f"Answer the query: {query}",
                    "status": "pending",
                },
            ]
            yield _event("warning", message=f"Planner error (using fallback): {exc}")
        planner_ms = _ms_since(planner_t0)

        yield _event("plan_ready", message="Plan created.", steps=plan_steps)
        execution_logs.append(f"[Planner] Initial plan: {len(plan_steps)} steps.")

        # ── Stage 3: Iterative Loop ───────────────────────────────────────────
        current_code = ""
        last_exec_result: ExecutionResult = ExecutionResult("", "", 0)
        verification: Dict[str, Any] = {
            "is_sufficient": False,
            "reason": "",
            "confidence": 0.0,
        }
        rounds_completed = 0
        rounds_until_sufficient = 0
        consecutive_coder_schema_errors = 0
        coder_raw_mode_engaged = False  # idempotent guard for force_raw_completion()

        for round_num in range(1, _effective_max_rounds + 1):
            rounds_completed = round_num

            # ── Token budget check (Gap 4) ─────────────────────────────────
            if token_tracker.over_budget():
                yield _event(
                    "warning",
                    message=(
                        f"Token budget of {MAX_TOKENS_PER_RUN:,} tokens exceeded "
                        f"({token_tracker.total_tokens:,} used). Stopping early."
                    ),
                )
                break

            yield _event(
                "round_start",
                message=f"Round {round_num}/{_effective_max_rounds}",
                round=round_num,
                max_rounds=_effective_max_rounds,
            )

            # ── 3a. Code Generation ───────────────────────────────────────────
            yield _event(
                "coding",
                message=f"Round {round_num}: Generating Python code…",
                round=round_num,
            )
            coder_t0 = time.monotonic()

            # Capture loop variables explicitly to avoid closure capture bug
            _query = query
            _desc = data_description
            _steps = list(plan_steps)
            _prev_code = current_code
            _exec_out = last_exec_result.combined_output()
            _available_cols = _collect_available_columns(combined, data_description)
            _schema_hints = "\n".join(_available_cols) if _available_cols else "(unknown)"

            try:
                current_code = await _with_retry(
                    functools.partial(
                        self.coder.generate_code,
                        _query,
                        _desc,
                        _steps,
                        _prev_code,
                        _exec_out,
                        schema_hints=_schema_hints,
                        token_tracker=token_tracker,
                    ),
                    "CoderAgent",
                    pending_retry_events,
                )
                for ev in pending_retry_events:
                    yield ev
                pending_retry_events.clear()
                consecutive_coder_schema_errors = 0
                yield _event(
                    "code_ready",
                    message="Code generated.",
                    code=current_code,
                    round=round_num,
                )
            except Exception as exc:  # pylint: disable=broad-except
                for ev in pending_retry_events:
                    yield ev
                pending_retry_events.clear()
                exc_str = str(exc)
                execution_logs.append(
                    f"[Round {round_num}] Coder exhausted retries: {exc_str}"
                )

                # Fix 3 (P1): Detect schema/column mismatch errors and inject
                # a diagnostic hint so the error message is actionable.
                #
                # Priority 1: if any KNOWN column name from the uploaded data
                # appears inside the error string, this is always a schema
                # hallucination error — regardless of wrappers like
                # "Unknown Error" or "raw completion failed".
                #
                # Priority 2: fall back to keyword heuristics for errors that
                # don't mention a column name but still look schema-related.
                exc_lower = exc_str.lower()

                # --- Priority 1: column-name presence in error text ---
                _col_names_lower = {c.lower() for c in _parse_columns_from_schema_hints(_schema_hints)}
                _found_col = any(
                    col_name in exc_lower
                    for col_name in _col_names_lower
                    if len(col_name) >= 2  # skip single-char column names to avoid false positives
                )

                # --- Priority 2: keyword heuristics (legacy path) ---
                _keyword_match = (
                    ("not found" in exc_lower and "404" not in exc_lower and "page not found" not in exc_lower)
                    or "keyerror" in exc_lower
                    or "column" in exc_lower
                    or "field" in exc_lower
                )
                is_schema_err = _found_col or _keyword_match
                if is_schema_err:
                    consecutive_coder_schema_errors += 1
                    # Append real column names to the next exec_output so the
                    # Debugger / next Coder round has ground-truth schema context.
                    _col_hint = (
                        "\n\nSCHEMA HINT — Available columns from uploaded files:\n"
                        + _schema_hints
                    )
                    last_exec_result = ExecutionResult(
                        stdout="",
                        stderr=f"Schema mismatch: {exc_str}{_col_hint}",
                        returncode=1,
                    )
                    yield _event(
                        "warning",
                        message=(
                            f"CoderAgent schema mismatch after 3 attempts: {exc_str}"
                            f"{_col_hint}"
                        ),
                    )

                    # Push the Coder into sticky raw-completion mode so the
                    # next round skips the brittle structured-output path
                    # entirely. Idempotent: only fires once per run.
                    if not coder_raw_mode_engaged:
                        try:
                            self.coder.force_raw_completion(
                                reason=(
                                    f"schema mismatch round {round_num} "
                                    f"({exc_str[:80]})"
                                )
                            )
                            coder_raw_mode_engaged = True
                            yield _event(
                                "warning",
                                message=(
                                    "CoderAgent switched to raw-completion mode "
                                    "for the rest of this run."
                                ),
                            )
                        except Exception as flip_exc:  # pylint: disable=broad-except
                            logger.warning(
                                "[Orchestrator] force_raw_completion failed: %s",
                                flip_exc,
                            )

                    # Threshold raised back to 3: now that we correctly detect
                    # column-name errors inside "Unknown Error" wrappers, the
                    # first failure triggers force_raw_completion(), the second
                    # retries with the fresh raw chain, and only the third
                    # consecutive failure terminates the run.
                    if consecutive_coder_schema_errors >= 3:
                        yield _event(
                            "error",
                            message=(
                                "CoderAgent repeatedly failed with schema/tool mismatch. "
                                "Stopping early to avoid retry loop."
                            ),
                        )
                        return
                    continue
                else:
                    yield _event(
                        "error",
                        message=f"CoderAgent failed after 3 attempts: {exc_str}",
                    )
                    return

            # ── 3b. Code Execution + Debugger Loop ───────────────────────────
            yield _event(
                "executing",
                message=f"Round {round_num}: Executing generated code…",
                round=round_num,
            )
            debug_attempts = 0
            _code_to_run = current_code
            _total_exec_ms = 0  # ARCH-06: accumulate across all debug-loop iterations

            while True:
                _iter_t0 = time.monotonic()  # ARCH-06: time each attempt individually
                try:
                    last_exec_result = await self.executor.run(
                        _code_to_run,
                        session_id=session_id,
                        workspace_id=workspace_id,
                    )
                    _total_exec_ms += _ms_since(_iter_t0)
                    exec_summary = (
                        f"[Round {round_num}] Execution "
                        f"{'succeeded' if last_exec_result.success else 'failed'}."
                        f" stdout={len(last_exec_result.stdout)} chars,"
                        f" stderr={len(last_exec_result.stderr)} chars."
                    )
                    execution_logs.append(exec_summary)
                    yield _event(
                        "execution_result",
                        message=(
                            f"Round {round_num}: Execution "
                            f"{'succeeded' if last_exec_result.success else 'failed'}."
                        ),
                        stdout=last_exec_result.stdout[:2000],
                        stderr=last_exec_result.stderr[:500],
                        success=last_exec_result.success,
                        round=round_num,
                    )

                    # Emit artifact events
                    for fname, b64data in last_exec_result.artifacts.items():
                        yield _event(
                            "artifact",
                            name=fname,
                            data=b64data,
                            mime_type=mime_for_artifact(fname),
                            round=round_num,
                        )

                    # If execution failed and we have retries left, invoke Debugger
                    if not last_exec_result.success and debug_attempts < self._max_debugger_retries:
                        debug_attempts += 1
                        yield _event(
                            "debugging",
                            message=(
                                f"Round {round_num}: Debugger fixing error "
                                f"(attempt {debug_attempts}/{self._max_debugger_retries})…"
                            ),
                            round=round_num,
                            debug_attempt=debug_attempts,
                        )
                        debug_t0 = time.monotonic()
                        try:
                            debug_result = await self.debugger.debug(
                                traceback=last_exec_result.stderr,
                                code=_code_to_run,
                                plan_steps=list(plan_steps),
                                # GAP-03 fix: use structured schema hints (col names) not raw description
                                schema_context=(
                                    _schema_hints
                                    + ("\n\n" + data_description[:1500] if data_description else "")
                                ),
                                token_tracker=token_tracker,
                            )
                            _code_to_run = debug_result["corrected_code"]
                            current_code = _code_to_run
                            execution_logs.append(
                                f"[Round {round_num}] Debugger fix #{debug_attempts}: "
                                f"{debug_result['fix_summary']}"
                            )
                            yield _event(
                                "debug_applied",
                                message=(
                                    f"Round {round_num}: {debug_result['fix_summary']}"
                                ),
                                error_type=debug_result["error_type"],
                                fix_summary=debug_result["fix_summary"],
                                round=round_num,
                                debug_ms=_ms_since(debug_t0),
                            )
                            # Loop back to re-execute the corrected code
                            continue
                        except Exception as dbg_exc:  # pylint: disable=broad-except
                            execution_logs.append(
                                f"[Round {round_num}] Debugger error: {dbg_exc}"
                            )
                            yield _event(
                                "warning",
                                message=f"Debugger failed: {dbg_exc}",
                            )
                            break  # Fall through to verifier with failed result
                    else:
                        break  # Success or max debugger retries exhausted

                except Exception as exc:  # pylint: disable=broad-except
                    _total_exec_ms += _ms_since(_iter_t0)
                    last_exec_result = ExecutionResult("", str(exc), 1)
                    execution_logs.append(f"[Round {round_num}] Executor crash: {exc}")
                    yield _event("warning", message=f"Executor crash: {exc}")
                    break

            # ── 3c. Verification ──────────────────────────────────────────────
            yield _event(
                "verifying",
                message=f"Round {round_num}: Verifying plan sufficiency…",
                round=round_num,
            )
            verifier_t0 = time.monotonic()
            artifact_names = list(last_exec_result.artifacts.keys())

            _verify_query = query
            _verify_desc = data_description
            _verify_steps = list(plan_steps)
            _verify_code = current_code
            _verify_exec = last_exec_result.combined_output()
            _verify_artifacts = artifact_names

            try:
                verification = await _with_retry(
                    functools.partial(
                        self.verifier.verify,
                        _verify_query,
                        _verify_desc,
                        _verify_steps,
                        _verify_code,
                        _verify_exec,
                        _verify_artifacts,
                        token_tracker=token_tracker,
                    ),
                    "VerifierAgent",
                    pending_retry_events,
                )
                for ev in pending_retry_events:
                    yield ev
                pending_retry_events.clear()
                execution_logs.append(
                    f"[Round {round_num}] Verifier: sufficient={verification['is_sufficient']}, "
                    f"reason={verification['reason'][:80]}"
                )
                yield _event(
                    "verification_result",
                    message=(
                        f"Round {round_num}: "
                        f"{'Sufficient ✓' if verification['is_sufficient'] else 'Insufficient — refining…'}"
                    ),
                    is_sufficient=verification["is_sufficient"],
                    reason=verification["reason"],
                    confidence=verification.get("confidence", 0.5),
                    round=round_num,
                )
            except Exception as exc:  # pylint: disable=broad-except
                for ev in pending_retry_events:
                    yield ev
                pending_retry_events.clear()
                verification = {
                    "is_sufficient": False,
                    "reason": str(exc),
                    "confidence": 0.0,
                }
                execution_logs.append(
                    f"[Round {round_num}] Verifier exhausted retries: {exc}"
                )
                yield _event("warning", message=f"Verifier error: {exc}")

            if verification["is_sufficient"]:
                rounds_until_sufficient = round_num
                break  # Done ✓

            # ── 3d. Routing (only if insufficient) ────────────────────────────
            if round_num < self._max_rounds:
                yield _event(
                    "routing",
                    message=f"Round {round_num}: Deciding how to refine the plan…",
                    round=round_num,
                )
                router_t0 = time.monotonic()

                _route_query = query
                _route_steps = list(plan_steps)
                _route_reason = verification["reason"]
                _route_exec = last_exec_result.combined_output()

                try:
                    decision = await _with_retry(
                        functools.partial(
                            self.router.route,
                            _route_query,
                            _route_steps,
                            _route_reason,
                            execution_output=_route_exec,   # GAP-01 fix: was positional data_description
                            token_tracker=token_tracker,
                        ),
                        "RouterAgent",
                        pending_retry_events,
                    )
                    for ev in pending_retry_events:
                        yield ev
                    pending_retry_events.clear()

                    action = decision.get("action", "ADD_STEP")
                    step_index = decision.get("step_index")
                    remove_from = decision.get("remove_from_index")
                    new_step = decision.get("new_step", {})

                    if action == "FIX_STEP" and step_index is not None:
                        plan_steps = self.planner.fix_step(
                            plan_steps, step_index, new_step
                        )
                    elif action == "REMOVE_STEPS" and remove_from is not None:
                        plan_steps = self.planner.remove_steps_from(
                            plan_steps, remove_from, new_step
                        )
                    else:
                        # GAP-02 fix: ADD_STEP — use sequential PlannerAgent mode
                        # (rounds 2+) so the new step is grounded in actual
                        # execution output discovered so far.
                        # FIX_STEP / REMOVE_STEPS are corrective mutations and
                        # continue to use the Router-provided step verbatim.
                        if round_num > 1:
                            seq_planner_events: List[Dict] = []
                            try:
                                _seq_steps = await _with_retry(
                                    functools.partial(
                                        self.planner.create_plan,
                                        query,
                                        data_description,
                                        token_tracker=token_tracker,
                                        execution_output=last_exec_result.combined_output(),
                                        completed_steps=list(plan_steps),
                                    ),
                                    "PlannerAgent(sequential)",
                                    seq_planner_events,
                                )
                                for ev in seq_planner_events:
                                    yield ev
                                # Sequential mode returns exactly 1 step;
                                # wrap it as the new_step for add_step().
                                if _seq_steps:
                                    new_step = _seq_steps[0]
                                    execution_logs.append(
                                        f"[Round {round_num}] Sequential planner: "
                                        f"{new_step.get('description', '')[:80]}"
                                    )
                                    yield _event(
                                        "planning",
                                        message=(
                                            f"Round {round_num}: Sequential planner "
                                            f"conditioned next step on execution output."
                                        ),
                                        round=round_num,
                                    )
                            except Exception as seq_exc:  # pylint: disable=broad-except
                                logger.warning(
                                    "[Orchestrator] Sequential planner failed (%s); "
                                    "using Router's new_step as fallback.",
                                    seq_exc,
                                )
                                for ev in seq_planner_events:
                                    yield ev
                        plan_steps = self.planner.add_step(plan_steps, new_step)

                    execution_logs.append(
                        f"[Round {round_num}] Router: {action}, "
                        f"new plan has {len(plan_steps)} steps."
                    )
                    yield _event(
                        "plan_updated",
                        message=f"Round {round_num}: Plan updated ({action}).",
                        steps=plan_steps,
                        action=action,
                        round=round_num,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    for ev in pending_retry_events:
                        yield ev
                    pending_retry_events.clear()
                    execution_logs.append(
                        f"[Round {round_num}] Router error: {exc}"
                    )
                    yield _event("warning", message=f"Router error: {exc}")

        # ── Stage 4: Evaluation Metrics ───────────────────────────────────────
        total_run_ms = _ms_since(run_t0)
        # ── Stage 5: Finalize ─────────────────────────────────────────────────
        artifact_names_final = list(last_exec_result.artifacts.keys())

        # Invoke FinalizerAgent only when Verifier approved the output
        finalized_output: Optional[Dict[str, Any]] = None
        if verification.get("is_sufficient"):
            yield _event("finalizing", message="Formatting verified output…")
            try:
                finalized_output = await self.finalizer.finalize(
                    query=query,
                    execution_output=last_exec_result.stdout,
                    plan_steps=plan_steps,
                    artifact_names=artifact_names_final,
                    token_tracker=token_tracker,
                )
                execution_logs.append(
                    f"[Finalizer] confidence={finalized_output.get('confidence', 0):.2f}"
                )
                yield _event(
                    "finalized",
                    message="Output formatted by Finalizer.",
                    headline=finalized_output.get("headline", ""),
                    formatted_output=finalized_output.get("formatted_output", ""),
                    confidence=finalized_output.get("confidence", 1.0),
                )
            except Exception as fin_exc:  # pylint: disable=broad-except
                logger.warning("[Orchestrator] Finalizer error: %s", fin_exc)
                yield _event("warning", message=f"Finalizer skipped: {fin_exc}")

        insights = _build_insights(
            query=query,
            plan_steps=plan_steps,
            execution_output=last_exec_result.stdout or last_exec_result.stderr,
            rounds=rounds_completed,
            verified=verification.get("is_sufficient", False),
            artifact_names=artifact_names_final,
            finalized_output=finalized_output,
        )

        final_result = {
            "insights": insights,
            "code": {"Python": current_code},
            "plan_steps": plan_steps,
            "rounds": rounds_completed,
            "execution_logs": execution_logs,
        }

        yield _event(
            "completed",
            message=f"DS-STAR analysis complete in {rounds_completed} round(s).",
            **final_result,
        )

        logger.info(
            "[Orchestrator] Completed — run_id=%s rounds=%d, steps=%d, "
            "code=%d chars, complexity=%s, total_ms=%d",
            run_id,
            rounds_completed,
            len(plan_steps),
            len(current_code),
            complexity,
            total_run_ms,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_insights(
    query: str,
    plan_steps: List[Dict[str, Any]],
    execution_output: str,
    rounds: int,
    verified: bool,
    artifact_names: Optional[List[str]] = None,
    finalized_output: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Builds a narrative insights dict from the agent run result.

    Args:
        query: Original user query.
        plan_steps: Final plan steps.
        execution_output: The stdout from the last execution.
        rounds: Number of rounds completed.
        verified: Whether the verifier approved the final plan.
        artifact_names: List of artifact filenames written to ./outputs/ (if any).
        finalized_output: Optional structured dict from FinalizerAgent
            containing ``headline`` and ``formatted_output`` keys.

    Returns:
        Insights dict with ``summary`` (str) and ``bullets`` (List[str]).
    """
    artifact_names = artifact_names or []
    image_artifacts = [
        n for n in artifact_names
        if n.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".html"))
    ]

    # Prefer FinalizerAgent's formatted output when verified
    if finalized_output and finalized_output.get("formatted_output"):
        summary = finalized_output["formatted_output"]
    elif execution_output and len(execution_output.strip()) > 10:
        summary = execution_output.strip()[:1200]
    elif image_artifacts:
        names = ", ".join(image_artifacts)
        summary = (
            f'DS-STAR generated {len(image_artifacts)} chart(s) for your query: "{query}". '
            f"Artifact(s) produced: {names}. "
            f"The agent completed in {rounds} refinement round(s)."
        )
    else:
        summary = (
            f'DS-STAR completed the analysis for your query: "{query}". '
            f"The agent ran {rounds} refinement round(s) and produced the code above."
        )

    bullets: List[str] = []
    if finalized_output and finalized_output.get("headline"):
        bullets.append(f"Answer: {finalized_output['headline']}")
    bullets.extend([
        f"Agent completed in {rounds} round(s)",
        f"Plan steps executed: {len(plan_steps)}",
        f"Verification status: {'✓ Approved' if verified else '⚠ Max rounds reached'}",
    ])
    if image_artifacts:
        bullets.append(f"Charts generated: {', '.join(image_artifacts)}")
    for step in plan_steps:
        bullets.append(f"• Step {step['index'] + 1}: {step['description']}")

    return {"summary": summary, "bullets": bullets}

