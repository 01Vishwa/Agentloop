/**
 * AuthContext.jsx — Global Supabase authentication state provider.
 *
 * Responsibilities:
 *  - Initialises and caches the Supabase session on mount.
 *  - Subscribes to onAuthStateChange so token refreshes, sign-outs and
 *    magic-link callbacks automatically update UI state.
 *  - Exposes signIn / signUp / signOut helpers and a getAccessToken()
 *    utility consumed by API interceptors.
 *
 * No credentials or URLs are hard-coded here — all config flows through
 * the singleton supabaseClient which reads from VITE_* env vars.
 */

/* eslint-disable react-refresh/only-export-components */

import React, {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  useMemo,
} from 'react'
import { supabase } from '../lib/supabaseClient'

// ---------------------------------------------------------------------------
// Context shape
// ---------------------------------------------------------------------------

/**
 * @typedef {Object} AuthContextValue
 * @property {import('@supabase/supabase-js').User|null}    user        - Authenticated user object or null.
 * @property {import('@supabase/supabase-js').Session|null} session     - Full session including tokens.
 * @property {boolean}                                      loading     - True during initial session hydration.
 * @property {string|null}                                  authError   - Last auth error message.
 * @property {Function}                                     signIn      - (email, password) → Promise
 * @property {Function}                                     signUp      - (email, password) → Promise
 * @property {Function}                                     signOut     - () → Promise
 * @property {Function}                                     getAccessToken - () → string|null (sync)
 * @property {boolean}                                      isAuthenticated - Convenience boolean.
 */

const AuthContext = createContext(/** @type {AuthContextValue} */ (null))

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

/**
 * AuthProvider — wraps the React tree and makes auth state available
 * everywhere via useAuth().
 *
 * @param {{ children: React.ReactNode }} props
 */
export function AuthProvider({ children }) {
  const [session, setSession] = useState(/** @type {import('@supabase/supabase-js').Session|null} */ (null))
  const [user, setUser] = useState(/** @type {import('@supabase/supabase-js').User|null} */ (null))
  const [loading, setLoading] = useState(true)
  const [authError, setAuthError] = useState(/** @type {string|null} */ (null))

  // ── Initialise: hydrate from persisted session ─────────────────────────
  useEffect(() => {
    let mounted = true

    supabase.auth.getSession().then(({ data: { session: s } }) => {
      if (!mounted) return
      setSession(s)
      setUser(s?.user ?? null)
      setLoading(false)
    })

    // Subscribe to auth state changes (token refresh, sign-out, magic link)
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      (_event, s) => {
        if (!mounted) return
        setSession(s)
        setUser(s?.user ?? null)
        setLoading(false)
        setAuthError(null)
      }
    )

    return () => {
      mounted = false
      subscription.unsubscribe()
    }
  }, [])

  // ── Sign-in with email + password ───────────────────────────────────────
  const signIn = useCallback(async (email, password) => {
    setAuthError(null)
    const { data, error } = await supabase.auth.signInWithPassword({ email, password })
    if (error) {
      setAuthError(error.message)
      throw error
    }
    return data
  }, [])

  // ── Sign-up with email + password ───────────────────────────────────────
  const signUp = useCallback(async (email, password) => {
    setAuthError(null)
    const { data, error } = await supabase.auth.signUp({ email, password })
    if (error) {
      setAuthError(error.message)
      throw error
    }
    return data
  }, [])

  // ── Sign-out ────────────────────────────────────────────────────────────
  const signOut = useCallback(async () => {
    setAuthError(null)
    const { error } = await supabase.auth.signOut()
    if (error) {
      setAuthError(error.message)
      throw error
    }
  }, [])

  /**
   * Returns the current JWT access token synchronously.
   * Used by API interceptors to stamp every protected request.
   *
   * @returns {string|null}
   */
  const getAccessToken = useCallback(() => {
    return session?.access_token ?? null
  }, [session])

  // Memoised value prevents unnecessary re-renders in consumers
  const value = useMemo(() => ({
    user,
    session,
    loading,
    authError,
    signIn,
    signUp,
    signOut,
    getAccessToken,
    isAuthenticated: !!user,
  }), [user, session, loading, authError, signIn, signUp, signOut, getAccessToken])

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  )
}

// ---------------------------------------------------------------------------
// Consumer hook
// ---------------------------------------------------------------------------

/**
 * useAuth — consume the auth context from any component.
 *
 * @returns {AuthContextValue}
 * @throws {Error} If called outside an <AuthProvider> tree.
 */
export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('[useAuth] Must be used inside an <AuthProvider>.')
  }
  return ctx
}
