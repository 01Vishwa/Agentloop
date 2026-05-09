import React, { useState, useEffect, useRef } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ThemeToggle } from '../components/shared/ThemeToggle'
import { useAuth } from '../contexts/AuthContext'
import { KpiCard } from '../components/eval/KpiCard'
import { AgentTable } from '../components/eval/AgentTable'
import { RunList } from '../components/eval/RunList'
import { TracePanel } from '../components/eval/TracePanel'
import { DebugChart } from '../components/eval/DebugChart'
import {
  getOverview,
  getAgentPerformance,
  getDebugLoopStats,
  listEvalRuns,
  getRunTrace,
} from '../services/evalApi'
import {
  Activity,
  Clock,
  RefreshCw,
  CheckCircle2,
  ArrowLeft,
  BarChart2,
  Cpu,
  Bug,
  Search,
} from 'lucide-react'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  BarChart,
  Bar,
  Cell,
  PieChart,
  Pie,
  Legend,
  RadarChart,
  Radar,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  AreaChart,
  Area,
} from 'recharts'

const BRAND_COLORS = ['#7c3aed', '#ec4899', '#f59e0b', '#10b981', '#3b82f6', '#6366f1', '#ef4444', '#14b8a6']

const NAV_SECTIONS = [
  { id: 'overview',    label: 'System Overview',   icon: Activity },
  { id: 'performance', label: 'Agent Performance',  icon: Cpu },
  { id: 'debug',       label: 'Debug Loop',          icon: Bug },
  { id: 'explorer',   label: 'Run Explorer',        icon: Search },
]

