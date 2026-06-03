import React, { useState } from 'react'
import { Copy, Check } from 'lucide-react'

export function CodeBlock({ codeByTab, collapsed }) {
  const tabs = Object.keys(codeByTab || {})
  const [activeTab, setActiveTab] = useState(tabs[0])
  const [copied, setCopied] = useState(false)

  if (!tabs.length) return null

  const currentCode = codeByTab[activeTab]

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(currentCode)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (err) {
      console.error('Failed to copy', err)
    }
  }

  return (
    <div className="rounded-xl overflow-hidden border border-slate-200 bg-slate-900 shadow-sm">
      <div className="flex items-center justify-between px-4 py-2 bg-slate-800 border-b border-slate-700">
        <div className="flex gap-2">
          {tabs.map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`text-[12px] font-mono px-3 py-1 rounded-md transition-colors ${
                activeTab === tab
                  ? 'bg-slate-700 text-slate-100'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-700/50'
              }`}
            >
              {tab}
            </button>
          ))}
        </div>
        <button
          onClick={handleCopy}
          className="text-slate-400 hover:text-slate-200 transition-colors p-1"
          title="Copy code"
        >
          {copied ? <Check size={14} className="text-emerald-400" /> : <Copy size={14} />}
        </button>
      </div>
      {!collapsed && (
        <div className="p-4 overflow-x-auto">
          <pre className="text-[13px] font-mono leading-relaxed text-slate-300">
            <code>{currentCode}</code>
          </pre>
        </div>
      )}
    </div>
  )
}
