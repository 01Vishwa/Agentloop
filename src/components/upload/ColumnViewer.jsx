import React, { useState, useMemo } from 'react'
import { Search, Database, ListFilter } from 'lucide-react'

export function ColumnViewer({ columns = [] }) {
  const [searchQuery, setSearchQuery] = useState('')

  const filteredColumns = useMemo(() => {
    if (!searchQuery.trim()) return columns
    const query = searchQuery.toLowerCase()
    return columns.filter(col => col.name.toLowerCase().includes(query))
  }, [columns, searchQuery])

  return (
    <div className="flex flex-col h-full bg-white flex-1 min-h-0">
      <div className="p-4 border-b border-slate-100 shrink-0">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={16} />
          <input
            type="text"
            placeholder="Search columns..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 pr-4 py-2 bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-all text-sm"
            aria-label="Search columns"
          />
        </div>
      </div>
      
      <div className="flex-1 overflow-y-auto p-4 min-h-[300px]">
        {filteredColumns.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center p-6 border-2 border-dashed border-slate-100 rounded-xl">
            <ListFilter size={32} className="text-slate-300 mb-3" />
            <h3 className="text-sm font-semibold text-slate-700 mb-1">No columns found</h3>
            <p className="text-xs text-slate-500">
              {searchQuery ? `No columns match "${searchQuery}"` : "This file has no columns."}
            </p>
          </div>
        ) : (
          <ul className="space-y-2" role="list">
            {filteredColumns.map((col, idx) => (
              <li 
                key={`${col.name}-${idx}`} 
                className="flex items-center justify-between p-3 rounded-xl border border-slate-100 hover:border-slate-200 bg-slate-50/50 hover:bg-slate-50 transition-colors"
              >
                <div className="flex items-center gap-2 overflow-hidden">
                  <Database size={14} className="text-brand-400 shrink-0" />
                  <span className="font-medium text-slate-700 text-sm truncate">{col.name}</span>
                </div>
                <span className="shrink-0 ml-3 px-2 py-1 bg-white border border-slate-200 text-slate-600 text-[11px] font-mono font-medium rounded-md uppercase tracking-wider shadow-sm">
                  {col.type}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