export function EvalDashboard() {
  const [overview,   setOverview]   = useState(null)
  const [agents,     setAgents]     = useState([])
  const [debugStats, setDebugStats] = useState(null)
  const [runs,       setRuns]       = useState([])
  const [selectedRunId, setSelectedRunId] = useState(null)
  const [traceData,  setTraceData]  = useState(null)
  const [loading,    setLoading]    = useState(true)
  const [activeSection, setActiveSection] = useState('overview')

  const { isAuthenticated, loading: authLoading, getAccessToken } = useAuth()
  const navigate = useNavigate()

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      navigate('/', { replace: true })
    }
  }, [authLoading, isAuthenticated, navigate])

  const sectionRefs = {
    overview:    useRef(null),
    performance: useRef(null),
    debug:       useRef(null),
    explorer:    useRef(null),
  }

  /* ── Initial data load ── */
  useEffect(() => {
    async function load() {
      try {
        setLoading(true)
        const token = getAccessToken()
        const [ov, ag, ds, rl] = await Promise.all([
          getOverview(token),
          getAgentPerformance(token),
          getDebugLoopStats(token),
          listEvalRuns({}, token),
        ])
        setOverview(ov)
        setAgents(ag)
        setDebugStats(ds)
        setRuns(rl)
      } catch (err) {
        console.error('Dashboard load error', err)
      } finally {
        setLoading(false)
      }
    }
    load()
  }, [getAccessToken])

  /* ── Trace load when run selected ── */
  useEffect(() => {
    if (!selectedRunId) { setTraceData(null); return }
    const token = getAccessToken()
    getRunTrace(selectedRunId, token)
      .then(setTraceData)
      .catch(() => setTraceData(null))
  }, [selectedRunId, getAccessToken])

  /* ── IntersectionObserver to track active section ── */
  useEffect(() => {
    const observers = []
    Object.entries(sectionRefs).forEach(([id, ref]) => {
      if (!ref.current) return
      const obs = new IntersectionObserver(
        ([entry]) => { if (entry.isIntersecting) setActiveSection(id) },
        { threshold: 0.3 }
      )
      obs.observe(ref.current)
      observers.push(obs)
    })
    return () => observers.forEach(o => o.disconnect())
  }, [loading])

  const scrollTo = (id) => {
    sectionRefs[id]?.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  /* ── Derived chart data ── */
  const latencyChartData = agents.map(a => ({
    name: a.agent_name,
    latency: Math.round(a.avg_latency_ms),
    calls: a.total_calls,
  }))

  const rawTrendData = runs.slice(0, 15).reverse().map((r, i) => ({
    idx: i + 1,
    success: r.success_rate ?? 0,
    time: new Date(r.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  }))
  
  // Recharts doesn't render lines/areas with a single data point. Pad it to a flat line.
  const successTrendData = rawTrendData.length === 1 
    ? [{...rawTrendData[0], time: 'Start'}, rawTrendData[0]] 
    : rawTrendData

  const radarData = agents.map(a => ({
    agent: a.agent_name,
    reliability: Math.max(0, 1 - a.failure_rate) * 100,
    throughput: Math.min(100, (a.total_calls / Math.max(1, Math.max(...agents.map(x => x.total_calls)))) * 100),
  }))

  if (loading && !overview) {
    return (
      <div className="flex items-center justify-center h-screen bg-slate-50">
        <div className="flex flex-col items-center gap-4">
          <RefreshCw className="animate-spin text-violet-500" size={32} />
          <p className="text-slate-500 text-sm font-medium">Loading observability data…</p>
        </div>
      </div>
    )
  }

  return (
    <div className="bg-slate-50 min-h-screen text-slate-800 font-sans">

      {/* ── Sticky Header ── */}
      <header className="sticky top-0 z-30 bg-white/90 backdrop-blur-md border-b border-slate-100 shadow-sm">
        <div className="max-w-7xl mx-auto px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Link
              to="/"
              className="inline-flex items-center gap-1.5 text-sm font-semibold text-violet-600 hover:text-violet-800 transition-colors"
            >
              <ArrowLeft size={15} />
              <span>Back</span>
            </Link>
            <div className="w-px h-5 bg-slate-200" />
            <div>
              <h1 className="text-base font-extrabold text-slate-900 tracking-tight">Agent Observability</h1>
              <p className="text-xs text-slate-400 font-medium">Evaluation metrics, error distributions & step-by-step traces</p>
            </div>
          </div>

          {/* Section nav pills */}
          <div className="hidden md:flex items-center gap-4">
            <nav className="flex items-center gap-1">
              {NAV_SECTIONS.map(({ id, label, icon: Icon }) => (
                <button
                  key={id}
                  onClick={() => scrollTo(id)}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                  activeSection === id
                    ? 'bg-violet-100 text-violet-700'
                    : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700'
                }`}
              >
                <Icon size={12} />
                {label}
              </button>
            ))}
            </nav>
            <ThemeToggle />
          </div>
        </div>
      </header>

      {/* ── Scrollable Content ── */}
      <main className="max-w-7xl mx-auto px-6 py-8 space-y-16">

        {/* ══════════ SECTION 1: System Overview ══════════ */}
        <section ref={sectionRefs.overview} id="overview" className="scroll-mt-20">
          <SectionTitle icon={Activity} title="System Overview" subtitle="High-level KPIs and success trends across all agent runs" />

          {/* KPI row */}
          {overview && (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
              <KpiCard icon={Activity}     label="Total Runs"    value={overview.total_runs} />
              <KpiCard icon={CheckCircle2} label="Success Rate"  value={`${overview.success_rate}%`}
                color="text-emerald-500" bg="bg-emerald-50" border="border-emerald-100" />
              <KpiCard icon={Clock}        label="Avg Latency"   value={`${(overview.avg_latency_ms / 1000).toFixed(1)}s`}
                color="text-amber-500"   bg="bg-amber-50"   border="border-amber-100" />
              <KpiCard icon={RefreshCw}    label="Avg Retries"   value={overview.avg_retries}
                color="text-rose-500"    bg="bg-rose-50"    border="border-rose-100" />
            </div>
          )}

          {/* Success trend line chart */}
          <ChartCard title="Run Success Trend" subtitle="Success rate per run (most recent 15)">
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={successTrendData}>
                <defs>
                  <linearGradient id="successGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="#7c3aed" stopOpacity={0.15}/>
                    <stop offset="95%" stopColor="#7c3aed" stopOpacity={0}/>
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e2e8f0" />
                <XAxis dataKey="time" tick={{ fontSize: 10, fill: '#94a3b8' }} />
                <YAxis tickFormatter={v => `${(v * 100).toFixed(0)}%`} domain={[0, 1]} tick={{ fontSize: 10, fill: '#94a3b8' }} />
                <Tooltip formatter={v => [`${(v * 100).toFixed(1)}%`, 'Success Rate']}
                  contentStyle={{ borderRadius: '10px', border: 'none', boxShadow: '0 4px 16px rgba(0,0,0,0.08)', fontSize: 12 }} />
                <Area type="monotone" dataKey="success" stroke="#7c3aed" strokeWidth={2.5}
                  fill="url(#successGrad)" dot={{ r: 4, fill: '#7c3aed', strokeWidth: 0 }} activeDot={{ r: 6 }} />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>

          {/* Runs per time bar chart */}
          {runs.length > 0 && (
            <ChartCard title="Run Volume Distribution" subtitle="Total calls by difficulty level" className="mt-6">
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={[
                  { label: 'Easy', count: runs.filter(r => r.difficulty !== 'hard').length },
                  { label: 'Hard', count: runs.filter(r => r.difficulty === 'hard').length },
                ]} barSize={48}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e2e8f0" />
                  <XAxis dataKey="label" tick={{ fontSize: 12, fill: '#64748b' }} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 10, fill: '#94a3b8' }} />
                  <Tooltip contentStyle={{ borderRadius: '10px', border: 'none', boxShadow: '0 4px 16px rgba(0,0,0,0.08)', fontSize: 12 }} />
                  <Bar dataKey="count" radius={[6, 6, 0, 0]}>
                    <Cell fill="#7c3aed" />
                    <Cell fill="#ec4899" />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </ChartCard>
          )}
        </section>

        <Divider />

        {/* ══════════ SECTION 2: Agent Performance ══════════ */}
        <section ref={sectionRefs.performance} id="performance" className="scroll-mt-20">
          <SectionTitle icon={Cpu} title="Agent Performance" subtitle="Per-agent latency, reliability, and call volume metrics" />

          {/* Per-agent metrics table */}
          <ChartCard title="Per-Agent Metrics Table">
            <AgentTable agents={agents} />
          </ChartCard>

          {/* Latency bar chart */}
          <ChartCard title="Average Latency per Agent" subtitle="In milliseconds — lower is better" className="mt-6">
            {latencyChartData.length > 0 ? (
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={latencyChartData} barSize={32}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e2e8f0" />
                  <XAxis dataKey="name" tick={{ fontSize: 11, fill: '#64748b' }} />
                  <YAxis tickFormatter={v => `${(v / 1000).toFixed(1)}s`} tick={{ fontSize: 10, fill: '#94a3b8' }} />
                  <Tooltip formatter={v => [`${v} ms`, 'Avg Latency']}
                    contentStyle={{ borderRadius: '10px', border: 'none', boxShadow: '0 4px 16px rgba(0,0,0,0.08)', fontSize: 12 }} />
                  <Bar dataKey="latency" radius={[6, 6, 0, 0]}>
                    {latencyChartData.map((_, i) => (
                      <Cell key={i} fill={BRAND_COLORS[i % BRAND_COLORS.length]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-[280px] flex items-center justify-center text-slate-400 text-sm border border-dashed border-slate-200 rounded-xl">
                No latency data available.
              </div>
            )}
          </ChartCard>

          {/* Radar chart */}
          <ChartCard title="Agent Reliability vs Throughput" subtitle="Radar view of normalised reliability × call volume" className="mt-6">
            {radarData.length > 0 ? (
              <ResponsiveContainer width="100%" height={300}>
                <RadarChart data={radarData} margin={{ top: 20, right: 30, left: 30, bottom: 20 }}>
                  <PolarGrid gridType="polygon" stroke="#e2e8f0" />
                  <PolarAngleAxis dataKey="agent" tick={{ fontSize: 11, fill: '#64748b' }} />
                  <PolarRadiusAxis angle={30} domain={[0, 100]} tick={{ fontSize: 9, fill: '#94a3b8' }} />
                  <Radar name="Reliability" dataKey="reliability" stroke="#7c3aed" fill="#7c3aed" fillOpacity={0.2} strokeWidth={2} />
                  <Radar name="Throughput"  dataKey="throughput"  stroke="#ec4899" fill="#ec4899" fillOpacity={0.15} strokeWidth={2} />
                  <Legend iconType="circle" wrapperStyle={{ fontSize: 11 }} />
                  <Tooltip contentStyle={{ borderRadius: '10px', border: 'none', boxShadow: '0 4px 16px rgba(0,0,0,0.08)', fontSize: 12 }} />
                </RadarChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-[300px] flex items-center justify-center text-slate-400 text-sm border border-dashed border-slate-200 rounded-xl">
                No reliability data available.
              </div>
            )}
          </ChartCard>
        </section>

        <Divider />

        {/* ══════════ SECTION 3: Debug Loop ══════════ */}
        <section ref={sectionRefs.debug} id="debug" className="scroll-mt-20">
          <SectionTitle icon={Bug} title="Debug Loop" subtitle="Error distributions, retry patterns, and debug depth analysis" />

          {debugStats ? (
            <>
              {/* Debug stats KPIs */}
              <div className="grid grid-cols-2 gap-4 mb-6">
                <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
                  <p className="text-3xl font-extrabold text-slate-800">{debugStats.avg_debug_depth}</p>
                  <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider mt-1">Avg Debug Depth</p>
                </div>
                <div className="bg-white rounded-2xl border border-slate-100 shadow-sm p-5">
                  <p className="text-3xl font-extrabold text-slate-800">
                    {(debugStats.retry_success_ratio * 100).toFixed(1)}%
                  </p>
                  <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider mt-1">Retry Recovery Rate</p>
                </div>
              </div>

              {/* Debug chart (pie + stats) */}
              <DebugChart debugStats={debugStats} />

              {/* Error type bar chart (if data exists) */}
              {(debugStats.error_type_distribution || []).length > 0 && (
                <ChartCard title="Error Type Distribution" subtitle="Count per error category" className="mt-6">
                  <ResponsiveContainer width="100%" height={260}>
                    <BarChart data={debugStats.error_type_distribution} layout="vertical" barSize={22}>
                      <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#e2e8f0" />
                      <XAxis type="number" tick={{ fontSize: 10, fill: '#94a3b8' }} />
                      <YAxis dataKey="error_type" type="category" tick={{ fontSize: 11, fill: '#64748b' }} width={110} />
                      <Tooltip contentStyle={{ borderRadius: '10px', border: 'none', boxShadow: '0 4px 16px rgba(0,0,0,0.08)', fontSize: 12 }} />
                      <Bar dataKey="count" radius={[0, 6, 6, 0]}>
                        {(debugStats.error_type_distribution).map((_, i) => (
                          <Cell key={i} fill={BRAND_COLORS[i % BRAND_COLORS.length]} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </ChartCard>
              )}
            </>
          ) : (
            <div className="bg-white rounded-2xl border border-dashed border-slate-200 p-12 text-center text-slate-400 text-sm">
              No debug data available.
            </div>
          )}
        </section>

        <Divider />

        {/* ══════════ SECTION 4: Run Explorer ══════════ */}
        <section ref={sectionRefs.explorer} id="explorer" className="scroll-mt-20">
          <SectionTitle icon={Search} title="Run Explorer" subtitle="Browse evaluation runs and inspect step-by-step execution traces" />

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-1 min-h-[600px]">
              <RunList runs={runs} onSelectRun={setSelectedRunId} selectedRunId={selectedRunId} />
            </div>
            <div className="lg:col-span-2 min-h-[600px]">
              <TracePanel traceData={traceData} />
            </div>
          </div>
        </section>

        {/* Bottom padding */}
        <div className="h-12" />
      </main>
    </div>
  )
}

/* ── Helpers ── */

function SectionTitle({ icon: Icon, title, subtitle }) {
  return (
    <div className="mb-6 flex items-start gap-3">
      <div className="w-9 h-9 rounded-xl bg-violet-100 flex items-center justify-center shrink-0 mt-0.5">
        <Icon size={16} className="text-violet-600" />
      </div>
      <div>
        <h2 className="text-xl font-extrabold text-slate-900 tracking-tight">{title}</h2>
        {subtitle && <p className="text-sm text-slate-500 mt-0.5">{subtitle}</p>}
      </div>
    </div>
  )
}

function ChartCard({ title, subtitle, children, className = '' }) {
  return (
    <div className={`bg-white rounded-2xl border border-slate-100 shadow-sm p-6 ${className}`}>
      {(title || subtitle) && (
        <div className="mb-5">
          {title && <h3 className="text-sm font-bold text-slate-800">{title}</h3>}
          {subtitle && <p className="text-xs text-slate-400 mt-0.5">{subtitle}</p>}
        </div>
      )}
      {children}
    </div>
  )
}

function Divider() {
  return <div className="border-t border-slate-200/70" />
}
