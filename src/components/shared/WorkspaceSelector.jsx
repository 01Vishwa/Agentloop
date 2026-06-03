/**
 * WorkspaceSelector.jsx — Workspace switcher dropdown.
 *
 * Renders above the FileUploadPanel. Shows the user's workspaces, allows
 * switching between them, and provides an inline "New workspace" creator.
 *
 * State lives in the Zustand workspaceStore; this component is purely
 * presentational + event-driven.
 */

import React, { useState, useRef, useEffect, useCallback } from 'react'
import { ChevronDown, Plus, Briefcase, Loader2, FolderKanban, Check } from 'lucide-react'
import { useWorkspaceStore } from '../../stores/workspaceStore'
import { useAuth } from '../../contexts/AuthContext'

// ---------------------------------------------------------------------------
// WorkspaceSelector
// ---------------------------------------------------------------------------

export function WorkspaceSelector() {
  const { user, isAuthenticated } = useAuth()
  const {
    workspaces,
    activeWorkspace,
    loading,
    fetchWorkspaces,
    createWorkspace,
    setActiveWorkspace,
  } = useWorkspaceStore()

  const [open, setOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [createLoading, setCreateLoading] = useState(false)

  const dropdownRef = useRef(null)
  const inputRef = useRef(null)

  // Fetch workspaces when user signs in
  useEffect(() => {
    if (isAuthenticated && user?.id) {
      fetchWorkspaces(user.id)
    }
  }, [isAuthenticated, user?.id, fetchWorkspaces])

  // Auto-focus the new workspace input
  useEffect(() => {
    if (creating) setTimeout(() => inputRef.current?.focus(), 80)
  }, [creating])

  // Close dropdown on outside click
  useEffect(() => {
    function handleClick(e) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setOpen(false)
        setCreating(false)
      }
    }
    if (open) document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [open])

  const handleSelect = useCallback((ws) => {
    setActiveWorkspace(ws)
    setOpen(false)
  }, [setActiveWorkspace])

  const handleCreate = async (e) => {
    e.preventDefault()
    if (!newName.trim() || !user?.id) return
    setCreateLoading(true)
    await createWorkspace(user.id, newName)
    setNewName('')
    setCreating(false)
    setCreateLoading(false)
    setOpen(false)
  }

  // Don't render for anonymous users
  if (!isAuthenticated) return null

  const displayName = activeWorkspace?.name ?? 'Select workspace'

  return (
    <div className="relative mb-3" ref={dropdownRef}>
      {/* Trigger button */}
      <button
        id="workspace-selector-btn"
        onClick={() => setOpen((v) => !v)}
        className={`w-full flex items-center justify-between gap-2 px-3 py-2.5
                    rounded-xl border transition-all duration-150 text-sm font-medium
                    ${open
                      ? 'border-brand-400 bg-brand-50 text-brand-700 shadow-sm shadow-brand-100'
                      : 'border-slate-200 bg-white text-slate-700 hover:border-brand-300 hover:bg-slate-50'
                    }`}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <div className="flex items-center gap-2 min-w-0">
          {loading ? (
            <Loader2 size={14} className="text-brand-500 animate-spin shrink-0" />
          ) : (
            <FolderKanban size={14} className="text-brand-500 shrink-0" />
          )}
          <span className="truncate">{displayName}</span>
        </div>
        <ChevronDown
          size={14}
          className={`text-slate-400 shrink-0 transition-transform duration-200 ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Dropdown panel */}
      {open && (
        <div
          className="absolute top-full left-0 right-0 mt-1.5 z-30
                     bg-white border border-slate-200 rounded-xl shadow-lg shadow-slate-900/10
                     overflow-hidden"
          role="listbox"
          aria-label="Workspaces"
        >
          {/* Workspace list */}
          <div className="max-h-44 overflow-y-auto py-1">
            {workspaces.length === 0 && !loading ? (
              <p className="text-xs text-slate-400 text-center py-4 px-3">
                No workspaces yet. Create one below.
              </p>
            ) : (
              workspaces.map((ws) => (
                <button
                  key={ws.id}
                  role="option"
                  aria-selected={ws.id === activeWorkspace?.id}
                  onClick={() => handleSelect(ws)}
                  className={`w-full flex items-center gap-2.5 px-3 py-2.5 text-sm transition-colors duration-100
                              ${ws.id === activeWorkspace?.id
                                ? 'bg-brand-50 text-brand-700 font-semibold'
                                : 'text-slate-700 hover:bg-slate-50'}`}
                >
                  <Briefcase size={13} className={ws.id === activeWorkspace?.id ? 'text-brand-500' : 'text-slate-400'} />
                  <span className="flex-1 truncate text-left">{ws.name}</span>
                  {ws.id === activeWorkspace?.id && (
                    <Check size={13} className="text-brand-500 shrink-0" />
                  )}
                </button>
              ))
            )}
          </div>

          {/* Divider */}
          <div className="border-t border-slate-100" />

          {/* Create workspace inline form */}
          {creating ? (
            <form onSubmit={handleCreate} className="flex items-center gap-2 px-3 py-2.5">
              <input
                ref={inputRef}
                id="new-workspace-input"
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Workspace name…"
                maxLength={60}
                className="flex-1 text-sm bg-slate-50 border border-slate-200 rounded-lg
                           px-2.5 py-1.5 focus:outline-none focus:border-brand-400 focus:ring-2
                           focus:ring-brand-400/20 text-slate-800 placeholder-slate-400"
                disabled={createLoading}
              />
              <button
                id="create-workspace-submit-btn"
                type="submit"
                disabled={!newName.trim() || createLoading}
                className="p-1.5 rounded-lg bg-brand-500 text-white hover:bg-brand-600
                           disabled:opacity-50 transition-colors duration-150"
              >
                {createLoading
                  ? <Loader2 size={13} className="animate-spin" />
                  : <Check size={13} />
                }
              </button>
            </form>
          ) : (
            <button
              id="new-workspace-btn"
              onClick={() => setCreating(true)}
              className="w-full flex items-center gap-2 px-3 py-2.5 text-xs font-semibold
                         text-brand-600 hover:bg-brand-50 transition-colors duration-100"
            >
              <Plus size={13} />
              New workspace
            </button>
          )}
        </div>
      )}
    </div>
  )
}
