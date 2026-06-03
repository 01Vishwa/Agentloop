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
 * Maps a pandas dtype string to a user-friendly display label.
 * e.g. "int64" → "INTEGER", "float64" → "FLOAT", "object" → "STRING"
 *
 * @param {string} dtype - Raw pandas dtype string
 * @returns {string} User-friendly type label
 */
const mapPandasDtype = (dtype) => {
  if (!dtype || typeof dtype !== 'string') return 'STRING'
  const d = dtype.toLowerCase()
  if (d.startsWith('int') || d === 'int64' || d === 'int32' || d === 'int16' || d === 'int8') return 'INTEGER'
  if (d.startsWith('uint')) return 'INTEGER'
  if (d.startsWith('float') || d === 'float64' || d === 'float32' || d === 'float16') return 'FLOAT'
  if (d === 'bool' || d === 'boolean' || d.startsWith('bool')) return 'BOOLEAN'
  if (d.startsWith('datetime') || d.includes('datetime')) return 'DATETIME'
  if (d.startsWith('timedelta')) return 'TIMEDELTA'
  if (d === 'category') return 'CATEGORY'
  if (d === 'string' || d === 'object') return 'STRING'
  return dtype.toUpperCase()
}

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
 * Extracts columns from a file object or URL.
 * Uses xlsx to parse both CSV and Excel files.
 */
const extractColumns = async (fileObj, filename, url = null) => {
  const ext = filename.split('.').pop()?.toLowerCase()
  
  if (['csv', 'xlsx', 'xls'].includes(ext)) {
    try {
      let dataBuffer;
      if (fileObj) {
        dataBuffer = await fileObj.arrayBuffer();
      } else if (url) {
        const res = await fetch(url);
        if (!res.ok) throw new Error('Failed to fetch file');
        dataBuffer = await res.arrayBuffer();
      } else {
        return null;
      }

      const XLSX = await import('xlsx');
      const workbook = XLSX.read(dataBuffer, { type: 'array' });
      const firstSheetName = workbook.SheetNames[0];
      const worksheet = workbook.Sheets[firstSheetName];
      const json = XLSX.utils.sheet_to_json(worksheet, { header: 1 });

      if (json.length > 0) {
        const headers = json[0];
        const dataRow = json.length > 1 ? json[1] : null;

        return headers.map((h, index) => {
          let type = 'string';
          if (dataRow && dataRow[index] !== undefined && dataRow[index] !== null) {
            const val = dataRow[index];
            if (typeof val === 'number') {
              type = Number.isInteger(val) ? 'integer' : 'float';
            } else if (typeof val === 'boolean') {
              type = 'boolean';
            } else if (val instanceof Date) {
              type = 'datetime';
            }
          }
          return { name: String(h || `Column ${index + 1}`), type };
        });
      }
    } catch (err) {
      console.error('Error extracting columns:', err);
      return null;
    }
  }
  return null;
}

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
  const [filesLoading, setFilesLoading] = useState(false)

  // Auth token for API calls — reads live from AuthContext
  const { getAccessToken } = useAuth()
  const { activeWorkspace, setMultiFileContext } = useWorkspaceStore()

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
    setFilesLoading(true)
    try {
      // Use workspace-scoped endpoint which reads from workspace_files table.
      // This is where workspace uploads (POST /workspaces/{id}/upload) save data.
      // The old /api/files endpoint reads from uploaded_files table which is
      // the wrong source for workspace-scoped files.
      const res = await fetch(`/api/workspaces/${activeWorkspace.id}/files`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (res.ok) {
        const data = await res.json()
        const formattedFiles = await Promise.all(data.map(async (r, i) => {
          const columns = r.schema_json?.columns
            ? r.schema_json.columns.map(c => {
                // c can be a plain string (column name) or an object with .name/.dtype
                const colName = typeof c === 'string' ? c : (c.name || c)
                // Dtypes live in schema_json.dtypes as a separate dict: {"col": "int64", ...}
                const rawDtype = (typeof c === 'object' && (c.dtype || c.type))
                  || (r.schema_json.dtypes && r.schema_json.dtypes[colName])
                  || 'string'
                return { name: colName, type: mapPandasDtype(rawDtype) }
              })
            : await extractColumns(null, r.filename, r.file_url || null)
          return {
            id: 10000 + i,
            dbId: r.id,
            name: r.filename,
            size: r.file_size,
            progress: 100,
            _raw: null,
            url: r.file_url || null,
            metadata: columns ? { columns } : null
          }
        }))
        setFiles(formattedFiles)
      }
    } catch (err) {
      console.error(err)
    } finally {
      setFilesLoading(false)
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
          // Store multiFileContext from workspace upload response for join inference
          if (uploadResult.multi_file_context) {
            setMultiFileContext(uploadResult.multi_file_context)
          }
        } catch (err) {
          toast(`Processing error: ${err.message}`, 'error')
        }
      }
    },
    [applyProgress, getAccessToken, sessionId, activeWorkspace?.id, setMultiFileContext]
  )

  const handleAddFiles = useCallback(
    async (newFiles) => {
      const entries = await Promise.all(newFiles.map(async (f) => {
        const columns = await extractColumns(f, f.name)
        return {
          id: ++fileIdCounter,
          name: f.name,
          size: f.size,
          progress: 0,
          _raw: f,
          metadata: columns ? { columns } : null
        }
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

  const handleCancelDuplicates = useCallback(() => {
    setPendingDuplicates([])
  }, [])

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
    filesLoading,
    pendingDuplicates,
    sessionId,
    handleAddFiles,
    handleConfirmDuplicates,
    handleCancelDuplicates,
    handleRemoveFile,
    handleClearAll,
  }
}
