/**
 * HistoryPanel.jsx — Collapsible past-run history drawer.
 *
 * Lists agent runs from Supabase scoped to the currently active project
 * (workspace). Clicking a row calls loadRun(id) to restore full state.
 *
 * Per-project isolation: history is fetched with workspace_id so each
 * project maintains a completely separate run history.
 */

import React, { useEffect, useState } from 'react'
import {
  History, ChevronDown, ChevronUp, CheckCircle2,
  XCircle, Loader2, Clock, RefreshCw, FolderKanban,
} from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspaceStore'

// ─── Status badge ────────────────────────────────────────────────────────────
function StatusBadge({ status }) {
  const map = {
    completed: {
      cls: 'bg-emerald-50 text-emerald-700 border-emerald-200',
      icon: <CheckCircle2 size={10} />,
      label: 'Completed',
    },
    failed: {
      cls: 'bg-red-50 text-red-700 border-red-200',
      icon: <XCircle size={10} />,
      label: 'Failed',
    },
    running: {
      cls: 'bg-blue-50 text-blue-700 border-blue-200',
      icon: <Loader2 size={10} className="animate-spin" />,
      label: 'Running',
    },
  }
  const meta = map[status] || map.failed
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full
                  text-[10px] font-bold border uppercase tracking-wider ${meta.cls}`}
    >
      {meta.icon}
      {meta.label}
    </span>
  )
}

// ─── Single run row ───────────────────────────────────────────────────────────
function RunRow({ run, onLoad, isLoading }) {
  const date = run.created_at
    ? new Date(run.created_at).toLocaleString(undefined, {
        dateStyle: 'short',
        timeStyle: 'short',
      })
    : '—'

  const querySnippet = (run.query || '')
  const snippet = querySnippet.length > 55
    ? querySnippet.slice(0, 55) + '…'
    : querySnippet

  return (
    <button
      onClick={() => onLoad(run.run_id || run.id)}
      disabled={isLoading}
      className="w-full text-left flex items-start gap-3 px-3 py-2.5
                 rounded-xl hover:bg-slate-50 transition-colors
                 border border-transparent hover:border-slate-200
                 disabled:opacity-50 group"
    >
      <div className="w-6 h-6 rounded-lg bg-brand-50 border border-brand-100
                      flex items-center justify-center shrink-0 mt-0.5">
        <Clock size={11} className="text-brand-500" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-[12px] font-semibold text-slate-700 leading-snug truncate
                      group-hover:text-slate-900">
          {snippet || 'Untitled run'}
        </p>
        <div className="flex items-center gap-2 mt-1">
          <span className="text-[10px] text-slate-400 font-medium">{date}</span>
          {run.rounds > 0 && (
            <span className="text-[10px] text-slate-400 font-medium">· {run.rounds} round(s)</span>
          )}
          <div className="ml-auto">
            <StatusBadge status={run.status} />
          </div>
        </div>
      </div>
    </button>
  )
}

// ─── Main component ───────────────────────────────────────────────────────────
export function HistoryPanel({ historyRuns, historyLoading, fetchHistory, loadRun }) {
  const [collapsed, setCollapsed] = useState(false)
  const [loadingId, setLoadingId] = useState(null)
  const { activeWorkspace } = useWorkspaceStore()

  // Fetch history when the component mounts OR when the active project changes.
  // force=true bypasses the 15s throttle so project switches show fresh history immediately.
  useEffect(() => {
    fetchHistory(20, true)
  }, [fetchHistory, activeWorkspace?.id])

  const handleLoad = async (runId) => {
    setLoadingId(runId)
    await loadRun(runId)
    setLoadingId(null)
  }

  return (
    <div className="glass-card-elevated overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between px-5 py-3.5
                   hover:bg-slate-50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="w-7 h-7 rounded-lg bg-slate-100 border border-slate-200
                          flex items-center justify-center">
            <History size={14} className="text-slate-500" />
          </div>
          <div className="text-left">
            <div className="flex items-center gap-2">
              <p className="text-[13px] font-bold text-slate-800">Run History</p>
              {activeWorkspace?.name && (
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md
                                 bg-brand-50 border border-brand-100 text-[10px] font-semibold
                                 text-brand-600 max-w-[120px] truncate">
                  <FolderKanban size={9} />
                  {activeWorkspace.name}
                </span>
              )}
            </div>
            <p className="text-[11px] text-slate-500 font-medium">
              {historyLoading
                ? 'Loading…'
                : `${historyRuns.length} past run${historyRuns.length !== 1 ? 's' : ''} in this project`}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* Refresh button */}
          <span
            role="button"
            tabIndex={0}
            onClick={(e) => { e.stopPropagation(); fetchHistory(20, true) }}
            onKeyDown={(e) => e.key === 'Enter' && fetchHistory(20, true)}
            className="w-6 h-6 flex items-center justify-center rounded-full
                       bg-slate-50 border border-slate-200 text-slate-400
                       hover:text-brand-500 hover:border-brand-200 transition-colors"
            title="Refresh history"
          >
            <RefreshCw size={11} className={historyLoading ? 'animate-spin' : ''} />
          </span>
          <div className="w-6 h-6 flex items-center justify-center rounded-full
                          bg-slate-50 border border-slate-200 text-slate-400">
            {collapsed ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
          </div>
        </div>
      </button>

      {/* Run list */}
      {!collapsed && (
        <div className="border-t border-slate-100 px-3 py-3 space-y-1 max-h-72 overflow-y-auto
                        animate-fade-in">
          {historyLoading && historyRuns.length === 0 ? (
            <div className="flex items-center justify-center py-8 gap-2 text-slate-400 text-[12px]">
              <Loader2 size={14} className="animate-spin" />
              Loading runs…
            </div>
          ) : historyRuns.length === 0 ? (
            <div className="text-center py-8 text-[12px] text-slate-400 font-medium">
              No runs in this project yet. Submit a query to get started.
            </div>
          ) : (
            historyRuns.map((run) => (
              <RunRow
                key={run.run_id || run.id}
                run={run}
                onLoad={handleLoad}
                isLoading={loadingId === (run.run_id || run.id)}
              />
            ))
          )}
        </div>
      )}
    </div>
  )
}
