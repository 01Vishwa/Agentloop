import React, { useEffect } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { AuthForm } from '../components/shared/AuthForm'
import { Cpu, Zap, Network, Bot } from 'lucide-react'

export function LandingPage() {
  const { isAuthenticated, loading } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  // Only auto-redirect to /projects if the user is authenticated AND we are
  // NOT arriving here as a result of a sign-out (which navigates to '/' while
  // Supabase is still clearing the session — isAuthenticated briefly stays true).
  const comingFromSignOut = location.state?.signedOut === true

  useEffect(() => {
    if (!loading && isAuthenticated && !comingFromSignOut) {
      navigate('/projects', { replace: true })
    }
  }, [isAuthenticated, loading, navigate, comingFromSignOut])

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#f8fafc]">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-brand-500"></div>
      </div>
    )
  }

  return (
    <div className="min-h-screen font-sans bg-[#f8fafc] text-slate-800 relative overflow-hidden flex">
      {/* Dynamic Background */}
      <div
        className="fixed inset-0 pointer-events-none z-0"
        style={{
          backgroundImage: `
            radial-gradient(circle at 15% 50%, rgba(59, 130, 246, 0.08) 0%, transparent 50%),
            radial-gradient(circle at 85% 30%, rgba(139, 92, 246, 0.08) 0%, transparent 50%),
            radial-gradient(circle at 50% 80%, rgba(16, 185, 129, 0.05) 0%, transparent 50%),
            linear-gradient(rgba(0,0,0,0.02) 1px, transparent 1px),
            linear-gradient(90deg, rgba(0,0,0,0.02) 1px, transparent 1px)
          `,
          backgroundSize: '100% 100%, 100% 100%, 100% 100%, 48px 48px, 48px 48px',
        }}
      />

      {/* Left side: Hero / Feature showcase */}
      <div className="hidden lg:flex flex-1 flex-col justify-center px-16 xl:px-24 relative z-10">
        <div className="max-w-xl">
          <div className="flex items-center gap-3 mb-8">
            <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-brand-500 to-violet-600
                            flex items-center justify-center shadow-lg shadow-brand-500/25">
              <Cpu size={24} className="text-white" />
            </div>
            <div>
              <h1 className="font-extrabold text-slate-900 text-3xl tracking-tight">Agentloop</h1>
              <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-brand-50 text-brand-600 uppercase tracking-wider">
                DS-STAR Platform
              </span>
            </div>
          </div>

          <h2 className="text-5xl font-extrabold tracking-tight text-slate-900 leading-[1.1] mb-6">
            Intelligent Data <br/>
            <span className="text-transparent bg-clip-text bg-gradient-to-r from-brand-600 to-violet-600">
              Processing &amp; Research
            </span>
          </h2>

          <p className="text-lg text-slate-600 mb-10 leading-relaxed">
            Agentloop autonomously interprets, plans, codes, executes, and verifies complex data operations. Step into the future of Agentic AI.
          </p>

          <div className="space-y-5">
            <div className="flex items-start gap-4">
              <div className="p-2.5 rounded-xl bg-white shadow-sm border border-slate-200/60 text-brand-500 shrink-0">
                <Zap size={20} />
              </div>
              <div>
                <h3 className="font-semibold text-slate-900">Autonomous Agent Loop</h3>
                <p className="text-sm text-slate-500 mt-1">Plan → Code → Execute → Verify → Route in a secure sandboxed environment.</p>
              </div>
            </div>

            <div className="flex items-start gap-4">
              <div className="p-2.5 rounded-xl bg-white shadow-sm border border-slate-200/60 text-violet-500 shrink-0">
                <Network size={20} />
              </div>
              <div>
                <h3 className="font-semibold text-slate-900">Deep Research Mode</h3>
                <p className="text-sm text-slate-500 mt-1">Decompose open-ended queries into parallel sub-questions and synthesize comprehensive reports.</p>
              </div>
            </div>

            <div className="flex items-start gap-4">
              <div className="p-2.5 rounded-xl bg-white shadow-sm border border-slate-200/60 text-emerald-500 shrink-0">
                <Bot size={20} />
              </div>
              <div>
                <h3 className="font-semibold text-slate-900">Isolated Workspaces</h3>
                <p className="text-sm text-slate-500 mt-1">Organize your data and agent runs into dedicated, secure projects.</p>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Right side: Auth Form */}
      <div className="flex-1 flex flex-col items-center justify-center p-6 relative z-10">
        <div className="w-full max-w-[420px] bg-white/80 backdrop-blur-xl border border-slate-200/80 rounded-2xl shadow-2xl shadow-slate-900/10 overflow-hidden relative">
          <div className="h-1.5 w-full bg-gradient-to-r from-brand-500 via-violet-500 to-brand-400" />
          <div className="px-8 pt-10 pb-10">
            <AuthForm onSuccess={() => navigate('/projects')} />
          </div>
        </div>
      </div>
    </div>
  )
}
