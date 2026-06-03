import React, { useState, useEffect } from 'react'
import { Cpu, Zap, BarChart2, LogIn, LogOut, User, ArrowLeft } from 'lucide-react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { FileUploadPanel } from '../components/upload/FileUploadPanel'
import { QueryInput } from '../components/query/QueryInput'
import { AgentProgressPanel } from '../components/agent/AgentProgressPanel'
import { HistoryPanel } from '../components/agent/HistoryPanel'
import { AgentSettings } from '../components/agent/AgentSettings'
import { ConfirmDialog } from '../components/shared/ConfirmDialog'
import { AuthModal } from '../components/shared/AuthModal'
import { useAuth } from '../contexts/AuthContext'
import { useWorkspaceStore } from '../stores/workspaceStore'

export function HomePage({ fileState, agentState }) {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const { workspaces, fetchWorkspaces, setActiveWorkspace, activeWorkspace } = useWorkspaceStore()

  const {
    files,
    filesLoading,
    pendingDuplicates,
    sessionId,
    handleAddFiles,
    handleConfirmDuplicates,
    handleCancelDuplicates,
    handleRemoveFile,
    handleClearAll,
  } = fileState

  const {
    agentStatus,
    phase,
    planSteps,
    currentCode,
    executionLogs,
    currentRound,
    maxRounds,
    output: agentOutput,
    verifierFeedback,
    artifacts,
    historyRuns,
    historyLoading,
    activeRunId,
    settings,
    setSettings,
    handleSubmit,
    rerunLastQuery,
    handleReset,
    fetchHistory,
    loadRun,
  } = agentState

  const { user, isAuthenticated, signOut, loading: authLoading } = useAuth()
  const [showAuthModal, setShowAuthModal] = useState(false)

  // Redirect if not authenticated
  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      navigate('/', { replace: true })
    }
  }, [authLoading, isAuthenticated, navigate])

  // Sync workspace from URL parameter
  useEffect(() => {
    if (user?.id) {
      if (workspaces.length === 0) {
        fetchWorkspaces(user.id)
      } else if (projectId) {
        const ws = workspaces.find((w) => w.id === projectId)
        if (ws && (!activeWorkspace || activeWorkspace.id !== projectId)) {
          setActiveWorkspace(ws)
        }
      }
    }
  }, [user?.id, workspaces, projectId, fetchWorkspaces, setActiveWorkspace, activeWorkspace])

  const isProcessing = !['idle', 'completed', 'failed'].includes(agentStatus)

  return (
    <div className="min-h-screen font-sans bg-[#f8fafc] text-slate-800">
      {/* Background grid */}
      <div
        className="fixed inset-0 pointer-events-none"
        style={{
          backgroundImage: `
            radial-gradient(ellipse 80% 50% at 20% 10%, rgba(59,78,248,0.04) 0%, transparent 60%),
            radial-gradient(ellipse 60% 40% at 80% 70%, rgba(139,92,246,0.03) 0%, transparent 60%),
            linear-gradient(rgba(0,0,0,0.03) 1px, transparent 1px),
            linear-gradient(90deg, rgba(0,0,0,0.03) 1px, transparent 1px)
          `,
          backgroundSize: '100% 100%, 100% 100%, 48px 48px, 48px 48px',
        }}
      />

      {/* ── Navbar ── */}
      <header className="relative z-20 border-b border-slate-200/80 bg-white/70 backdrop-blur-md">
        <div className="max-w-[1600px] mx-auto px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate('/projects')}
              className="p-2 rounded-xl text-slate-500 hover:bg-slate-100 transition-colors duration-150 mr-2"
              title="Back to Projects"
            >
              <ArrowLeft size={18} />
            </button>
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-brand-500 to-violet-600
                            flex items-center justify-center shadow-md">
              <Cpu size={18} className="text-white" />
            </div>
            <div>
              <span className="font-bold text-slate-800 text-base tracking-tight">Agentloop</span>
              <span className="ml-2 text-[10px] font-semibold px-1.5 py-0.5 rounded-full
                               bg-brand-50 text-brand-600 uppercase tracking-wider">
                DS-STAR
              </span>
            </div>
          </div>

          <div className="hidden md:flex items-center gap-2 text-xs text-slate-500 font-medium">
            <Zap size={12} className="text-brand-500" />
            Project: {activeWorkspace?.name || 'Loading...'}
          </div>

          <div className="flex items-center gap-3">

            <AgentSettings
              settings={settings}
              onChange={setSettings}
              disabled={isProcessing}
              placement="header"
            />

            {/* ── Auth control ── */}
            {!authLoading && (
              isAuthenticated ? (
                <div className="flex items-center gap-2">
                  <div
                    id="user-avatar-chip"
                    className="flex items-center gap-2 px-3 py-1.5 rounded-xl border border-slate-200
                               bg-white text-[12px] font-medium text-slate-700 shadow-sm"
                    title={user?.email}
                  >
                    <User size={13} className="text-brand-500" />
                    <span className="hidden sm:block max-w-[120px] truncate">
                      {user?.email?.split('@')[0]}
                    </span>
                  </div>
                  <button
                    id="sign-out-btn"
                    onClick={async () => { await signOut(); navigate('/', { state: { signedOut: true } }); }}
                    className="btn-ghost px-3 text-[12px]"
                    title="Sign out"
                  >
                    <LogOut size={15} />
                  </button>
                </div>
              ) : (
                <button
                  id="sign-in-btn"
                  onClick={() => setShowAuthModal(true)}
                  className="flex items-center gap-2 px-3 py-1.5 rounded-xl
                             bg-brand-500 text-white text-[12px] font-semibold shadow-sm
                             hover:bg-brand-600 transition-colors duration-150"
                >
                  <LogIn size={13} />
                  Sign In
                </button>
              )
            )}
          </div>
        </div>
      </header>

      {/* ── Main Layout ── */}
      <main className="relative z-10 max-w-[1600px] mx-auto px-6 py-6
                       h-[calc(100vh-61px)] flex gap-6">

        {/* LEFT — File Upload Panel (32%) */}
        <aside className="w-[32%] shrink-0 flex flex-col min-h-0">
          <div className="glass-card-elevated h-full flex flex-col min-h-0 p-5">
            <FileUploadPanel
              files={files}
              filesLoading={filesLoading}
              onAddFiles={handleAddFiles}
              onRemoveFile={handleRemoveFile}
              onClearAll={handleClearAll}
            />
          </div>
        </aside>

        {/* RIGHT — Query + Output (68%) */}
        <section className="flex-1 flex flex-col gap-5 overflow-y-auto">

          {/* Query input */}
          <div className="glass-card-elevated p-5 shrink-0">
            <QueryInput
              onSubmit={(query) => handleSubmit(query, sessionId)}
              isProcessing={isProcessing}
              fileCount={files.filter((f) => f.progress === 100).length}
              placeholder="Ask DS-STAR anything about your data…"
            />
          </div>

          {/* Output area */}
          <div className="flex-1 space-y-4">
            <HistoryPanel
              historyRuns={historyRuns}
              historyLoading={historyLoading}
              fetchHistory={fetchHistory}
              loadRun={loadRun}
            />
            <AgentProgressPanel
              phase={phase}
              agentStatus={agentStatus}
              planSteps={planSteps}
              currentCode={currentCode}
              executionLogs={executionLogs}
              currentRound={currentRound}
              maxRounds={maxRounds}
              output={agentOutput}
              verifierFeedback={verifierFeedback}
              artifacts={artifacts}
              activeRunId={activeRunId}
              onReset={handleReset}
              onRerun={() => rerunLastQuery(sessionId)}
            />
          </div>
        </section>
      </main>

      <ConfirmDialog
        isOpen={pendingDuplicates.length > 0}
        duplicates={pendingDuplicates}
        onConfirm={handleConfirmDuplicates}
        onCancel={handleCancelDuplicates}
      />

      <AuthModal
        isOpen={showAuthModal}
        onClose={() => setShowAuthModal(false)}
      />
    </div>
  )
}
