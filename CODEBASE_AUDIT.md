# Comprehensive Codebase Audit: Agentloop (DS-STAR Platform)

## 1. 🗂 ARCHITECTURE OVERVIEW

### High-Level Structure
Agentloop is a modern, full-stack application structured around a React 19 SPA frontend and an asynchronous FastAPI (Python 3.10+) backend. The architecture centers around a multi-agent orchestration framework (DS-STAR and DS-STAR+) that autonomously resolves data-driven queries.

### Modules & Components
*   **Frontend (React 19 + Vite)**: Uses Zustand for state, Tailwind CSS for styling, and standard HTTP/SSE for backend communication. Modules include `App.jsx`, `pages/`, `components/agent/`, `contexts/AuthContext.jsx`.
*   **Backend API (FastAPI)**: Thin routing layer (`backend/api/routes.py`), JWT Auth Middleware (`backend/middleware/auth.py`), and Exception handling.
*   **Orchestrator Layer**: `DsStarOrchestrator` (`backend/core/ds_star_orchestrator.py`) for single queries and `DeepResearchOrchestrator` for parallelized open-ended research.
*   **Agent Swarm**: Specific modules under `backend/core/` (e.g., `planner/`, `coder/`, `verifier/`, `router/`) built on LangChain.
*   **Sandbox**: `CodeExecutor` (`backend/core/executor/code_executor.py`) that runs untrusted LLM-generated Python.
*   **Storage & DB (Supabase)**: `supabase_service.py` handles asynchronous operations mapped to PostgreSQL via Row-Level Security (RLS).

### Component Interactions
1.  User authenticates via Supabase on the Frontend; receives JWT.
2.  Frontend uploads files to `/api/upload` and initiates a query to `/api/agent/run` (or `/api/research`), passing the JWT.
3.  FastAPI validates the JWT and delegates to the Orchestrator.
4.  The Orchestrator loops through Agents (Plan $\rightarrow$ Code $\rightarrow$ Execute $\rightarrow$ Verify $\rightarrow$ Route).
5.  Agents generate code, which the `CodeExecutor` runs in a sandboxed subprocess.
6.  Results, logs, and artifacts are streamed back to the client via Server-Sent Events (SSE).
7.  Final metrics and execution traces are persisted asynchronously to Supabase.

### Design Patterns
*   **Multi-Agent State Machine**: Orchestrated loop handling discrete tasks.
*   **Async-First**: FastAPI, `asyncio`, and `run_in_executor` for DB blocking calls.
*   **Structured Output Parsing**: Use of LangChain's `.with_structured_output()` to enforce Pydantic schemas.
*   **Event-Driven Streaming**: Generator functions yielding JSON-line SSEs.
*   **In-Memory Session Caching**: `_session_contexts` isolating user file uploads temporarily.

---

## 2. 🎯 CORE OBJECTIVE

### Primary Problem
Agentloop solves the problem of manual, time-consuming data analysis and research. It bridges the gap between unstructured data/queries and executable code, allowing users to automate complex Intelligent Document Processing (IDP) and deep research without writing a single line of code.

### Intended End-to-End Workflow
1.  **Ingestion**: User uploads datasets (CSV, PDF, JSON).
2.  **Contextualization**: System analyzes and describes file schema/content.
3.  **Planning & Coding**: Agents autonomously draft a step-by-step plan and write the Python logic.
4.  **Execution**: The code runs locally on the data.
5.  **Verification**: The system checks if the executed code produced the required insight.
6.  **Synthesis**: In DS-STAR+ mode, it merges outputs from parallel sub-questions into a cohesive markdown report.

### Target User
*   **Data Analysts & Researchers**: For rapid exploratory data analysis.
*   **Business Intelligence Professionals**: For automated reporting.
*   **No-Code Users**: To leverage Python's data stack (Pandas, matplotlib) via natural language.

---

## 3. ✅ WHAT IS DONE (Completed Work)

*   **`backend/main.py`**:
    *   FastAPI app instantiation, CORS middleware, global error handler wiring.
*   **`backend/api/routes.py`**:
    *   `POST /api/upload`: Session-scoped file caching.
    *   `POST /api/process`: Document parsing logic.
    *   `POST /api/agent/run`: Core SSE stream generator invoking `DsStarOrchestrator.run()`.
    *   `POST /api/research`: Deep research SSE generator.
    *   `GET /api/workspaces`, `GET /api/agent/runs`: Fully wired CRUD mapped to Supabase.
*   **`backend/middleware/auth.py`**:
    *   `get_current_user` JWT validation logic (HS256) enforcing endpoint protection.
*   **`backend/core/ds_star_orchestrator.py`**:
    *   The `run()` function perfectly implements the while-loop of Plan, Code, Execute, Verify, Route. Token tracking and max-round limits (default 10) are functional.
