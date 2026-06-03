/**
 * workspaceStore.js — Zustand store for active workspace state.
 *
 * Manages:
 *  - The list of user workspaces fetched from Supabase.
 *  - The currently active workspace (persisted to sessionStorage).
 *  - CRUD actions: fetch, create, select.
 *  - Multi-file context: the MultiFileContext returned by the backend
 *    after a workspace upload, including FileSchema objects and inferred
 *    join candidates.
 *  - Join overrides: manually specified join key pairs that override the
 *    inferred candidates in SchemaPreviewPanel.
 *
 * The store is auth-aware: fetchWorkspaces() is a no-op when no
 * access token is provided, and createWorkspace() guards the same way.
 *
 * All Supabase interaction goes through the REST client injected via
 * the supabase singleton — no hard-coded URLs.
 */

import { create } from 'zustand'
import { supabase } from '../lib/supabaseClient'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const SESSION_KEY = 'agentloop_active_workspace'

function loadPersistedWorkspaceId() {
  try {
    return sessionStorage.getItem(SESSION_KEY) || null
  } catch {
    return null
  }
}

function persistWorkspaceId(id) {
  try {
    if (id) sessionStorage.setItem(SESSION_KEY, id)
    else sessionStorage.removeItem(SESSION_KEY)
  } catch {
    // sessionStorage not available (private browsing, etc.)
  }
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

/**
 * @typedef {import('../types/index').Workspace} Workspace
 *
 * @typedef {Object} JoinCandidate
 * @property {string} left_var
 * @property {string} right_var
 * @property {string} left_col
 * @property {string} right_col
 * @property {number} confidence
 * @property {string} reason
 *
 * @typedef {Object} ColumnMeta
 * @property {string} name
 * @property {string} dtype
 * @property {string[]} sample_values
 *
 * @typedef {Object} FileSchema
 * @property {string} var_name
 * @property {string} file_name
 * @property {string} file_path
 * @property {string} file_type
 * @property {number} row_count
 * @property {ColumnMeta[]} columns
 *
 * @typedef {Object} MultiFileContext
 * @property {FileSchema[]} files
 * @property {JoinCandidate[]} join_candidates
 */

export const useWorkspaceStore = create((set) => ({
  /** @type {Workspace[]} */
  workspaces: [],

  /** @type {Workspace|null} */
  activeWorkspace: null,

  loading: false,

  /** @type {string|null} */
  error: null,

  // ── Multi-file state ───────────────────────────────────────────────────────

  /**
   * The MultiFileContext returned by the backend after a workspace upload.
   * Contains per-file schemas and inferred join candidates.
   *
   * @type {MultiFileContext|null}
   */
  multiFileContext: null,

  /**
   * User-specified join key overrides. Each entry replaces (not merges with)
   * the inferred candidate at the same position when sent to the agent.
   *
   * @type {JoinCandidate[]}
   */
  joinOverrides: [],

  // ── Actions ──────────────────────────────────────────────────────────────

  /**
   * Fetches workspaces owned by the authenticated user.
   * Restores the previously active workspace from sessionStorage if valid.
   *
   * @param {string} userId - The authenticated user's UUID.
   */
  fetchWorkspaces: async (userId) => {
    if (!userId) return
    set({ loading: true, error: null })

    const { data, error } = await supabase
      .from('workspaces')
      .select('*')
      .eq('user_id', userId)
      .order('created_at', { ascending: true })

    if (error) {
      set({ loading: false, error: error.message })
      return
    }

    const workspaces = data ?? []
    const persistedId = loadPersistedWorkspaceId()
    const active =
      workspaces.find((w) => w.id === persistedId) ?? workspaces[0] ?? null

    set({ workspaces, activeWorkspace: active, loading: false })
  },

  /**
   * Creates a new workspace for the authenticated user, then adds it to
   * the store and activates it.
   *
   * @param {string} userId - The authenticated user's UUID.
   * @param {string} name   - Workspace display name.
   * @returns {Promise<Workspace|null>}
   */
  createWorkspace: async (userId, name) => {
    if (!userId || !name.trim()) return null
    set({ loading: true, error: null })

    const { data, error } = await supabase
      .from('workspaces')
      .insert({ user_id: userId, name: name.trim() })
      .select()
      .single()

    if (error) {
      set({ loading: false, error: error.message })
      return null
    }

    set((state) => ({
      workspaces: [...state.workspaces, data],
      activeWorkspace: data,
      loading: false,
    }))
    persistWorkspaceId(data.id)
    return data
  },

  /**
   * Sets the active workspace and persists the choice to sessionStorage.
   * Clears the multi-file context because the new workspace has its own files.
   *
   * @param {Workspace} workspace
   */
  setActiveWorkspace: (workspace) => {
    persistWorkspaceId(workspace?.id ?? null)
    set({ activeWorkspace: workspace, multiFileContext: null, joinOverrides: [] })
  },

  /**
   * Stores the MultiFileContext returned after a workspace file upload.
   * Called by FileUploadPanel after each successful upload response.
   *
   * @param {MultiFileContext|null} ctx
   */
  setMultiFileContext: (ctx) => {
    set({ multiFileContext: ctx })
  },

  /**
   * Appends a manual join key override.
   * The override must be a JoinCandidate-shaped object.
   * Duplicates (same left_var+left_col+right_var+right_col) are replaced.
   *
   * @param {JoinCandidate} candidate
   */
  addJoinOverride: (candidate) => {
    set((state) => {
      const existing = state.joinOverrides.filter(
        (o) =>
          !(
            o.left_var === candidate.left_var &&
            o.left_col === candidate.left_col &&
            o.right_var === candidate.right_var &&
            o.right_col === candidate.right_col
          )
      )
      return { joinOverrides: [...existing, { ...candidate, confidence: 1.0, reason: 'manual override' }] }
    })
  },

  /**
   * Removes the join override at the given index.
   *
   * @param {number} index
   */
  removeJoinOverride: (index) => {
    set((state) => ({
      joinOverrides: state.joinOverrides.filter((_, i) => i !== index),
    }))
  },

  /**
   * Clears the multi-file context and all overrides.
   * Called when the user removes all files from the workspace.
   */
  resetFileContext: () => {
    set({ multiFileContext: null, joinOverrides: [] })
  },

  /**
   * Resets the store (call on sign-out).
   */
  reset: () => {
    persistWorkspaceId(null)
    set({
      workspaces: [],
      activeWorkspace: null,
      loading: false,
      error: null,
      multiFileContext: null,
      joinOverrides: [],
    })
  },
}))
