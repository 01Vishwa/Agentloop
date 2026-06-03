import React, { useState, useCallback } from 'react'
import {
  GitMerge, AlertCircle, CheckCircle2, HelpCircle,
  ChevronDown, ChevronUp, Plus, X,
} from 'lucide-react'
import { useWorkspaceStore } from '../stores/workspaceStore'

// ---------------------------------------------------------------------------
// Confidence dot
// ---------------------------------------------------------------------------

function ConfidenceDot({ score }) {
  const pct = Math.round(score * 100)
  let color = 'bg-slate-300'
  if (score >= 0.80) color = 'bg-emerald-400'
  else if (score >= 0.50) color = 'bg-amber-400'

  return (
    <span className="flex items-center gap-1.5 shrink-0">
      <span className={`w-2 h-2 rounded-full ${color} inline-block`} />
      <span className="text-[10px] font-semibold text-slate-500">{pct}%</span>
    </span>
  )
}

// ---------------------------------------------------------------------------
// File card
// ---------------------------------------------------------------------------

const DTYPE_COLORS = {
  int:    'bg-blue-50   text-blue-700',
  float:  'bg-indigo-50 text-indigo-700',
  object: 'bg-slate-50  text-slate-600',
  bool:   'bg-amber-50  text-amber-700',
}

function dtypeColor(dtype) {
  const lower = dtype.toLowerCase()
  if (lower.includes('int'))   return DTYPE_COLORS.int
  if (lower.includes('float')) return DTYPE_COLORS.float
  if (lower.includes('bool'))  return DTYPE_COLORS.bool
  return DTYPE_COLORS.object
}

