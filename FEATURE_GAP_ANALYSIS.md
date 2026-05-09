# TASK 5 — Feature Completeness & Implementation Gap Analysis

## Feature Gap Matrix

| Feature | Status | Location | What's Missing | Effort |
|---|---|---|---|---|
| **FinalizerAgent** | Redundant | `backend/core/finalizer/finalizer_agent.py` | Should be removed or repurposed. The Router/Verifier handle completion and correctness. Output formatting can be a simple synchronous function or integrated into the Router to save tokens and latency. | Low |
| **Collaborative Workspaces** | Incomplete | `backend/api/routes.py`, DB Schema | True 1:N team sharing. Needs `workspace_members` join table, RBAC policies, and member management APIs. | High |
| **Docker-by-Default Executor** | Partial | `backend/core/executor/code_executor.py` | `DOCKER_SANDBOX_ENABLED` needs to default to `True`, with explicit fallback warnings. | Low |
| **Batch Processing / Scheduling** | Missing | `backend/api/routes.py` | Celery/Redis integration for long-running open-ended research tasks (`/agent/run`). | High |
| **Frontend UX Polish** | Partial | `src/components/` | Drag-and-drop previews, dark mode toggle, skeleton loaders, advanced error toasts. | Medium |
| **Prompt Observability** | Missing | `backend/core/llm_client.py` | Intermediate LLM prompt/response logging for debugging hallucinations. | Medium |

---

## Specific Area Assessments

### 1. FinalizerAgent
- **Assessment**: It is truly redundant. The `Verifier` already ensures output sufficiency, and the `Router` handles control flow. The `FinalizerAgent` simply takes the output and formats it via a fast LLM.
- **Action**: **Remove it.** It adds unnecessary latency and token costs. Output formatting should be handled by a simpler static formatter or by the UI.

### 2. Collaborative Workspaces
- **Delta**: The current model assumes a strict 1:1 relationship (`workspace_id` mapped strictly to a single `user_id` ownership). Full team sharing requires a many-to-many relationship.
- **Required API Endpoints**:
  - `POST /workspaces/{id}/members` (Invite user)
  - `DELETE /workspaces/{id}/members/{user_id}` (Remove user)
  - `PUT /workspaces/{id}/members/{user_id}/role` (Update permissions: viewer/editor)
- **DB Schema Changes**:
  - Add `workspace_members` table (`workspace_id`, `user_id`, `role`).
  - Update RLS (Row Level Security) policies in Supabase to check membership roles instead of just `workspace.user_id == auth.uid()`.
- **Frontend Components**:
  - `WorkspaceMembersPanel.jsx`: UI for managing team members.
  - `WorkspaceShareModal.jsx`: Modal for generating invite links or adding emails.

### 3. Docker-by-Default for CodeExecutor
- **Toggle Needed**: Set `DOCKER_SANDBOX_ENABLED = True` by default in `core.config.py`.
- **Updated Initialization Logic**:
  Modify the executor initialization to proactively check Docker availability on startup, falling back securely if not present:
  ```python
  class CodeExecutor:
      def __init__(self):
          self.use_docker = DOCKER_SANDBOX_ENABLED
          if self.use_docker:
              try:
                  import docker
                  client = docker.from_env()
                  client.ping()
              except Exception as e:
                  logger.warning(f"Docker unavailable, falling back to subprocess: {e}")
                  self.use_docker = False

      async def run(self, code: str, session_id: str = "__anon__") -> ExecutionResult:
          loop = asyncio.get_event_loop()
          if self.use_docker:
              return await loop.run_in_executor(None, self._run_in_docker, code, session_id)
          return await loop.run_in_executor(None, self._run_sync, code, session_id)
  ```

### 4. Batch Processing / Scheduling
- **Celery Worker Integration**: Setup a Celery worker pool backed by Redis. The orchestrator logic (especially Deep Research) should be wrapped in a `@celery.task`.
- **Routes to Convert**:
  - `POST /agent/run` (when `is_open_ended=True`): Instead of holding the SSE connection open for 10+ minutes, return a `task_id`.
  - `POST /process`: For large document batches.
- **New Pattern**: Client polls `GET /agent/task/{task_id}` or listens via WebSocket/SSE to a dedicated task progress stream.

### 5. Frontend UX Gaps
- **Drag-and-Drop File Preview**:
  - **File**: `src/components/DropZone.jsx` / `FileList.jsx`
  - **Implementation**: Map over accepted files and use `URL.createObjectURL(file)` to render an `<img src>` for images or an icon for PDFs/CSV before uploading.
- **Dark Mode Toggle**:
  - **File**: `src/App.jsx` or a new `ThemeToggle.jsx`
  - **Implementation**: Add a button that toggles a `"dark"` class on `document.documentElement` and persists the state to `localStorage`.
- **Loading Skeletons**:
  - **File**: `src/components/agent/HistoryPanel.jsx`
  - **Implementation**: Replace the simple `"Loading history..."` text with: `<div className="animate-pulse h-12 bg-slate-200 rounded w-full mb-2"></div>` mapped 3-5 times.
- **Error Toasts**:
  - **File**: `src/components/Toast.jsx`
  - **Implementation**: Extend the `ToastItem` props to accept a `details` string or `action` callback. Add a dropdown/expand button in the toast to view stack traces or "Retry" action.

### 6. Prompt/LLM Observability
- **Current State**: Intermediate raw prompts and exact LLM JSON responses are not persistently logged, making hallucination debugging difficult.
- **Minimum Logging Hook**:
  Implement a LangChain `BaseCallbackHandler` in `core.llm_client.py` and attach it to the `get_structured_llm()` setup:
  ```python
  from langchain_core.callbacks import BaseCallbackHandler

  class PromptLoggerCallback(BaseCallbackHandler):
      def on_llm_start(self, serialized, prompts, **kwargs):
          logger.debug(f"[LLM Prompt]: {prompts}")
          
      def on_llm_end(self, response, **kwargs):
          logger.debug(f"[LLM Response]: {response.generations}")
  ```

---

## API Endpoint Mismatches (routes.py vs Frontend)

**Defined in Backend but NOT called by Frontend:**
- `GET /agent/runs/{run_id}/download` — Endpoint exists to download a ZIP of code and results, but there is no "Download" button in the frontend calling it.

**Called by Frontend but missing from `services/api.js` exports:**
- `GET /workspaces` and `POST /workspaces` are not centralized in `services/api.js` (likely called directly via inline `fetch` in store/components or not cleanly abstracted).
- `GET /agent/runs` and `GET /agent/runs/{run_id}` are called inline inside `useAgentRun.js` rather than via the `services/api.js` abstraction layer.
