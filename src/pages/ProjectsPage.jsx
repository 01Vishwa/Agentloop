import React, { useEffect, useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Plus, Briefcase, Loader2, ArrowRight,
  FolderKanban, LogOut, User, BarChart2, Clock,
  Search, ArrowUpDown, ChevronUp, ChevronDown
} from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { useWorkspaceStore } from '../stores/workspaceStore'
import { supabase } from '../lib/supabaseClient'

export function ProjectsPage() {
  const { user, signOut, isAuthenticated, loading: authLoading, getAccessToken } = useAuth()
  const { workspaces, loading: wsLoading, fetchWorkspaces, createWorkspace, setActiveWorkspace } = useWorkspaceStore()
  const navigate = useNavigate()

  const [isCreating, setIsCreating] = useState(false)
  const [newProjectName, setNewProjectName] = useState('')
  const [createLoading, setCreateLoading] = useState(false)
  // Per-workspace run stats: { [workspace_id]: { run_count, last_run_at } }
  const [wsStats, setWsStats] = useState({})

  const [searchQuery, setSearchQuery] = useState('')
  const [sortConfig, setSortConfig] = useState({ key: 'created_at', direction: 'desc' })
  const [currentPage, setCurrentPage] = useState(1)
  const itemsPerPage = 10

  // Filtering and Sorting logic
  const filteredAndSortedWorkspaces = useMemo(() => {
    let result = [...workspaces]

    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase()
      result = result.filter(ws => ws.name.toLowerCase().includes(query))
    }

    result.sort((a, b) => {
      let aVal, bVal
      
      if (sortConfig.key === 'name') {
        aVal = a.name.toLowerCase()
        bVal = b.name.toLowerCase()
      } else if (sortConfig.key === 'created_at') {
        aVal = new Date(a.created_at).getTime()
        bVal = new Date(b.created_at).getTime()
      } else if (sortConfig.key === 'run_count') {
        aVal = wsStats[a.id]?.run_count || 0
        bVal = wsStats[b.id]?.run_count || 0
      } else if (sortConfig.key === 'last_run_at') {
        aVal = wsStats[a.id]?.last_run_at ? new Date(wsStats[a.id].last_run_at).getTime() : 0
        bVal = wsStats[b.id]?.last_run_at ? new Date(wsStats[b.id].last_run_at).getTime() : 0
      }

      if (aVal < bVal) return sortConfig.direction === 'asc' ? -1 : 1
      if (aVal > bVal) return sortConfig.direction === 'asc' ? 1 : -1
      return 0
    })

    return result
  }, [workspaces, searchQuery, sortConfig, wsStats])

  const handleSort = (key) => {
    setSortConfig(prev => ({
      key,
      direction: prev.key === key && prev.direction === 'asc' ? 'desc' : 'asc'
    }))
  }

  // Reset page to 1 when search query changes
  useEffect(() => {
    setCurrentPage(1)
  }, [searchQuery])

  // Pagination logic
  const totalPages = Math.ceil(filteredAndSortedWorkspaces.length / itemsPerPage)
  const paginatedWorkspaces = useMemo(() => {
    const startIndex = (currentPage - 1) * itemsPerPage
    return filteredAndSortedWorkspaces.slice(startIndex, startIndex + itemsPerPage)
  }, [filteredAndSortedWorkspaces, currentPage, itemsPerPage])

  const SortIcon = ({ sortKey }) => {
    if (sortConfig.key !== sortKey) return <ArrowUpDown size={14} className="text-slate-300 opacity-0 group-hover:opacity-100 transition-opacity ml-1 inline-block" />
    return sortConfig.direction === 'asc' ? <ChevronUp size={14} className="text-brand-500 ml-1 inline-block" /> : <ChevronDown size={14} className="text-brand-500 ml-1 inline-block" />
  }

  // Redirect if not authenticated
  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      navigate('/', { replace: true })
    }
  }, [authLoading, isAuthenticated, navigate])

  // Fetch projects on load
  useEffect(() => {
    if (user?.id) {
      fetchWorkspaces(user.id)
    }
  }, [user?.id, fetchWorkspaces])

  // Fetch per-workspace run statistics for card display
  useEffect(() => {
    if (!user?.id || workspaces.length === 0) return
    const loadStats = async () => {
      try {
        const { data: { session } } = await supabase.auth.getSession()
        const token = session?.access_token
        const res = await fetch('/api/workspaces/stats', {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
        if (res.ok) {
          const data = await res.json()
          setWsStats(data)
        }
      } catch {
        // Silently ignore — stats are decorative
      }
    }
    loadStats()
  }, [user?.id, workspaces.length])

  const handleSelectProject = (project) => {
    setActiveWorkspace(project)
    navigate(`/project/${project.id}`)
  }

  const handleCreateProject = async (e) => {
    e.preventDefault()
    if (!newProjectName.trim() || !user?.id) return
    setCreateLoading(true)
    try {
      const newWs = await createWorkspace(user.id, newProjectName)
      setNewProjectName('')
      setIsCreating(false)
      handleSelectProject(newWs)
    } catch (err) {
      console.error('Failed to create project:', err)
    } finally {
      setCreateLoading(false)
    }
  }

  if (authLoading || (wsLoading && workspaces.length === 0)) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#f8fafc]">
        <Loader2 size={32} className="animate-spin text-brand-500" />
      </div>
    )
  }

  return (
    <div className="min-h-screen font-sans bg-[#f8fafc] text-slate-800 relative">
      {/* Navbar */}
      <header className="relative z-20 border-b border-slate-200/80 bg-white/70 backdrop-blur-md">
        <div className="max-w-[1200px] mx-auto px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-brand-500 to-violet-600
                            flex items-center justify-center shadow-md">
              <FolderKanban size={18} className="text-white" />
            </div>
            <span className="font-bold text-slate-800 text-base tracking-tight">Agentloop Projects</span>
          </div>

          <div className="flex items-center gap-2">
            <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl border border-slate-200
                            bg-white text-[12px] font-medium text-slate-700 shadow-sm">
              <User size={13} className="text-brand-500" />
              <span className="hidden sm:block max-w-[150px] truncate">{user?.email}</span>
            </div>
            <button
              onClick={async () => { await signOut(); navigate('/', { state: { signedOut: true } }) }}
              className="btn-ghost px-3 text-[12px]"
              title="Sign out"
            >
              <LogOut size={15} />
            </button>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-[1200px] mx-auto px-6 py-12 relative z-10">
        <div className="flex items-center justify-between mb-10">
          <div>
            <h1 className="text-3xl font-extrabold text-slate-900 tracking-tight">Your Projects</h1>
          </div>
          <button
            onClick={() => setIsCreating(true)}
            className="btn-primary"
            disabled={isCreating}
          >
            <Plus size={16} />
            New Project
          </button>
        </div>

        {isCreating && (
          <div className="mb-8 p-6 glass-card-elevated animate-slide-up border-brand-200 bg-brand-50/30">
            <h3 className="text-sm font-bold text-slate-800 mb-3">Create New Project</h3>
            <form onSubmit={handleCreateProject} className="flex gap-3">
              <input
                autoFocus
                type="text"
                placeholder="Project Name (e.g. Q1 Financial Analysis)"
                value={newProjectName}
                onChange={(e) => setNewProjectName(e.target.value)}
                maxLength={60}
                className="flex-1 input-field"
                disabled={createLoading}
              />
              <button type="submit" disabled={!newProjectName.trim() || createLoading} className="btn-primary shrink-0">
                {createLoading ? <Loader2 size={16} className="animate-spin" /> : 'Create'}
              </button>
              <button type="button" onClick={() => setIsCreating(false)} className="btn-secondary shrink-0" disabled={createLoading}>
                Cancel
              </button>
            </form>
          </div>
        )}

        {workspaces.length > 0 && !isCreating && (
          <div className="mb-6 flex flex-col sm:flex-row gap-4 items-center justify-between bg-white p-4 rounded-2xl shadow-sm border border-slate-100">
            <div className="relative w-full sm:max-w-md">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={18} />
              <input
                type="text"
                placeholder="Search projects..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full pl-10 pr-4 py-2 bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-all text-sm"
              />
            </div>
            <div className="text-sm text-slate-500 font-medium">
              Showing {filteredAndSortedWorkspaces.length} project{filteredAndSortedWorkspaces.length !== 1 ? 's' : ''}
            </div>
          </div>
        )}

        {workspaces.length === 0 && !wsLoading && !isCreating ? (
          <div className="text-center py-20 px-6 glass-card-elevated border-dashed border-2 border-slate-200">
            <Briefcase size={48} className="mx-auto text-slate-300 mb-4" />
            <h2 className="text-lg font-bold text-slate-700 mb-2">No projects yet</h2>
            <p className="text-slate-500 mb-6">Create your first project to start running agents and analyzing data.</p>
            <button onClick={() => setIsCreating(true)} className="btn-primary mx-auto">
              <Plus size={16} />
              Create Project
            </button>
          </div>
        ) : filteredAndSortedWorkspaces.length === 0 && workspaces.length > 0 && !isCreating ? (
           <div className="text-center py-20 px-6 glass-card-elevated border-dashed border-2 border-slate-200">
             <Search size={48} className="mx-auto text-slate-300 mb-4" />
             <h2 className="text-lg font-bold text-slate-700 mb-2">No results found</h2>
             <p className="text-slate-500 mb-6">No projects match your search query "{searchQuery}".</p>
             <button onClick={() => setSearchQuery('')} className="btn-secondary mx-auto">
               Clear Search
             </button>
           </div>
        ) : (
          !isCreating && (
            <div className="glass-card-elevated overflow-hidden border border-slate-200 bg-white shadow-sm rounded-2xl">
              <div className="overflow-x-auto overflow-y-auto max-h-[60vh]">
                <table className="w-full text-left border-collapse min-w-[700px]">
                  <thead className="sticky top-0 z-10 shadow-sm">
                    <tr className="border-b border-slate-200 bg-slate-50">
                      <th className="p-4 font-semibold text-slate-600 text-sm cursor-pointer group hover:bg-slate-100 transition-colors w-[35%]" onClick={() => handleSort('name')}>
                        <div className="flex items-center">
                          Project Name
                          <SortIcon sortKey="name" />
                        </div>
                      </th>
                      <th className="p-4 font-semibold text-slate-600 text-sm cursor-pointer group hover:bg-slate-100 transition-colors w-[20%]" onClick={() => handleSort('created_at')}>
                        <div className="flex items-center">
                          Created Date
                          <SortIcon sortKey="created_at" />
                        </div>
                      </th>
                      <th className="p-4 font-semibold text-slate-600 text-sm cursor-pointer group hover:bg-slate-100 transition-colors w-[15%]" onClick={() => handleSort('run_count')}>
                        <div className="flex items-center">
                          Runs
                          <SortIcon sortKey="run_count" />
                        </div>
                      </th>
                      <th className="p-4 font-semibold text-slate-600 text-sm cursor-pointer group hover:bg-slate-100 transition-colors w-[20%]" onClick={() => handleSort('last_run_at')}>
                        <div className="flex items-center">
                          Last Run
                          <SortIcon sortKey="last_run_at" />
                        </div>
                      </th>
                      <th className="p-4 font-semibold text-slate-600 text-sm text-right w-[10%]">
                        Action
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {paginatedWorkspaces.map((ws) => {
                      const stats = wsStats[ws.id]
                      const runCount = stats?.run_count ?? 0
                      const lastRun = stats?.last_run_at
                        ? new Date(stats.last_run_at).toLocaleDateString()
                        : null

                      return (
                        <tr 
                          key={ws.id}
                          onClick={() => handleSelectProject(ws)}
                          className="group border-b border-slate-100 hover:bg-brand-50/30 cursor-pointer transition-colors last:border-b-0"
                        >
                          <td className="p-4">
                            <div className="flex items-center gap-3">
                              <div className="w-10 h-10 rounded-xl bg-slate-100 flex items-center justify-center text-slate-500 group-hover:bg-brand-100 group-hover:text-brand-600 transition-colors shrink-0">
                                <Briefcase size={18} />
                              </div>
                              <span className="font-semibold text-slate-800 text-base truncate max-w-[250px]">{ws.name}</span>
                            </div>
                          </td>
                          <td className="p-4 text-sm text-slate-500">
                            {new Date(ws.created_at).toLocaleDateString()}
                          </td>
                          <td className="p-4">
                            <div className="flex items-center gap-1.5 text-sm font-medium text-slate-600">
                              <BarChart2 size={14} className="text-brand-400" />
                              {runCount} run{runCount !== 1 ? 's' : ''}
                            </div>
                          </td>
                          <td className="p-4">
                            {lastRun ? (
                              <div className="flex items-center gap-1.5 text-sm text-slate-500">
                                <Clock size={14} />
                                {lastRun}
                              </div>
                            ) : (
                              <span className="text-sm text-slate-300 italic font-medium">No runs yet</span>
                            )}
                          </td>
                          <td className="p-4 text-right pr-6">
                            <ArrowRight size={18} className="inline-block text-slate-300 group-hover:text-brand-500 group-hover:translate-x-1 transition-all" />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              {totalPages > 1 && (
                <div className="px-6 py-4 border-t border-slate-200 bg-slate-50 flex items-center justify-between">
                  <span className="text-sm text-slate-500">
                    Showing {(currentPage - 1) * itemsPerPage + 1} to {Math.min(currentPage * itemsPerPage, filteredAndSortedWorkspaces.length)} of {filteredAndSortedWorkspaces.length} projects
                  </span>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => setCurrentPage(prev => Math.max(1, prev - 1))}
                      disabled={currentPage === 1}
                      className="px-3 py-1.5 text-sm font-medium rounded-lg border border-slate-200 bg-white text-slate-600 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      Previous
                    </button>
                    <span className="text-sm font-medium text-slate-700 px-2">
                      Page {currentPage} of {totalPages}
                    </span>
                    <button
                      onClick={() => setCurrentPage(prev => Math.min(totalPages, prev + 1))}
                      disabled={currentPage === totalPages}
                      className="px-3 py-1.5 text-sm font-medium rounded-lg border border-slate-200 bg-white text-slate-600 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          )
        )}
      </main>
    </div>
  )
}
