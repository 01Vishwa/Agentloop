import React, { useRef, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import {
  Brain, Terminal, GitBranch, Layers, RefreshCw,
  ChevronDown, ChevronUp, Cpu, CheckCircle2, XCircle,
  Loader2, Sparkles, Image, FileText, Bug, FileCheck, Network, Activity, Edit3, LayoutList, Download
} from 'lucide-react'
import { useAuth } from '../../contexts/AuthContext'
import { toast } from '../shared/Toast'
import { PlanStepList } from './PlanStepList'
import { CodeBlock } from './CodeBlock'

// Strip leading markdown/unicode bullet characters from a string so we don't
// render a CSS dot AND an embedded '• ' or '* ' simultaneously.
const cleanBullet = (s) => s.replace(/^[•\-*]\s*/, '').trim()

// ─── Phase meta ─────────────────────────────────────────────────────────────
const PHASE_META = {
  idle:       { label: 'Idle',       cls: 'phase-planning',   icon: Cpu },
  analyzing:  { label: 'Analyzing',  cls: 'phase-analyzing',  icon: Brain },
  planning:   { label: 'Planning',   cls: 'phase-planning',   icon: Layers },
  coding:     { label: 'Coding',     cls: 'phase-coding',     icon: GitBranch },
  executing:  { label: 'Executing',  cls: 'phase-executing',  icon: Terminal },
  verifying:  { label: 'Verifying',  cls: 'phase-verifying',  icon: CheckCircle2 },
  routing:    { label: 'Routing',    cls: 'phase-routing',    icon: RefreshCw },
  completed:  { label: 'Completed',  cls: 'phase-completed',  icon: Sparkles },
  error:      { label: 'Error',      cls: 'phase-error',      icon: XCircle },
  debugging:  { label: 'Debugging',  cls: 'phase-coding',     icon: Bug },
  finalizing: { label: 'Finalizing', cls: 'phase-routing',    icon: FileCheck },
  generating_subquestions: { label: 'Decomposing', cls: 'phase-analyzing', icon: Network },
  running_subquestions:    { label: 'Parallel Research', cls: 'phase-executing', icon: Activity },
  writing_report:          { label: 'Writing Report', cls: 'phase-coding', icon: Edit3 },
}

const isActive = (s) => !['idle', 'completed', 'failed', 'error'].includes(s)

// ─── Phase Badge ─────────────────────────────────────────────────────────────
function PhaseBadge({ phase }) {
  const meta = PHASE_META[phase] || PHASE_META.idle
  const Icon = meta.icon
  const spinning = isActive(phase)
  return (
    <span className={`agent-phase ${meta.cls} relative`}>
      <Icon size={11} className={spinning ? 'animate-spin' : ''} />
      {meta.label}
      {spinning && (
        <span className="ml-1 flex gap-0.5">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="w-1 h-1 rounded-full bg-current animate-bounce"
              style={{ animationDelay: `${i * 0.15}s` }}
            />
          ))}
        </span>
      )}
    </span>
  )
}

// ─── Round Counter ───────────────────────────────────────────────────────────
function RoundCounter({ current, max }) {
  if (current === 0) return null
  return (
    <div className="flex items-center gap-1.5">
      {Array.from({ length: max }, (_, i) => (
        <span
          key={i}
          className={`w-2.5 h-2.5 rounded-full transition-all duration-300 ${
            i < current
              ? 'bg-brand-500 scale-100'
              : i === current
              ? 'bg-brand-300 scale-110 animate-pulse'
              : 'bg-slate-200 scale-90'
          }`}
        />
      ))}
      <span className="text-[10px] text-slate-500 font-medium ml-1">
        {current}/{max}
      </span>
    </div>
  )
}

// ─── Terminal Log ─────────────────────────────────────────────────────────────
function AgentTerminal({ logs }) {
  const bottomRef = useRef(null)
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs])

  const typeClass = (type) => {
    switch (type) {
      case 'success': return 'log-success'
      case 'warn':    return 'log-warn'
      case 'error':   return 'log-error'
      default:        return 'log-info'
    }
  }
  return (
    <div className="agent-terminal">
      {logs.length === 0 ? (
        <span className="log-info">$ Waiting for agent events…</span>
      ) : (
        logs.map((log, i) => (
          <div key={i} className={typeClass(log.type)}>
            <span className="opacity-40 mr-2 select-none">
              {String(i + 1).padStart(2, '0')}
            </span>
            {log.text}
          </div>
        ))
      )}
      <div ref={bottomRef} />
    </div>
  )
}

