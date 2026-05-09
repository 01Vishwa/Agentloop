# Frontend ↔ Backend API Connection Audit

## 1. SSE Streaming (`/api/agent/run` and `/api/research`)

**Endpoint** | **Issue** | **User-Visible Impact** | **Fix**
--- | --- | --- | ---
`/api/agent/run` (Backend) | The `try...finally` block in `agent_controller.py` cleans up background tasks and eval loggers, but leaves `_session_contexts` dangling. It relies on a lazy 1-hour TTL eviction. | High memory consumption and potential OOM errors if many clients disconnect prematurely. | Explicitly pop the `session_id` from `_session_contexts` in the `finally` block of `handle_agent_run` when `disc_event.is_set()` is true.
`/api/agent/run` (Frontend) | `agentApi.js` lacks an exponential backoff/reconnection strategy. If the connection drops mid-stream, the stream aborts completely. Malformed JSON lines are silently swallowed. | Agent runs stall indefinitely on network drops without warning. Missing SSE chunks lead to confusing incomplete UI states. | Wrap `runAgentStream` in a retry loop with exponential backoff (e.g., using a last-received event ID). Log malformed JSON to `console.error`.

## 2. File Upload (`/api/upload`)

**Endpoint** | **Issue** | **User-Visible Impact** | **Fix**
--- | --- | --- | ---
`/api/upload` | MIME type is validated strictly via `python-magic` and max file size is enforced sequentially in 1MB chunks (FastAPI layer), but files are not sanitized until *parsing* time (`sanitize_text` in parsers). | Malicious payloads (e.g. scripts disguised as CSVs) can sit inside the application's memory `_FILE_CACHE` indefinitely. | Move `sanitize_text` (or an equivalent scanner) into `save_upload_file` so text files are sanitized sequentially during the chunk read process before caching.

## 3. Authentication Flow

**Endpoint** | **Issue** | **User-Visible Impact** | **Fix**
--- | --- | --- | ---
`middleware/auth.py` | JWT verification checks the unverified `alg` header and falls back to `HS256` symmetric decoding if requested. | **Security Downgrade:** Susceptible to an algorithm confusion attack where an attacker signs a payload using the public JWKS key as the HS256 secret. | Hardcode `algorithms=["RS256", "ES256"]` in `jwt.decode` and explicitly remove the HS256 fallback if the Supabase project uses asymmetric keys.

## 4. Error Propagation

**Endpoint** | **Issue** | **User-Visible Impact** | **Fix**
--- | --- | --- | ---
`/api/agent/run` | Supabase DB writes (`_try_update_run`) fail silently via an `except Exception as exc: logger.warning()` block. The SSE stream never sends an error event for persistence failures. | The frontend shows a triumphant "Completed" state, but the user's run history remains empty on reload because it was never saved. | If `_try_update_run` fails, yield an SSE `{"event": "error", "payload": {"message": "Failed to persist run"}}` before closing the stream.
Global React App | The React app does not have a global ErrorBoundary component to catch unhandled rendering exceptions. | The UI will crash to a blank white screen ("white screen of death") without recovery options if an unforeseen UI error occurs. | Implement a global `ErrorBoundary` component (code provided below).

## 5. Rate Limiting

**Endpoint** | **Issue** | **User-Visible Impact** | **Fix**
--- | --- | --- | ---
`/api/agent/run` | No `slowapi` or equivalent rate limiter is configured on the backend. | Malicious users can spam the `/api/agent/run` endpoint, exhausting LLM API credits and performing a Denial of Service (DoS). | Implement `slowapi` for endpoint-level rate limiting (code provided below).

---

### Implementation Details

#### React Error Boundary (Frontend Fix)
```jsx
// src/components/shared/ErrorBoundary.jsx
import React from 'react';

export class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="p-8 text-center max-w-md mx-auto mt-20 bg-white border border-slate-200 rounded-xl shadow-lg">
          <h2 className="text-xl font-bold text-red-600 mb-2">Something went wrong</h2>
          <p className="text-slate-600 text-sm font-mono mb-4">{this.state.error?.message}</p>
          <button 
            onClick={() => window.location.reload()} 
            className="px-4 py-2 bg-brand-500 text-white rounded-lg text-sm font-medium hover:bg-brand-600 transition"
          >
            Reload Application
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
```

#### SlowAPI Rate Limiter (Backend Fix)
```python
# In backend/main.py
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# In backend/api/routes.py
@router.post("/agent/run")
@limiter.limit("20/hour")
async def agent_run(
    request: AgentRunRequest,
    http_request: Request, # Explicitly required by slowapi
    auth: AuthUser = Depends(get_current_user),
):
    # Existing handler logic...
```
