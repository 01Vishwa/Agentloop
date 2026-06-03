import React, { useCallback } from 'react'
import { UploadCloud, Trash2, FolderOpen, FolderKanban, Loader2 } from 'lucide-react'
import { DropZone } from './DropZone'
import { FileList } from './FileList'
import { toast } from '../shared/Toast'
import { useWorkspaceStore } from '../../stores/workspaceStore'

export function FileUploadPanel({ files, filesLoading, onAddFiles, onRemoveFile, onClearAll }) {
  const handleRejected = useCallback((rejectedFiles) => {
    rejectedFiles.forEach((r) => toast(`"${r.name}" — ${r.reason}`, 'error'))
  }, [])

  const { activeWorkspace } = useWorkspaceStore()

  return (
    <div className="h-full flex flex-col gap-4 min-h-0">

      {/* Header */}
      <div>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-brand-50 border border-brand-100 flex items-center justify-center shadow-sm">
              <FolderOpen size={16} className="text-brand-600" />
            </div>
            <div>
              <h2 className="font-bold text-slate-800 text-base leading-tight">Document Ingestion</h2>
              {activeWorkspace?.name && (
                <div className="flex items-center gap-1 mt-0.5">
                  <FolderKanban size={10} className="text-brand-500 shrink-0" />
                  <span className="text-[10px] font-semibold text-brand-600 truncate max-w-[160px]">
                    {activeWorkspace.name}
                  </span>
                </div>
              )}
            </div>
          </div>
          {files.length > 0 && (
            <button
              id="clear-all-files-btn"
              onClick={onClearAll}
              className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-rose-600 font-medium
                         px-2.5 py-1.5 rounded-lg hover:bg-rose-50 border border-transparent hover:border-rose-100 transition-all duration-150"
            >
              <Trash2 size={12} />
              Clear all
            </button>
          )}
        </div>
        <p className="text-xs text-slate-500 mt-1 ml-10">
          Supported: CSV, TXT, XLSX, PDF, JSON, Markdown, Parquet
        </p>
      </div>

      {/* Drop Zone */}
      <DropZone onFiles={onAddFiles} onRejected={handleRejected} />

      {/* File List */}
      {files.length > 0 ? (
        <FileList files={files} onRemove={onRemoveFile} />
      ) : filesLoading ? (
        <div className="flex-1 flex flex-col items-center justify-center text-center py-8 opacity-60">
          <Loader2 size={28} className="text-brand-400 mb-2 animate-spin" />
          <p className="text-xs text-slate-400 font-medium">Loading files…</p>
        </div>
      ) : (
        <div className="flex-1 flex flex-col items-center justify-center text-center py-8 opacity-60">
          <UploadCloud size={32} className="text-slate-300 mb-2" />
          <p className="text-xs text-slate-400 font-medium">No files uploaded yet</p>
          {activeWorkspace?.name && (
            <p className="text-[10px] text-slate-300 font-medium mt-1">
              Files are scoped to "{activeWorkspace.name}"
            </p>
          )}
        </div>
      )}
    </div>
  )
}

