# Data Layer & Persistence Review

## 1. Error Handling (`supabase_service.py`)

**Issue** | **File:Line** | **Risk** | **Fix**
--- | --- | --- | ---
Silent swallow of DB Exceptions | `supabase_service.py:219` (`create_agent_run`) | Silent data loss. Caller thinks run was created. | `raise DatabaseError(f"Failed to create run: {exc}") from exc`
Silent swallow of DB Exceptions | `supabase_service.py:269` (`update_agent_run`) | Run completes but data is never saved to history. | `raise DatabaseError(f"Failed to update run: {exc}") from exc`
Silent swallow of DB Exceptions | `supabase_service.py:296` (`get_agent_run`) | Caller gets empty dict instead of 404/500 error. | `raise DatabaseError(f"Failed to fetch run: {exc}") from exc`
Silent swallow of DB Exceptions | `supabase_service.py:335` (`list_agent_runs`) | User sees empty history instead of error state. | `raise DatabaseError(f"Failed to list runs: {exc}") from exc`
Silent swallow of DB Exceptions | `supabase_service.py:382` (`update_agent_run_metrics`) | Telemetry drops silently. | `raise DatabaseError(f"Failed to save metrics: {exc}") from exc`
Silent swallow of DB Exceptions | `supabase_service.py:431` (`create_report_run`) | DeepResearch runs drop out of history. | `raise DatabaseError(f"Failed to create report: {exc}") from exc`
Silent swallow of DB Exceptions | `supabase_service.py:455, 473` (`create_subquestions`) | Sub-questions vanish, stalling the planner loop. | `raise DatabaseError("Failed to create subquestions")`
Silent swallow of DB Exceptions | `supabase_service.py:502` (`link_subquestion_run`) | Final synthesis fails due to unlinked results. | `raise DatabaseError("Failed to link subquestion")`
Silent swallow of DB Exceptions | `supabase_service.py:547` (`update_report_status`) | Final research report is lost permanently. | `raise DatabaseError("Failed to update report")`
Silent swallow of DB Exceptions | `supabase_service.py:580` (`list_workspaces`) | Users see 0 workspaces instead of failure UI. | `raise DatabaseError("Failed to list workspaces")`

## 2. In-Memory Session State (`routes.py`)

**Issue** | **File:Line** | **Risk** | **Fix**
--- | --- | --- | ---
Horizontal Scaling Blocker | `api/routes.py:61` (`_session_contexts`) | If deploying multiple FastAPI workers/pods, a `/process` request hits Worker A, but the subsequent `/agent/run` hits Worker B. Worker B will throw a missing session error because the `dict` is bound to Worker A's memory. | Migrate session state to a **Redis-backed session store**. Use `redis.setex(session_id, 3600, json.dumps(context))` to ensure multi-node access.
Memory Leak on Crash | `api/routes.py:68` | Although there is a TTL eviction policy (3600s), if the orchestrator crashes or the client drops unexpectedly, the data lingers in memory for a full hour. Under heavy error loads, this is a severe memory leak. | Explicitly pop the session dict and call `clear_file_cache(session_id)` inside the `finally` or `except` block of `handle_agent_run` to free memory instantly on failure.

## 3. Async Correctness

**Issue** | **File:Line** | **Risk** | **Fix**
--- | --- | --- | ---
Synchronous DB calls in async functions | `supabase_service.py` (all functions) | None. All blocking Supabase calls are correctly wrapped in `asyncio.get_running_loop().run_in_executor(None, _sync)`. | N/A - Implementation is currently correct.
Synchronous HTTP request in FastAPI dependency | `middleware/auth.py:76` | `httpx.get()` is executed synchronously inside `_get_jwks_key`, which is called by the `async def get_current_user` dependency. Whenever the JWKS cache expires (or on first boot), this will completely block the FastAPI event loop for up to 5 seconds. | Use `httpx.AsyncClient().get()` and cascade `async/await` up through `_get_jwks_key` and `_decode_supabase_jwt`.

## 4. Audit Logging

**Issue** | **File:Line** | **Risk** | **Fix**
--- | --- | --- | ---
Missing Immutable Audit Trail | DB Schema | Enterprise compliance gap. There is no historical record of who executed which AI action, what files were analyzed, or when. | Create an `audit_logs` table (schema below) and add logging hooks to the Supabase Postgres triggers, or manually in `supabase_service.py`.

### Proposed Audit Log Schema
```sql
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES auth.users(id) NOT NULL,
    action VARCHAR(255) NOT NULL,          -- e.g., 'AGENT_RUN_CREATED', 'FILE_UPLOADED'
    resource_type VARCHAR(50) NOT NULL,    -- e.g., 'agent_run', 'workspace'
    resource_id UUID NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    ip_address INET,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

## 5. Data Schema Gaps

**Issue** | **File:Line** | **Risk** | **Fix**
--- | --- | --- | ---
1:1 User-to-Workspace Foreign Key Mapping | `supabase_service.py:602` | The `workspaces` table uses a direct `user_id` owner column. This explicitly prevents multi-tenant collaborative workspaces (many users sharing one workspace), which is a common enterprise requirement. | Remove `user_id` from the `workspaces` table. Introduce a `workspace_members` junction table with columns `(workspace_id, user_id, role)`.