*   **`backend/core/deep_research_orchestrator.py`**:
    *   Implements `SubQuestionGeneratorAgent`, `asyncio.Semaphore(3)` for concurrent runs, and `ReportWriterAgent` synthesis.
*   **`backend/core/executor/code_executor.py`**:
    *   `CodeExecutor.run()` successfully spawns a subprocess with timeouts, capturing `stdout`, `stderr`, and filesystem artifacts from a hardcoded `./outputs/` directory.
*   **`backend/services/supabase_service.py`**:
    *   Asynchronous wrappers over `supabase-py` using thread pool executors to prevent event loop blocking. Handlers for `upload_to_storage`, `insert_file_record`, `create_agent_run`, etc., are fully defined.
*   **Frontend UI (`src/`)**:
    *   `AuthContext.jsx`: Supabase JS client integration.
    *   `components/agent/HistoryPanel.jsx` & `AgentProgressPanel.jsx`: Working real-time visualizers for SSE streams.

---

## 4. ⚙️ WHAT WORKS (Functional Assessment)

### Production-Ready Components
*   **JWT Authentication & Row-Level Security (RLS)**: The end-to-end security model from React to FastAPI to PostgreSQL works flawlessly. Users are strictly isolated.
*   **SSE Streaming Engine**: Emitting agent states and execution logs to the frontend works reliably, providing an excellent UX.
*   **Code Execution Engine**: The subprocess isolation correctly traps logic and returns standard output/errors without crashing the host API.

### Tests & Integrations
*   Unit and integration tests exist in `backend/tests/` (e.g., `test_auth_middleware.py`, `test_code_executor.py`, `test_orchestrator_integration.py`).
*   Supabase (Auth + DB + Storage) and LangChain (NVIDIA NIM) integrations are tightly wired and functional.

### Hidden Risks & Edge Cases
*   **Subprocess Isolation**: While the executor works, it relies on system subprocesses. This works for controlled deployments but is vulnerable to malicious code if Docker is not strictly enforced.
*   **Cache Eviction**: The `_session_contexts` relies on manual TTL eviction when new requests arrive. If an orchestrator crashes mid-stream, the context lingers until TTL.
*   **Structured Output Fragility**: The system leans heavily on LangChain's `.with_structured_output()`. Non-function-calling models (e.g., specific Mistral or Gemma variants) will cause silent verification failures if accidentally selected.

---

## 5. ❌ WHAT IS MISSING (Gap Analysis)

### Stubbed or Incomplete Features
*   **`FinalizerAgent`**: (`backend/core/finalizer/`) It is instantiated in the codebase and has logic, but the Router and Verifier already dictate task completion. It represents redundant code.
*   **Collaborative Workspaces**: DB schema allows for it, but the application enforces strict 1:1 user-to-workspace mapping. No team sharing logic exists.

### Absent Logic & Validation
*   **Query Length Validation**: Missing in `backend/api/routes.py` (`/api/agent/run`). A maliciously large query can trigger massive token usage and backend OOM errors.
*   **No Rate Limiting**: The FastAPI application entirely lacks IP or User-based rate limiting.
*   **Hardcoded Artifact Path**: `CodeExecutor` expects outputs specifically in `./outputs/` relative to execution. If an agent script writes to `./data/`, artifacts are silently ignored.
*   **Session Cleanup on Crash**: If the frontend disconnects abruptly or an unhandled exception occurs, there is no teardown mechanism for the in-memory `_session_contexts`.

### Error Handling & Documentation
*   **Broad Exception Catching**: In `backend/services/supabase_service.py` (e.g., line 219 `except Exception as exc:`), DB failures are swallowed and logged. The orchestrator continues unaware that persistence failed.
*   **Frontend Error Boundary**: React app lacks a global `ErrorBoundary`, meaning an unhandled state issue results in a white screen.
*   **Documentation**: Missing explicit documentation on how agents log their intermediate prompts.

---

## 6. 🚨 VULNERABILITY & TECHNICAL DEBT AUDIT (Severity Ranked)

This section maps all findings into strict severity categories based on security, reliability, and architectural risk.

### 🔴 CRITICAL (Fix Immediately - Blockers for Production)
1. **Remote Code Execution (RCE) via Subprocess Fallback**
   - **Component**: `backend/core/executor/code_executor.py`
   - **Risk**: If `DOCKER_SANDBOX_ENABLED=true` but the Docker daemon fails or is missing, the system silently falls back to `_run_sync` (host OS subprocess). Running untrusted, LLM-generated code directly on the host is a catastrophic RCE risk.
   - **Fix**: The executor must *fail-closed*. If Docker is enabled but unavailable, raise an exception and halt execution. Never fall back to host execution in production.