// ─── Verifier Verdict ─────────────────────────────────────────────────────────
function VerifierCard({ feedback }) {
  if (!feedback) return null
  const ok = feedback.isSufficient
  return (
    <div
      className={`flex items-start gap-3 p-3 rounded-xl border text-[12px] font-medium
        ${ok
          ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
          : 'bg-amber-50 border-amber-200 text-amber-800'}`}
    >
      {ok
        ? <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-emerald-600" />
        : <RefreshCw size={15} className="mt-0.5 shrink-0 text-amber-600" />}
      <div>
        <span className="font-bold">{ok ? 'Verified ✓' : 'Refining'}</span>
        {' — '}
        {feedback.reason}
        {feedback.confidence !== undefined && (
          <span className="ml-2 opacity-60 text-[11px]">
            ({Math.round(feedback.confidence * 100)}% confidence)
          </span>
        )}
      </div>
    </div>
  )
}

// ─── Artifact Gallery (fix #4) ───────────────────────────────────────────────
function ArtifactGallery({ artifacts, collapsed, onToggle }) {
  if (!artifacts || artifacts.length === 0) return null

  return (
    <div className="glass-card-elevated overflow-hidden animate-slide-up">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-5 py-4
                   hover:bg-slate-50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="w-7 h-7 rounded-lg bg-violet-50 border border-violet-100 flex items-center justify-center">
            <Image size={14} className="text-violet-600" />
          </div>
          <div className="text-left">
            <p className="text-[13px] font-bold text-slate-800">Artifacts</p>
            <p className="text-[11px] text-slate-500 font-medium">
              {artifacts.length} file{artifacts.length !== 1 ? 's' : ''} captured
            </p>
          </div>
        </div>
        <div className="w-6 h-6 flex items-center justify-center rounded-full
                        bg-slate-50 border border-slate-200 text-slate-400">
          {collapsed ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
        </div>
      </button>

      {!collapsed && (
        <div className="p-5 grid gap-4 border-t border-slate-100 animate-fade-in">
          {artifacts.map((art, i) => {
            const isImage = art.mime_type?.startsWith('image/')
            const isCSV   = art.mime_type === 'text/csv' || art.name?.endsWith('.csv')
            const src     = `data:${art.mime_type};base64,${art.data}`

            return (
              <div key={i} className="rounded-xl border border-slate-100 overflow-hidden bg-slate-50">
                {/* File name header */}
                <div className="flex items-center gap-2 px-4 py-2 bg-white border-b border-slate-100">
                  {isImage
                    ? <Image size={12} className="text-violet-500" />
                    : <FileText size={12} className="text-emerald-500" />}
                  <span className="text-[12px] font-mono font-semibold text-slate-700">
                    {art.name}
                  </span>
                  <a
                    href={src}
                    download={art.name}
                    className="ml-auto text-[11px] text-brand-600 hover:text-brand-700 font-semibold"
                  >
                    Download
                  </a>
                </div>
                {/* Render image inline; CSV gets a download-only card */}
                {isImage ? (
                  <img
                    src={src}
                    alt={art.name}
                    className="w-full max-h-96 object-contain bg-white p-2"
                  />
                ) : isCSV ? (
                  <div className="flex items-center gap-2 px-4 py-4 text-[12px] text-slate-500">
                    <FileText size={14} className="text-emerald-400" />
                    CSV data file — use the Download button above to open in Excel.
                  </div>
                ) : (
                  <div className="px-4 py-4 text-[12px] text-slate-400 font-mono">
                    Binary file — download to view.
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ─── Sub-Questions List ───────────────────────────────────────────────────────
function SubQuestionsList({ questions }) {
  if (!questions || questions.length === 0) return null
  return (
    <div className="space-y-2">
      {questions.map((q, i) => {
        let statusIcon
        let statusCls = ''
        switch (q.status) {
          case 'pending':
            statusIcon = <div className="w-1.5 h-1.5 rounded-full bg-slate-300" />
            break
          case 'running':
            statusIcon = <Loader2 size={12} className="animate-spin text-brand-500" />
            statusCls = 'border-brand-200 bg-brand-50 shadow-sm'
            break
          case 'completed':
            statusIcon = <CheckCircle2 size={13} className="text-emerald-500" />
            statusCls = 'bg-emerald-50/50 border-emerald-100'
            break
          default: // failed or max_rounds_reached
            statusIcon = <XCircle size={13} className="text-amber-500" />
            statusCls = 'bg-amber-50/50 border-amber-100'
            break
        }

        return (
          <div key={i} className={`flex items-start gap-3 p-3 rounded-lg border border-slate-100 bg-white transition-colors ${statusCls}`}>
            <div className="mt-0.5 shrink-0 w-4 flex items-center justify-center">
              {statusIcon}
            </div>
            <div className="flex-1">
              <p className="text-[13px] text-slate-700 font-medium leading-relaxed">{q.question}</p>
              <div className="mt-1 flex items-center gap-2 text-[11px] font-semibold tracking-wider uppercase">
                <span className="text-slate-400">Sub-Question {i + 1}</span>
                {q.status !== 'pending' && <span className="opacity-50">·</span>}
                {q.status === 'running' && <span className="text-brand-600">Running</span>}
                {q.status === 'completed' && <span className="text-emerald-600">Completed</span>}
                {(q.status !== 'pending' && q.status !== 'running' && q.status !== 'completed') && (
                  <span className="text-amber-600">{q.status.replace(/_/g, ' ')}</span>
                )}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ─── Research Report View ─────────────────────────────────────────────────────
function ResearchReportView({ report }) {
  if (!report) return null
  return (
    <div className="space-y-6">
      <div className="pb-4 border-b border-slate-100">
        <h2 className="text-lg font-bold text-slate-800 mb-2">{report.title}</h2>
        <p className="text-[13px] text-slate-600 leading-relaxed font-medium">
          {report.executive_summary}
        </p>
      </div>
      
      {report.key_findings?.length > 0 && (
        <div>
          <h3 className="text-sm font-bold text-slate-800 mb-3 flex items-center gap-2">
            <Sparkles size={14} className="text-brand-500" />
            Key Findings
          </h3>
          <ul className="space-y-2">
            {report.key_findings.map((finding, i) => (
              <li key={i} className="flex items-start gap-2 text-[13px] text-slate-600">
                <span className="w-1.5 h-1.5 rounded-full bg-brand-500 shrink-0 mt-1.5" />
                <span className="leading-relaxed">{finding}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.report_body && (
        <div className="prose prose-sm max-w-none text-slate-700">
          <React.Fragment>
            {report.report_body.split('\n\n').map((paragraph, i) => (
               <p key={i} className="text-[13px] leading-relaxed mb-4">{paragraph}</p>
            ))}
          </React.Fragment>
        </div>
      )}

      {report.caveats?.length > 0 && (
        <div className="bg-amber-50 rounded-xl p-4 border border-amber-100">
          <h3 className="text-[12px] font-bold text-amber-800 uppercase tracking-wider mb-2 flex items-center gap-2">
            <XCircle size={12} className="text-amber-500" />
            Caveats & Limitations
          </h3>
          <ul className="space-y-1.5 list-disc pl-4">
            {report.caveats.map((cav, i) => (
              <li key={i} className="text-[12px] text-amber-700/80">{cav}</li>
            ))}
          </ul>
        </div>
      )}
      
      {report.total_ms > 0 && (
        <div className="pt-2 text-right text-[11px] text-slate-400 font-mono">
          Research generated in {Math.round(report.total_ms / 1000)}s
        </div>
      )}
    </div>
  )
}

// ─── Main Component ──────────────────────────────────────────────────────────
export function AgentProgressPanel({
  phase,
  agentStatus,
  planSteps,
  currentCode,
  executionLogs,
  currentRound,
  maxRounds,
  output,
  verifierFeedback,
  artifacts,
  activeRunId,
  onReset,
  onRerun,
  // Deep Research mode props
  isResearchMode,
  subQuestions,
  researchReport,
}) {
  const [planCollapsed, setPlanCollapsed] = useState(false)
  const [logsCollapsed, setLogsCollapsed] = useState(false)
  const [codeCollapsed, setCodeCollapsed] = useState(false)
  const [artifactsCollapsed, setArtifactsCollapsed] = useState(false)
  const [insightsCollapsed, setInsightsCollapsed] = useState(false)
  const [reportCollapsed, setReportCollapsed] = useState(false)
  const [isDownloading, setIsDownloading] = useState(false)
  const { getAccessToken } = useAuth()

  const busy   = isActive(agentStatus)
  const done   = agentStatus === 'completed'
  const failed = agentStatus === 'failed'

  const handleDownload = async () => {
    if (!activeRunId) return
    setIsDownloading(true)
    try {
      const token = getAccessToken()
      const res = await fetch(`/api/agent/runs/${activeRunId}/download`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {}
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `agentloop-run-${activeRunId}.zip`
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)
    } catch (err) {
      toast('Failed to download artifacts: ' + err.message, 'error')
    } finally {
      setIsDownloading(false)
    }
  }

  return (
    <div className="space-y-4">

      {/* ── Header strip ───────────────────────────────────────────────── */}
      <div className="glass-card-elevated p-4 flex items-center justify-between gap-4">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="w-8 h-8 rounded-xl bg-gradient-to-br from-brand-500 to-violet-600
                          flex items-center justify-center shadow-md shrink-0">
            <Brain size={16} className="text-white" />
          </div>
          <div>
            <p className="text-sm font-bold text-slate-800 leading-tight">DS-STAR Agent</p>
            <p className="text-[11px] text-slate-500 font-medium">
              Plan → Code → Execute → Verify
            </p>
          </div>
          <PhaseBadge phase={phase} />
          <RoundCounter current={currentRound} max={maxRounds} />
        </div>

        {(done || failed) && (
          <div className="flex items-center gap-2">
            {done && activeRunId && (
              <button
                onClick={handleDownload}
                disabled={isDownloading}
                className="btn-ghost text-[12px] gap-1.5 shrink-0"
                title="Download Artifacts (.zip)"
              >
                {isDownloading ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
                Download ZIP
              </button>
            )}
            <button
              id="agent-reset-btn"
              onClick={onRerun || onReset}
              className="btn-ghost text-[12px] gap-1.5 shrink-0"
              title="Run the previous query again"
            >
              <RefreshCw size={13} />
              Re-run
            </button>
          </div>
        )}
      </div>

      {/* ── Deep Research View ─────────────────────────────────────────── */}
      {isResearchMode ? (
        <>
          {subQuestions.length > 0 && (
            <div className="glass-card-elevated overflow-hidden">
              <button
                onClick={() => setPlanCollapsed((c) => !c)}
                className="w-full flex items-center justify-between px-5 py-4
                           hover:bg-slate-50 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <div className="w-7 h-7 rounded-lg bg-indigo-50 border border-indigo-100
                                  flex items-center justify-center">
                    <LayoutList size={14} className="text-indigo-600" />
                  </div>
                  <div className="text-left">
                    <p className="text-[13px] font-bold text-slate-800">Sub-Questions</p>
                    <p className="text-[11px] text-slate-500 font-medium">
                      {subQuestions.length} concurrent research threads
                    </p>
                  </div>
                </div>
                <div className="w-6 h-6 flex items-center justify-center rounded-full
                                bg-slate-50 border border-slate-200 text-slate-400">
                  {planCollapsed ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
                </div>
              </button>
              {!planCollapsed && (
                <div className="px-5 pb-5 border-t border-slate-100 pt-4 animate-fade-in">
                  <SubQuestionsList questions={subQuestions} />
                </div>
              )}
            </div>
          )}

          {researchReport && (
            <div className="glass-card-elevated overflow-hidden animate-slide-up">
              <button
                onClick={() => setReportCollapsed((c) => !c)}
                className="w-full flex items-center justify-between px-5 py-4
                           hover:bg-slate-50 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <div className="w-7 h-7 rounded-lg bg-brand-50 border border-brand-100
                                  flex items-center justify-center">
                    <Sparkles size={14} className="text-brand-600" />
                  </div>
                  <div className="text-left">
                    <p className="text-[13px] font-bold text-slate-800">Research Report</p>
                    <p className="text-[11px] text-slate-500 font-medium">
                      Finalized comprehensive overview
                    </p>
                  </div>
                </div>
                <div className="w-6 h-6 flex items-center justify-center rounded-full
                                bg-slate-50 border border-slate-200 text-slate-400">
                  {reportCollapsed ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
                </div>
              </button>
              {!reportCollapsed && (
                <div className="px-5 pb-5 border-t border-slate-100 pt-4 animate-fade-in">
                  <ResearchReportView report={researchReport} />
                </div>
              )}
            </div>
          )}
        </>
      ) : (
      /* ── Standard Mode Plan Steps ────────────────────────────────────── */
      <div className="glass-card-elevated overflow-hidden">
        <button
          onClick={() => setPlanCollapsed((c) => !c)}
          className="w-full flex items-center justify-between px-5 py-4
                     hover:bg-slate-50 transition-colors"
        >
          <div className="flex items-center gap-3">
            <div className="w-7 h-7 rounded-lg bg-blue-50 border border-blue-100
                            flex items-center justify-center">
              <Layers size={14} className="text-blue-600" />
            </div>
            <div className="text-left">
              <p className="text-[13px] font-bold text-slate-800">Analysis Plan</p>
              <p className="text-[11px] text-slate-500 font-medium">
                {planSteps.length > 0
                  ? `${planSteps.length} steps`
                  : 'Waiting for planner…'}
              </p>
            </div>
          </div>
          <div className="w-6 h-6 flex items-center justify-center rounded-full
                          bg-slate-50 border border-slate-200 text-slate-400">
            {planCollapsed ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
          </div>
        </button>
        {!planCollapsed && (
          <div className="px-5 pb-5 border-t border-slate-100 pt-4 animate-fade-in">
            <PlanStepList steps={planSteps} currentRound={currentRound} />
          </div>
        )}
      </div>
      )}

      {/* ── Verifier Card ─────────────────────────────────────────────── */}
      {verifierFeedback && (
        <div className="animate-slide-up">
          <VerifierCard feedback={verifierFeedback} />
        </div>
      )}

      {/* ── Execution Logs ────────────────────────────────────────────── */}
      <div className="glass-card-elevated overflow-hidden">
        <button
          onClick={() => setLogsCollapsed((c) => !c)}
          className="w-full flex items-center justify-between px-5 py-4
                     hover:bg-slate-50 transition-colors"
        >
          <div className="flex items-center gap-3">
            <div className="w-7 h-7 rounded-lg bg-slate-800 border border-slate-700
                            flex items-center justify-center">
              <Terminal size={14} className="text-slate-300" />
            </div>
            <div className="text-left">
              <p className="text-[13px] font-bold text-slate-800">Execution Log</p>
              <p className="text-[11px] text-slate-500 font-medium">
                {executionLogs.length} entries
                {busy && (
                  <Loader2 size={10} className="inline ml-1.5 animate-spin text-brand-500" />
                )}
              </p>
            </div>
          </div>
          <div className="w-6 h-6 flex items-center justify-center rounded-full
                          bg-slate-50 border border-slate-200 text-slate-400">
            {logsCollapsed ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
          </div>
        </button>
        {!logsCollapsed && (
          <div className="px-5 pb-5 border-t border-slate-100 pt-4 animate-fade-in">
            <AgentTerminal logs={executionLogs} />
          </div>
        )}
      </div>

      {/* ── Generated Code (Hidden in Research Mode) ──────────────────── */}
      {!isResearchMode && currentCode && (
        <div className="glass-card-elevated overflow-hidden animate-slide-up">
          <button
            onClick={() => setCodeCollapsed((c) => !c)}
            className="w-full flex items-center justify-between px-5 py-4
                       hover:bg-slate-50 transition-colors"
          >
            <div className="flex items-center gap-3">
              <div className="w-7 h-7 rounded-lg bg-brand-50 border border-brand-100
                              flex items-center justify-center">
                <GitBranch size={14} className="text-brand-600" />
              </div>
              <div className="text-left">
                <p className="text-[13px] font-bold text-slate-800">Generated Code</p>
                <p className="text-[11px] text-slate-500 font-medium">
                  Latest Python script — {currentCode.split('\n').length} lines
                </p>
              </div>
            </div>
            <div className="w-6 h-6 flex items-center justify-center rounded-full
                            bg-slate-50 border border-slate-200 text-slate-400">
              {codeCollapsed ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
            </div>
          </button>
          {!codeCollapsed && (
            <div className="px-5 pb-5 border-t border-slate-100 pt-4 animate-fade-in">
              <CodeBlock
                codeByTab={{ Python: currentCode }}
                collapsed={false}
                onToggleCollapse={() => {}}
              />
            </div>
          )}
        </div>
      )}

      {/* ── Artifact Gallery (fix #4) ─────────────────────────────────── */}
      <ArtifactGallery
        artifacts={artifacts}
        collapsed={artifactsCollapsed}
        onToggle={() => setArtifactsCollapsed(!artifactsCollapsed)}
      />

      {/* ── Final Insights (only when completed, hidden in research mode) */}
      {!isResearchMode && done && output?.insights && (
        <div className="glass-card-elevated overflow-hidden animate-slide-up">
          <button
            onClick={() => setInsightsCollapsed((c) => !c)}
            className="w-full flex items-center justify-between px-5 py-4
                       hover:bg-slate-50 transition-colors"
          >
            <div className="flex items-center gap-3">
              <div className="w-7 h-7 rounded-lg bg-violet-50 border border-violet-100
                              flex items-center justify-center">
                <Sparkles size={14} className="text-violet-600" />
              </div>
              <div className="text-left">
                <p className="text-[13px] font-bold text-slate-800">Final Insights</p>
                <p className="text-[11px] text-slate-500 font-medium">
                  Completed in {output.rounds} round(s)
                </p>
              </div>
            </div>
            <div className="w-6 h-6 flex items-center justify-center rounded-full
                            bg-slate-50 border border-slate-200 text-slate-400">
              {insightsCollapsed ? <ChevronDown size={13} /> : <ChevronUp size={13} />}
            </div>
          </button>
          {!insightsCollapsed && (
          <div className="px-5 pb-5 border-t border-slate-100 pt-4 space-y-4 animate-fade-in">
              {output.insights.summary && (
                <div className="p-4 bg-slate-50 border border-slate-100 rounded-xl overflow-x-auto
                                prose prose-sm max-w-none
                                prose-headings:text-slate-800 prose-headings:font-bold
                                prose-headings:mt-3 prose-headings:mb-1
                                prose-p:text-[12px] prose-p:text-slate-700 prose-p:leading-relaxed prose-p:whitespace-pre-wrap prose-p:font-mono
                                prose-li:text-[13px] prose-li:text-slate-600
                                prose-strong:text-slate-800">
                  <ReactMarkdown>{output.insights.summary}</ReactMarkdown>
                </div>
              )}
              {output.insights.bullets?.length > 0 && (
                <ul className="space-y-2.5 px-1">
                  {output.insights.bullets.map((b, i) => (
                    <li key={i} className="flex gap-3 items-start text-[13px] text-slate-600">
                      <span className="mt-[7px] w-1.5 h-1.5 rounded-full bg-brand-500 shrink-0" />
                      <span className="leading-relaxed">{cleanBullet(b)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Idle empty state ─────────────────────────────────────────── */}
      {agentStatus === 'idle' && (
        <div className="glass-card flex flex-col items-center justify-center py-14 gap-4
                        border-dashed border-slate-200">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-brand-50 to-violet-50
                          border border-slate-200 flex items-center justify-center shadow-sm">
            <Brain size={24} className="text-slate-300" />
          </div>
          <div className="text-center">
            <p className="text-sm font-semibold text-slate-500">DS-STAR Agent Ready</p>
            <p className="text-[12px] text-slate-400 mt-1 font-medium">
              Upload files and submit a query to start
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