function FileCard({ file }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 bg-slate-50 border-b border-slate-100">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs font-bold text-brand-600 bg-brand-50 border border-brand-200 rounded-full px-2 py-0.5">
            {file.var_name}
          </span>
          <span className="text-sm font-semibold text-slate-700 truncate max-w-[160px]">
            {file.file_name}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {file.row_count > 0 && (
            <span className="text-[10px] text-slate-500 bg-slate-100 rounded-full px-2 py-0.5 font-semibold">
              {file.row_count >= 1000
                ? `${(file.row_count / 1000).toFixed(1)}k rows`
                : `${file.row_count} rows`}
            </span>
          )}
          <span className="text-[10px] font-bold uppercase text-slate-500 bg-slate-200 rounded-full px-2 py-0.5">
            {file.file_type}
          </span>
          <button
            onClick={() => setExpanded((e) => !e)}
            className="text-slate-400 hover:text-slate-600 transition-colors"
            id={`file-card-toggle-${file.var_name}`}
            aria-label={expanded ? 'Collapse columns' : 'Expand columns'}
          >
            {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </button>
        </div>
      </div>

      {/* Column list */}
      {expanded && file.columns?.length > 0 && (
        <div className="p-3 max-h-48 overflow-y-auto space-y-1">
          {file.columns.map((col) => (
            <div
              key={col.name}
              className="flex items-start gap-2 text-xs text-slate-600"
            >
              <span className={`font-mono text-[10px] rounded px-1.5 py-0.5 font-semibold ${dtypeColor(col.dtype)} shrink-0`}>
                {col.dtype}
              </span>
              <span className="font-medium text-slate-700 flex-1">{col.name}</span>
              {col.sample_values?.length > 0 && (
                <span className="text-slate-400 truncate max-w-[80px]">
                  {col.sample_values.slice(0, 2).join(', ')}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Join override editor
// ---------------------------------------------------------------------------

function JoinOverrideEditor({ files }) {
  const { joinOverrides, addJoinOverride, removeJoinOverride } = useWorkspaceStore()
  const [leftVar, setLeftVar]   = useState(files[0]?.var_name ?? '')
  const [leftCol, setLeftCol]   = useState('')
  const [rightVar, setRightVar] = useState(files[1]?.var_name ?? '')
  const [rightCol, setRightCol] = useState('')

  const leftFile  = files.find((f) => f.var_name === leftVar)
  const rightFile = files.find((f) => f.var_name === rightVar)

  const handleAdd = useCallback(() => {
    if (!leftVar || !leftCol || !rightVar || !rightCol) return
    if (leftVar === rightVar) return
    addJoinOverride({ left_var: leftVar, left_col: leftCol, right_var: rightVar, right_col: rightCol })
    setLeftCol('')
    setRightCol('')
  }, [leftVar, leftCol, rightVar, rightCol, addJoinOverride])

  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50/50 p-4 space-y-3">
      <p className="text-xs font-semibold text-slate-600 uppercase tracking-wide">
        Override join keys
      </p>

      {/* Existing overrides */}
      {joinOverrides.length > 0 && (
        <div className="space-y-1.5">
          {joinOverrides.map((o, i) => (
            <div
              key={i}
              className="flex items-center justify-between gap-2 bg-white border border-slate-200 rounded-lg px-3 py-1.5 text-xs"
            >
              <span className="font-mono text-slate-700">
                {o.left_var}[<span className="text-brand-600">'{o.left_col}'</span>]
                {' '}&harr;{' '}
                {o.right_var}[<span className="text-brand-600">'{o.right_col}'</span>]
              </span>
              <button
                onClick={() => removeJoinOverride(i)}
                id={`remove-override-${i}`}
                className="text-slate-300 hover:text-red-400 transition-colors"
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Add override form */}
      <div className="grid grid-cols-[1fr_auto_1fr_auto] gap-2 items-end">
        <div className="space-y-1">
          <label className="text-[10px] font-semibold text-slate-500">Left DataFrame</label>
          <select
            id="join-override-left-var"
            value={leftVar}
            onChange={(e) => { setLeftVar(e.target.value); setLeftCol('') }}
            className="w-full text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-brand-300"
          >
            {files.map((f) => (
              <option key={f.var_name} value={f.var_name}>{f.var_name} ({f.file_name})</option>
            ))}
          </select>
          <select
            id="join-override-left-col"
            value={leftCol}
            onChange={(e) => setLeftCol(e.target.value)}
            className="w-full text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-brand-300"
          >
            <option value="">— column —</option>
            {leftFile?.columns?.map((c) => (
              <option key={c.name} value={c.name}>{c.name}</option>
            ))}
          </select>
        </div>

        <span className="text-slate-400 text-sm pb-3">&harr;</span>

        <div className="space-y-1">
          <label className="text-[10px] font-semibold text-slate-500">Right DataFrame</label>
          <select
            id="join-override-right-var"
            value={rightVar}
            onChange={(e) => { setRightVar(e.target.value); setRightCol('') }}
            className="w-full text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-brand-300"
          >
            {files.map((f) => (
              <option key={f.var_name} value={f.var_name}>{f.var_name} ({f.file_name})</option>
            ))}
          </select>
          <select
            id="join-override-right-col"
            value={rightCol}
            onChange={(e) => setRightCol(e.target.value)}
            className="w-full text-xs border border-slate-200 rounded-lg px-2 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-brand-300"
          >
            <option value="">— column —</option>
            {rightFile?.columns?.map((c) => (
              <option key={c.name} value={c.name}>{c.name}</option>
            ))}
          </select>
        </div>

        <button
          id="add-join-override"
          onClick={handleAdd}
          disabled={!leftCol || !rightCol || leftVar === rightVar}
          className="flex items-center gap-1 text-xs font-semibold text-white bg-brand-500 hover:bg-brand-600 disabled:opacity-40 disabled:cursor-not-allowed rounded-lg px-3 py-1.5 transition-colors mb-0.5"
        >
          <Plus size={12} />
          Add
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

/**
 * SchemaPreviewPanel — displays the multi-file context returned by the backend.
 *
 * Shows:
 *  - Horizontal scrollable row of file cards (variable name, filename, row count, columns).
 *  - "Detected join keys" section with coloured confidence indicators.
 *  - Expandable "Override join keys" section for manual control.
 *
 * @param {Object}           props
 * @param {import('../stores/workspaceStore').MultiFileContext} props.context
 */
export function SchemaPreviewPanel({ context }) {
  const [showOverrideEditor, setShowOverrideEditor] = useState(false)

  if (!context?.files?.length) return null

  const { files, join_candidates: candidates = [] } = context

  return (
    <div className="rounded-2xl border border-slate-200 bg-white shadow-sm overflow-hidden">
      {/* File cards */}
      <div className="p-4 border-b border-slate-100">
        <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-3">
          Uploaded files
        </p>
        <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.min(files.length, 2)}, 1fr)` }}>
          {files.map((file) => (
            <FileCard key={file.var_name} file={file} />
          ))}
        </div>
      </div>

      {/* Join candidates */}
      <div className="p-4 border-b border-slate-100">
        <div className="flex items-center gap-2 mb-3">
          <GitMerge size={14} className="text-brand-500" />
          <p className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
            Detected join keys
          </p>
        </div>

        {candidates.length === 0 ? (
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <HelpCircle size={13} />
            <span>No join keys detected automatically. Use the override editor below.</span>
          </div>
        ) : (
          <div className="space-y-2">
            {candidates.map((c, i) => (
              <div
                key={i}
                className="flex items-center justify-between gap-3 rounded-lg border border-slate-100 bg-slate-50 px-3 py-2"
              >
                <div className="flex items-center gap-2 min-w-0 flex-1">
                  {c.confidence >= 0.80 ? (
                    <CheckCircle2 size={13} className="text-emerald-500 shrink-0" />
                  ) : c.confidence >= 0.50 ? (
                    <AlertCircle size={13} className="text-amber-500 shrink-0" />
                  ) : (
                    <HelpCircle size={13} className="text-slate-400 shrink-0" />
                  )}
                  <span className="font-mono text-xs text-slate-700 truncate">
                    <span className="text-brand-600 font-semibold">{c.left_var}</span>
                    [&apos;{c.left_col}&apos;]
                    {' '}&harr;{' '}
                    <span className="text-brand-600 font-semibold">{c.right_var}</span>
                    [&apos;{c.right_col}&apos;]
                  </span>
                </div>
                <div className="shrink-0 flex items-center gap-2">
                  <ConfidenceDot score={c.confidence} />
                  <span className="text-[10px] text-slate-400 italic hidden sm:block truncate max-w-[120px]">
                    {c.reason}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Override editor toggle */}
      <div className="p-4">
        <button
          id="toggle-join-override-editor"
          onClick={() => setShowOverrideEditor((s) => !s)}
          className="flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-slate-700 transition-colors"
        >
          {showOverrideEditor ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          {showOverrideEditor ? 'Hide' : 'Override join keys…'}
        </button>

        {showOverrideEditor && (
          <div className="mt-3">
            <JoinOverrideEditor files={files} />
          </div>
        )}
      </div>
    </div>
  )
}