2. **Unbounded File Memory Leak (OOM Risk)**
   - **Component**: `backend/services/upload_service.py` (`_FILE_CACHE`)
   - **Risk**: Uploaded file bytes are stored indefinitely in `_FILE_CACHE`. While `_session_contexts` (in `routes.py`) has an eviction loop, `_FILE_CACHE` does not. A malicious or heavy user could upload gigabytes of files, never triggering the `/clear` endpoint, resulting in a backend Out-Of-Memory (OOM) crash.
   - **Fix**: Wire the `clear_file_cache` logic into the `_evict_stale_sessions` loop in `routes.py`, or implement an explicit TTL/LRU cache for `_FILE_CACHE`.
3. **Overly Permissive CORS Policy**
   - **Component**: `backend/main.py`
   - **Risk**: `allow_origins=["*"]` allows any domain to make cross-origin requests to the API. If cookies or local storage aren't perfectly secured, this opens the door to CSRF and hijacking.
   - **Fix**: Strictly bind `ALLOWED_ORIGINS` to the exact frontend domain (e.g., `["https://app.agentloop.ai"]`) and remove the `*` default.

### 🟠 HIGH (Fix Before Next Major Release)
1. **Silent Failures in Database Persistence**
   - **Component**: `backend/services/supabase_service.py`
   - **Risk**: Broad `except Exception as exc:` blocks catch DB write failures and return `{}` or empty lists (e.g., in `update_agent_run`). The orchestrator thinks the data was saved, but it's lost. The user UI will never reflect the failure.
   - **Fix**: Remove broad exception swallowing. Raise custom `DatabaseError` exceptions and let the FastAPI global error handler return a 500 status or emit a warning SSE event.
2. **Missing Rate Limiting & Query Length Constraints**
   - **Component**: `backend/api/routes.py` and FastAPI setup
   - **Risk**: No endpoint rate limiting exists. A user can spam `/api/agent/run` with massive context lengths, exhausting LLM tokens (billing risk) and locking up executor threads.
   - **Fix**: Implement `slowapi` or an Nginx rate limit. Add `max_length=5000` constraints to the `AgentRunRequest.query` model.

### 🟡 MEDIUM (Address as Technical Debt)
1. **Idempotency & State Recovery**
   - **Component**: `backend/core/ds_star_orchestrator.py`
   - **Risk**: If a network connection drops mid-run, the agent continues processing (costing LLM tokens), but the frontend loses the stream. There is no way to "re-attach" to a running session.
   - **Fix**: Detect `Request.is_disconnected()` in the SSE generator and cleanly cancel the underlying orchestrator task.
2. **JWT Secret Fallback Misconfiguration**
   - **Component**: `backend/middleware/auth.py`
   - **Risk**: The system supports both JWKS (ES256) and shared secret (HS256). If `SUPABASE_JWT_SECRET` is leaked, legacy tokens could be minted.
   - **Fix**: Force ES256 (JWKS) exclusively for modern Supabase projects and remove HS256 support unless absolutely necessary.
3. **Hardcoded Artifact Paths**
   - **Component**: `backend/core/executor/code_executor.py`
   - **Risk**: The executor only looks for artifacts in `./outputs/`. If the LLM generates a script that saves a plot to `./figures/`, the user will never see it.
   - **Fix**: Broaden artifact collection, or strictly prompt the LLM to *only* write to `./outputs/`.

### 🔵 LOW (Enhancements)
1. **Redundant Agent Modules**
   - **Component**: `backend/core/finalizer/`
   - **Risk**: Extra code to maintain. It is invoked but provides minimal value over the Verifier's output.
2. **Missing React Error Boundaries**
   - **Component**: `src/App.jsx`
   - **Risk**: Unhandled UI exceptions will crash the entire React tree, leaving a blank white screen.
   - **Fix**: Wrap `AppInner` with a standard `ErrorBoundary` component.

---

## 7. 🛡 AI SAFETY & TRUST DOMAIN AUDIT

*   **Transparency**: 
    *   ✅ *Good*: The SSE stream provides excellent visibility into the Python code and execution stdout.
    *   ❌ *Gap*: Raw LLM prompts/responses are hidden. If a model hallucinates, debugging the *why* is impossible without backend console access.
*   **Reliability**: 
    *   ✅ *Good*: The `TokenTracker` strictly guards against infinite loops.
    *   ❌ *Gap*: Single-point failure if the LLM cannot recover after 3 debug attempts.
*   **Groundedness**: 
    *   ✅ *Good*: Code execution ensures mathematical facts are empirically derived, not hallucinated.
*   **Auditability**: 
    *   ❌ *Gap*: No immutable audit logs exist for API access, posing a compliance risk for Enterprise/Gov deployments.

***End of Audit***
