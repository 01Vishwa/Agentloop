import React, { useCallback, useState } from 'react'
import { X, FileText, AlertCircle, Loader2, Trash2, ChevronDown, ChevronUp } from 'lucide-react'
import { DropZone } from './DropZone'
import { SchemaPreviewPanel } from './SchemaPreviewPanel'
import { useWorkspaceStore } from '../stores/workspaceStore'

// File type → badge colour mapping
const FILE_TYPE_COLORS = {
  csv:     'bg-emerald-100 text-emerald-700 border-emerald-200',
  xlsx:    'bg-blue-100   text-blue-700   border-blue-200',
  xls:     'bg-blue-100   text-blue-700   border-blue-200',
  parquet: 'bg-violet-100 text-violet-700 border-violet-200',
  json:    'bg-amber-100  text-amber-700  border-amber-200',
  md:      'bg-slate-100  text-slate-700  border-slate-200',
  pdf:     'bg-red-100    text-red-700    border-red-200',
}

function getFileExt(filename) {
  return filename?.split('.').pop()?.toLowerCase() ?? ''
}

function formatRows(n) {
  if (!n || n === 0) return null
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k rows` : `${n} rows`
}

/**
 * FileUploadPanel — multi-file upload panel with per-file chip management.
 *
 * Supports workspace-scoped batch uploads via POST /workspaces/{id}/upload.
 * After each upload the returned MultiFileContext is stored in workspaceStore
 * so SchemaPreviewPanel can render detected join candidates.
 *
 * Falls back to the legacy POST /upload endpoint when no activeWorkspace is set.
 *
 * @param {Object}   props
 * @param {Function} props.onUploaded   - Called with array of accepted filenames after success.
 * @param {Function} [props.onRejected] - Called with array of rejected file objects.
 * @param {string}   [props.sessionId]  - Optional session identifier for the in-memory cache.
 * @param {string}   [props.apiBase]    - Base URL for backend API calls (default: '/api').
 */
export function FileUploadPanel({
  onUploaded,
  onRejected,
  sessionId,
  apiBase = '/api',
}) {
  const { activeWorkspace, setMultiFileContext, resetFileContext, multiFileContext } =
    useWorkspaceStore()

  // Local state for file chips (separate from the store — these are ephemeral UI handles)
  const [uploadedFiles, setUploadedFiles] = useState([])
  const [uploading, setUploading]         = useState(false)
  const [uploadErrors, setUploadErrors]   = useState([])
  const [showSchema, setShowSchema]       = useState(true)

  // ── Upload handler ─────────────────────────────────────────────────────────

  const handleFiles = useCallback(async (rawFiles) => {
    if (!rawFiles?.length) return

    setUploading(true)
    setUploadErrors([])

    const formData = new FormData()
    for (const f of rawFiles) formData.append('files', f)
    if (sessionId) formData.append('session_id', sessionId)

    try {
      let url
      let response
      let result

      if (activeWorkspace?.id) {
        // Workspace-aware upload (Phase 9)
        url = `${apiBase}/workspaces/${activeWorkspace.id}/upload`
        if (sessionId) url += `?session_id=${encodeURIComponent(sessionId)}`
        response = await fetch(url, { method: 'POST', body: formData })
        result   = await response.json()

        if (!response.ok) {
          throw new Error(result?.detail ?? `Upload failed (${response.status})`)
        }

        // Update store with returned MultiFileContext
        if (result.multi_file_context) {
          setMultiFileContext(result.multi_file_context)
        }

        const accepted = result.accepted_files ?? []
        const rejected = result.rejected_files ?? []

        if (rejected.length && onRejected) {
          onRejected(rejected.map((r) => ({ name: r.filename, reason: r.reason })))
        }

        if (accepted.length) {
          const newChips = accepted.map((a) => ({
            filename: a.filename,
            ext:      getFileExt(a.filename),
            // row count is embedded in the multi_file_context
            rowCount: result.multi_file_context?.files?.find(
              (f) => f.file_name === a.filename
            )?.row_count ?? null,
          }))
          setUploadedFiles((prev) => [...prev, ...newChips])
          onUploaded?.(accepted.map((a) => a.filename))
        }

        if (rejected.length) {
          setUploadErrors(rejected.map((r) => `${r.filename}: ${r.reason}`))
        }
      } else {
        // Legacy in-memory upload (backward compat)
        url      = `${apiBase}/upload`
        response = await fetch(url, { method: 'POST', body: formData })
        result   = await response.json()

        if (!response.ok) {
          throw new Error(result?.detail ?? `Upload failed (${response.status})`)
        }

        const accepted = result.accepted_files ?? []
        const rejected = result.rejected_files ?? []

        if (rejected.length && onRejected) {
          onRejected(rejected.map((r) => ({ name: r.filename, reason: r.reason })))
        }

        if (accepted.length) {
          const newChips = accepted.map((a) => ({
            filename: a.filename,
            ext:      getFileExt(a.filename),
            rowCount: null,
          }))
          setUploadedFiles((prev) => [...prev, ...newChips])
          onUploaded?.(accepted.map((a) => a.filename))
        }

        if (rejected.length) {
          setUploadErrors(rejected.map((r) => `${r.filename}: ${r.reason}`))
        }
      }
    } catch (err) {
      setUploadErrors([err.message ?? 'Upload failed.'])
    } finally {
      setUploading(false)
    }
  }, [activeWorkspace, sessionId, apiBase, onUploaded, onRejected, setMultiFileContext])

  // ── Remove a single file ───────────────────────────────────────────────────

  const handleRemoveFile = useCallback(async (chip) => {
    if (!activeWorkspace?.id) {
      // Legacy: just remove the chip (no DB row to delete)
      setUploadedFiles((prev) => prev.filter((f) => f.filename !== chip.filename))
      return
    }

    // Find the workspace_files id from multiFileContext
    const fileEntry = multiFileContext?.files?.find((f) => f.file_name === chip.filename)
    if (!fileEntry?.id) {
      // Fallback: chip-only removal
      setUploadedFiles((prev) => prev.filter((f) => f.filename !== chip.filename))
      return
    }

    try {
      const url = `${apiBase}/workspaces/${activeWorkspace.id}/files/${fileEntry.id}`
      const response = await fetch(url, { method: 'DELETE' })
      const result   = await response.json()

      if (!response.ok) throw new Error(result?.detail ?? 'Delete failed.')

      setUploadedFiles((prev) => prev.filter((f) => f.filename !== chip.filename))
      if (result.multi_file_context) {
        setMultiFileContext(result.multi_file_context)
      }
    } catch (err) {
      setUploadErrors([err.message ?? 'Delete failed.'])
    }
  }, [activeWorkspace, apiBase, multiFileContext, setMultiFileContext])

  // ── Clear all files ────────────────────────────────────────────────────────

  const handleClearAll = useCallback(() => {
    setUploadedFiles([])
    resetFileContext()
    setUploadErrors([])
  }, [resetFileContext])

  // ── Render ─────────────────────────────────────────────────────────────────

  const hasFiles        = uploadedFiles.length > 0
  const hasMultiContext = multiFileContext?.files?.length >= 2
  const hasCandidates   = multiFileContext?.join_candidates?.length > 0

  return (
    <div className="space-y-4">
      {/* Drop zone */}
      <DropZone onFiles={handleFiles} onRejected={onRejected} />

      {/* Upload spinner */}
      {uploading && (
        <div className="flex items-center gap-2 text-sm text-brand-600 animate-pulse">
          <Loader2 size={16} className="animate-spin" />
          <span>Uploading files…</span>
        </div>
      )}

      {/* Error banner */}
      {uploadErrors.length > 0 && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 space-y-1">
          {uploadErrors.map((err, i) => (
            <div key={i} className="flex items-start gap-2 text-sm text-red-700">
              <AlertCircle size={14} className="mt-0.5 shrink-0" />
              <span>{err}</span>
            </div>
          ))}
        </div>
      )}

      {/* File chip list */}
      {hasFiles && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
              {uploadedFiles.length} file{uploadedFiles.length > 1 ? 's' : ''} uploaded
            </span>
            {uploadedFiles.length > 1 && (
              <button
                id="file-upload-clear-all"
                onClick={handleClearAll}
                className="flex items-center gap-1 text-xs text-slate-400 hover:text-red-500 transition-colors"
              >
                <Trash2 size={12} />
                Clear all
              </button>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            {uploadedFiles.map((chip) => {
              const typeColor = FILE_TYPE_COLORS[chip.ext] ?? 'bg-slate-100 text-slate-600 border-slate-200'
              const rows      = formatRows(chip.rowCount)

              return (
                <div
                  key={chip.filename}
                  className="flex items-center justify-between gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 shadow-sm"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <FileText size={14} className="shrink-0 text-slate-400" />
                    <span className="text-sm font-medium text-slate-700 truncate">
                      {chip.filename}
                    </span>
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    {rows && (
                      <span className="text-[10px] font-semibold text-slate-500 bg-slate-100 rounded-full px-2 py-0.5">
                        {rows}
                      </span>
                    )}
                    <span className={`text-[10px] font-bold border rounded-full px-2 py-0.5 uppercase ${typeColor}`}>
                      {chip.ext}
                    </span>
                    <button
                      id={`remove-file-${chip.filename.replace(/\W/g, '_')}`}
                      onClick={() => handleRemoveFile(chip)}
                      className="text-slate-300 hover:text-red-400 transition-colors"
                      aria-label={`Remove ${chip.filename}`}
                    >
                      <X size={14} />
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* Schema preview (multi-file only) */}
      {hasMultiContext && (
        <div>
          <button
            id="toggle-schema-preview"
            onClick={() => setShowSchema((s) => !s)}
            className="flex items-center gap-1.5 text-xs font-semibold text-brand-600 hover:text-brand-700 transition-colors mb-2"
          >
            {showSchema ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            {hasCandidates
              ? `${multiFileContext.join_candidates.length} join key${multiFileContext.join_candidates.length > 1 ? 's' : ''} detected`
              : 'Schema preview'}
          </button>
          {showSchema && <SchemaPreviewPanel context={multiFileContext} />}
        </div>
      )}
    </div>
  )
}
