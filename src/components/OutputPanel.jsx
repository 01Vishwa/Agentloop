import React, { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import {
  Brain, Code2, Activity, ChevronDown, ChevronUp,
  CheckCircle2, Loader2, Clock, Lightbulb, ImageIcon, Download,
} from 'lucide-react'
import { CodeBlock } from './CodeBlock'

// Strip leading markdown/unicode bullet characters so CSS dot + embedded char don't double-up.
const cleanBullet = (s) => s.replace(/^[•\-*]\s*/, '').trim()

// ─── Status Panel ────────────────────────────────────────────────────────────
function StatusPanel({ status }) {
  const config = {
    idle:       { icon: Clock,        color: 'text-slate-500', bg: 'bg-slate-50', ring: 'ring-slate-200', label: 'Idle',       desc: 'Waiting for a query…' },
    processing: { icon: Loader2,      color: 'text-brand-600', bg: 'bg-brand-50', ring: 'ring-brand-200', label: 'Processing', desc: 'Analyzing your documents…' },
    completed:  { icon: CheckCircle2, color: 'text-emerald-600', bg: 'bg-emerald-50', ring: 'ring-emerald-200', label: 'Completed', desc: 'Results ready.' },
  }

  const { icon: Icon, color, bg, ring, label, desc } = config[status] || config.idle

  return (
    <div className={`flex items-center gap-3 px-4 py-3 rounded-xl bg-white shadow-sm ring-1 ${ring}`}>
      <div className={`w-8 h-8 rounded-lg ${bg} flex items-center justify-center`}>
        <Icon size={16} className={`${color} ${status === 'processing' ? 'animate-spin' : ''}`} />
      </div>
      <div>
        <p className={`text-sm font-semibold ${color}`}>{label}</p>
        <p className="text-[11px] text-slate-500 font-medium">{desc}</p>
      </div>
      {status === 'processing' && (
        <div className="ml-auto flex gap-1">
          {[0, 1, 2].map(i => (
            <span
              key={i}
              className="w-1.5 h-1.5 rounded-full bg-brand-400 animate-bounce"
              style={{ animationDelay: `${i * 0.15}s` }}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ─── Skeleton loader ──────────────────────────────────────────────────────────
function Skeleton() {
  return (
    <div className="space-y-3">
      {[100, 90, 95, 60].map((w, i) => (
        <div
          key={i}
          className="h-3 rounded shimmer-bg"
          style={{ width: `${w}%`, animationDelay: `${i * 0.1}s` }}
        />
      ))}
    </div>
  )
}

// ─── Artifacts Panel ─────────────────────────────────────────────────────────
/**
 * Renders base64-encoded artifact files emitted by the DS-STAR agent.
 * Visualization tasks produce PNG/JPG files; wrangling tasks produce CSVs.
 */
function ArtifactsPanel({ artifacts }) {
  if (!artifacts || artifacts.length === 0) return null

  const images = artifacts.filter(a =>
    a.mime_type?.startsWith('image/') || a.name?.match(/\.(png|jpg|jpeg|svg)$/i)
  )
  const downloads = artifacts.filter(a => !images.includes(a))

  return (
    <div className="glass-card shadow-sm overflow-hidden animate-slide-up bg-white" style={{ animationDelay: '0.05s' }}>
      <div className="flex items-center gap-3 px-5 py-4 border-b border-slate-100">
        <div className="w-8 h-8 rounded-lg bg-amber-50 border border-amber-100 flex items-center justify-center shadow-sm">
          <ImageIcon size={16} className="text-amber-600" />
        </div>
        <div>
          <p className="text-sm font-bold text-slate-800">Generated Artifacts</p>
          <p className="text-[11px] text-slate-500 font-medium">
            {artifacts.length} file{artifacts.length !== 1 ? 's' : ''} produced by the agent
          </p>
        </div>
      </div>

      <div className="px-5 py-4 space-y-4">
        {/* Image artifacts rendered inline */}
        {images.length > 0 && (
          <div className="grid gap-4">
            {images.map((artifact, idx) => (
              <div
                key={`img-${idx}`}
                id={`artifact-image-${idx}`}
                className="border border-slate-200 rounded-xl overflow-hidden shadow-sm"
              >
                <div className="flex items-center justify-between px-3 py-2 bg-slate-50 border-b border-slate-100">
                  <span className="text-[11px] font-semibold text-slate-600 truncate">
                    {artifact.name}
                  </span>
                  <a
                    href={`data:${artifact.mime_type};base64,${artifact.data}`}
                    download={artifact.name}
                    className="flex items-center gap-1 text-[10px] text-brand-600 hover:text-brand-800 font-semibold"
                    aria-label={`Download ${artifact.name}`}
                  >
                    <Download size={11} />
                    Download
                  </a>
                </div>
                <img
                  src={`data:${artifact.mime_type};base64,${artifact.data}`}
                  alt={artifact.name}
                  className="w-full object-contain max-h-[500px] bg-white"
                />
              </div>
            ))}
          </div>
        )}

        {/* Non-image artifacts as download links */}
        {downloads.length > 0 && (
          <div className="space-y-2">
            {downloads.map((artifact, idx) => (
              <a
                key={`dl-${idx}`}
                id={`artifact-download-${idx}`}
                href={`data:${artifact.mime_type};base64,${artifact.data}`}
                download={artifact.name}
                className="flex items-center gap-3 px-4 py-3 rounded-xl border border-slate-200 bg-slate-50
                           hover:bg-brand-50 hover:border-brand-200 transition-colors group"
                aria-label={`Download artifact ${artifact.name}`}
              >
                <Download size={14} className="text-slate-400 group-hover:text-brand-600 shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-[12px] font-semibold text-slate-700 truncate">{artifact.name}</p>
                  <p className="text-[10px] text-slate-400">{artifact.mime_type}</p>
                </div>
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Insights Panel ───────────────────────────────────────────────────────────
function InsightsPanel({ insights, isLoading, collapsed, onToggle }) {
  return (
    <div className="glass-card shadow-sm overflow-hidden animate-slide-up bg-white">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-5 py-4 hover:bg-slate-50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-violet-50 border border-violet-100 flex items-center justify-center shadow-sm">
            <Brain size={16} className="text-violet-600" />
          </div>
          <div className="text-left">
            <p className="text-sm font-bold text-slate-800">Extracted Insights</p>
            <p className="text-[11px] text-slate-500 font-medium">AI-powered analysis of your documents</p>
          </div>
        </div>
        <div className="w-7 h-7 flex items-center justify-center rounded-full bg-slate-50 border border-slate-200 text-slate-500 hover:text-slate-800">
          {collapsed ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
        </div>
      </button>

      {!collapsed && (
        <div className="px-5 pb-5 border-t border-slate-100 pt-4 animate-fade-in">
          {isLoading ? (
            <Skeleton />
          ) : insights ? (
            <div className="space-y-5 text-[13px] text-slate-700 leading-relaxed font-medium">
              {insights.summary && (
                <div className="p-3 bg-slate-50 border border-slate-100 rounded-xl
                                prose prose-sm max-w-none
                                prose-headings:text-slate-800 prose-headings:font-bold
                                prose-headings:mt-3 prose-headings:mb-1
                                prose-p:text-[13px] prose-p:text-slate-700 prose-p:leading-relaxed prose-p:my-1
                                prose-li:text-[13px] prose-li:text-slate-600
                                prose-strong:text-slate-800">
                  <ReactMarkdown>{insights.summary}</ReactMarkdown>
                </div>
              )}
              {insights.bullets?.length > 0 && (
                <ul className="space-y-2.5 px-1">
                  {insights.bullets.map((b, i) => (
                    <li key={i} className="flex gap-3 items-start">
                      <span className="mt-[7px] w-1.5 h-1.5 rounded-full bg-brand-500 shrink-0 shadow-sm" />
                      <span className="leading-relaxed">{cleanBullet(b)}</span>
                    </li>
                  ))}
                </ul>
              )}
              {insights.table && (
                <div className="overflow-x-auto rounded-xl border border-slate-200 mt-4 shadow-sm">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="bg-slate-50/80 border-b border-slate-200">
                        {insights.table.headers.map((h, i) => (
                          <th key={i} className="text-left px-4 py-3 text-slate-600 font-bold uppercase tracking-wider text-[10px]">
                            {h}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="bg-white">
                      {insights.table.rows.map((row, i) => (
                        <tr key={i} className="border-b border-slate-100 last:border-none hover:bg-slate-50">
                          {row.map((cell, j) => (
                            <td key={j} className="px-4 py-3 text-slate-800">{cell}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ) : (
            <div className="flex flex-col items-center py-8 gap-3">
              <div className="w-12 h-12 rounded-2xl bg-slate-50 border border-slate-100 flex items-center justify-center shadow-sm">
                <Lightbulb size={20} className="text-slate-400" />
              </div>
              <p className="text-sm text-slate-500 font-medium text-center">
                Submit a query above to see AI-generated insights
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Code Panel ───────────────────────────────────────────────────────────────
function GeneratedCodePanel({ codeByTab, isLoading, collapsed, onToggle }) {
  const [codeCollapsed, setCodeCollapsed] = useState(false)

  return (
    <div className="glass-card shadow-sm overflow-hidden animate-slide-up bg-white" style={{ animationDelay: '0.1s' }}>
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-5 py-4 hover:bg-slate-50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-brand-50 border border-brand-100 flex items-center justify-center shadow-sm">
            <Code2 size={16} className="text-brand-600" />
          </div>
          <div className="text-left">
            <p className="text-sm font-bold text-slate-800">Generated Code</p>
            <p className="text-[11px] text-slate-500 font-medium">Executable scripts for your analysis</p>
          </div>
        </div>
        <div className="w-7 h-7 flex items-center justify-center rounded-full bg-slate-50 border border-slate-200 text-slate-500 hover:text-slate-800">
          {collapsed ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
        </div>
      </button>

      {!collapsed && (
        <div className="px-5 pb-5 border-t border-slate-100 pt-4 animate-fade-in">
          {isLoading ? (
            <div className="h-40 shimmer-bg rounded-xl border border-slate-100" />
          ) : codeByTab ? (
            <CodeBlock
              codeByTab={codeByTab}
              collapsed={codeCollapsed}
              onToggleCollapse={() => setCodeCollapsed(c => !c)}
            />
          ) : (
            <div className="flex flex-col items-center py-8 gap-3">
              <div className="w-12 h-12 rounded-2xl bg-slate-50 border border-slate-100 flex items-center justify-center shadow-sm">
                <Code2 size={20} className="text-slate-400" />
              </div>
              <p className="text-sm text-slate-500 font-medium text-center">
                Generated code will appear here after analysis
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Main OutputPanel ─────────────────────────────────────────────────────────
/**
 * @param {object} props
 * @param {object} props.output      — Result payload from the agent
 * @param {string} props.status      — 'idle' | 'processing' | 'completed'
 * @param {Array}  props.artifacts   — Array of artifact objects {name, data, mime_type}
 */
export function OutputPanel({ output, status, artifacts = [] }) {
  const [insightsCollapsed, setInsightsCollapsed] = useState(false)
  const [codeCollapsed, setCodeCollapsed] = useState(false)

  const isLoading = status === 'processing'

  return (
    <div className="space-y-4">
      {/* Status */}
      <div className="space-y-2">
        <div className="section-header px-1">
          <Activity size={13} className="text-brand-500" />
          <span className="text-slate-600">Processing Status</span>
        </div>
        <StatusPanel status={status} />
      </div>

      {/* Artifact images — rendered as soon as they arrive during streaming */}
      <ArtifactsPanel artifacts={artifacts} />

      {/* Output sections */}
      <InsightsPanel
        insights={output?.insights}
        isLoading={isLoading}
        collapsed={insightsCollapsed}
        onToggle={() => setInsightsCollapsed(c => !c)}
      />
      <GeneratedCodePanel
        codeByTab={output?.code}
        isLoading={isLoading}
        collapsed={codeCollapsed}
        onToggle={() => setCodeCollapsed(c => !c)}
      />
    </div>
  )
}
