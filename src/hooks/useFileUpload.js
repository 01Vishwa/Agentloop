/**
 * useFileUpload.js — Custom hook for file upload orchestration.
 *
 * Encapsulates all file state, upload flow, progress tracking,
 * and duplicate handling — extracted from App.jsx.
 *
 * Auth integration: reads the current access token from AuthContext and
 * stamps it onto every upload / process / clear API call so that the
 * FastAPI auth middleware can verify the Supabase JWT.
 */

import { useState, useCallback, useRef, useEffect } from 'react'
import { useParams } from 'react-router-dom'
import { toast } from '../components/shared/Toast'
import { uploadFiles, processFiles, clearBackendCache } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { useWorkspaceStore } from '../stores/workspaceStore'

let fileIdCounter = 0

/**
 * Generates a stable session ID for this browser tab.
 * Uses crypto.randomUUID when available, falls back to a random hex string.
 *
 * @returns {string}
 */
const _generateSessionId = () =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2)

/**
 * Manages the full file lifecycle: adding, uploading, replacing duplicates, removing.
 *
 * Per-project isolation: when a projectId is present in the URL, the sessionId
 * is derived from it so that every project gets its own backend file-cache bucket.
 * If the user has not selected a project yet, a random UUID is used as a fallback.
 *
 * @returns {{ files, pendingDuplicates, sessionId, handleAddFiles, handleConfirmDuplicates, handleRemoveFile, handleClearAll }}
 */
export function useFileUpload() {
  const [files, setFiles] = useState([])
  const [pendingDuplicates, setPendingDuplicates] = useState([])

  // Auth token for API calls — reads live from AuthContext
  const { getAccessToken } = useAuth()
  const { activeWorkspace } = useWorkspaceStore()

  // Derive sessionId from the active projectId so that the backend file-cache
  // bucket is scoped per project. Falls back to a random UUID when no project
  // is selected (e.g. on the first render before routing completes).
  const { projectId } = useParams() ?? {}
  const fallbackRef = useRef(_generateSessionId())
  // Prefix with 'proj-' to make it clear this bucket belongs to a project.
  const sessionId = projectId ? `proj-${projectId}` : fallbackRef.current

  const applyProgress = useCallback((id, pct) => {
    setFiles((prev) =>
      prev.map((f) => (f.id === id ? { ...f, progress: pct < 0 ? 0 : pct } : f))
    )
  }, [])

  const fetchUploadedFiles = useCallback(async () => {
    if (!activeWorkspace?.id) return
    const token = getAccessToken()
    try {
      const res = await fetch(`/api/files?workspace_id=${activeWorkspace.id}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (res.ok) {
        const data = await res.json()
        setFiles(data.map((r, i) => ({
          id: 10000 + i,
          name: r.filename,
          size: r.file_size,
          progress: 100,
          _raw: null,
          url: r.file_url
        })))
      }
    } catch (err) {
      console.error(err)
    }
  }, [activeWorkspace?.id, getAccessToken])

  useEffect(() => {
    fetchUploadedFiles()
  }, [fetchUploadedFiles])

  /** Upload batch → /api/upload then trigger /api/process for accepted files. */
  const startUploads = useCallback(
    async (entriesToUpload) => {
      const token = getAccessToken()
      let uploadResult
      try {
        uploadResult = await uploadFiles(entriesToUpload, applyProgress, sessionId, token, activeWorkspace?.id)
      } catch (err) {
        toast(`Upload error: ${err.message}`, 'error')
        entriesToUpload.forEach((e) => applyProgress(e.id, 0))
        return
      }

      if (uploadResult.rejected_files?.length > 0) {
        uploadResult.rejected_files.forEach((r) =>
          toast(`Rejected "${r.filename}": ${r.reason}`, 'error')
        )
      }

      const acceptedNames = uploadResult.accepted_files.map((f) => f.filename)
      if (acceptedNames.length > 0) {
        try {
          await processFiles(acceptedNames, sessionId, token)
          toast(
            `${acceptedNames.length} file${acceptedNames.length > 1 ? 's' : ''} uploaded successfully`,
            'success'
          )
        } catch (err) {
          toast(`Processing error: ${err.message}`, 'error')
        }
      }
    },
    [applyProgress, getAccessToken, sessionId, activeWorkspace?.id]
  )

  const handleAddFiles = useCallback(
    async (newFiles) => {
      const entries = newFiles.map((f) => ({
        id: ++fileIdCounter,
        name: f.name,
        size: f.size,
        progress: 0,
        _raw: f,
      }))

      const existingNames = files.map((f) => f.name)
      const unique = entries.filter((e) => !existingNames.includes(e.name))
      const duplicates = entries.filter((e) => existingNames.includes(e.name))

      if (unique.length > 0) {
        toast(`${unique.length} file${unique.length > 1 ? 's' : ''} added`, 'success')
        setFiles((prev) => [...prev, ...unique])
        startUploads(unique)
      }

      if (duplicates.length > 0) {
        setPendingDuplicates(duplicates)
      } else if (unique.length === 0) {
        toast('Files already uploaded', 'info')
      }
    },
    [files, startUploads]
  )

  const handleConfirmDuplicates = useCallback(() => {
    if (pendingDuplicates.length > 0) {
      toast(
        `${pendingDuplicates.length} file${pendingDuplicates.length > 1 ? 's' : ''} replaced`,
        'success'
      )
      setFiles((prev) => {
        const dupNames = pendingDuplicates.map((d) => d.name)
        return [...prev.filter((f) => !dupNames.includes(f.name)), ...pendingDuplicates]
      })
      startUploads(pendingDuplicates)
      setPendingDuplicates([])
    }
  }, [pendingDuplicates, startUploads])

  const handleRemoveFile = useCallback(
    (id) => {
      const file = files.find((f) => f.id === id)
      if (file) {
        toast(`Removed "${file.name}"`, 'error')
        setFiles((prev) => prev.filter((f) => f.id !== id))
      }
    },
    [files]
  )

  const handleClearAll = useCallback(async () => {
    if (files.length > 0) {
      toast('All files cleared', 'info')
      setFiles([])
      const token = getAccessToken()
      await clearBackendCache(sessionId, token)
    }
  }, [files, sessionId, getAccessToken])

  return {
    files,
    pendingDuplicates,
    sessionId,
    handleAddFiles,
    handleConfirmDuplicates,
    handleRemoveFile,
    handleClearAll,
  }
}
