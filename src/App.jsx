/**
 * App.jsx — Application root.
 *
 * Wraps the entire component tree with <AuthProvider> so that every
 * descendant can consume auth state via useAuth(). The provider must be
 * the outermost wrapper — placed here rather than in main.jsx so that
 * AuthModal can be rendered at the router level if needed.
 *
 * Per-project isolation fix:
 * - useFileUpload() and useAgentRun() are now instantiated inside
 *   <ProjectWorkspace>, which is keyed by projectId.
 * - React's key prop forces a full remount (and hook reset) whenever
 *   the user switches projects — giving each project its own:
 *     • sessionId (scoped file cache on the backend)
 *     • files list (no cross-project file bleed)
 *     • run history (already filtered by workspace_id on the API)
 *     • agent state (clean slate per project)
 */

import React from 'react'
import { Routes, Route, Navigate, useLocation, useParams } from 'react-router-dom'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import { LandingPage } from './pages/LandingPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { HomePage } from './pages/HomePage'
import { ToastContainer } from './components/shared/Toast'
import { EvalDashboard } from './pages/EvalDashboard'
import { useFileUpload } from './hooks/useFileUpload'
import { useAgentRun } from './hooks/useAgentRun'
import './index.css'

/**
 * Protected Route Wrapper
 */
function ProtectedRoute({ children }) {
  const { isAuthenticated, loading } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#f8fafc]">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-brand-500"></div>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to="/" state={{ from: location }} replace />
  }

  return children
}

/**
 * ProjectWorkspace — per-project isolated state container.
 *
 * Keyed by projectId so React remounts this component (and all its hooks)
 * whenever the user switches to a different project. This ensures:
 *  - A fresh sessionId per project (backend file cache scoped per project)
 *  - An empty file list per project
 *  - Project-scoped run history via workspace_id query param
 *  - Clean agent state per project
 */
function ProjectWorkspace() {
  const { projectId } = useParams()
  const fileState = useFileUpload()
  const agentState = useAgentRun(fileState.files)

  return <HomePage fileState={fileState} agentState={agentState} />
}

/**
 * Inner app — lives inside AuthProvider so hooks can call useAuth().
 */
function AppInner() {
  return (
    <>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route path="/projects" element={
          <ProtectedRoute>
            <ProjectsPage />
          </ProtectedRoute>
        } />
        <Route path="/project/:projectId" element={
          <ProtectedRoute>
            {/* key=projectId forces full remount on project switch */}
            <ProjectWorkspaceRoute />
          </ProtectedRoute>
        } />
        <Route path="/eval" element={
          <ProtectedRoute>
            <EvalDashboard />
          </ProtectedRoute>
        } />
      </Routes>
      <ToastContainer />
    </>
  )
}

/**
 * Thin bridge that reads projectId from the URL and passes it as the
 * React key to ProjectWorkspace, triggering a remount on project change.
 */
function ProjectWorkspaceRoute() {
  const { projectId } = useParams()
  return <ProjectWorkspace key={projectId} />
}

export default function App() {
  return (
    <AuthProvider>
      <AppInner />
    </AuthProvider>
  )
}
