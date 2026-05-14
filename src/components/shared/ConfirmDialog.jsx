import React, { useEffect } from 'react'
import { AlertTriangle } from 'lucide-react'

export function ConfirmDialog({ isOpen, duplicates, onConfirm, onCancel }) {
  useEffect(() => {
    if (!isOpen) return
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') onCancel()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onCancel])

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/20 backdrop-blur-sm p-4 animate-fade-in">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-md overflow-hidden flex flex-col animate-bounce-in border border-slate-100">
        <div className="p-6">
          <div className="flex items-start gap-4">
            <div className="w-10 h-10 rounded-xl bg-orange-50 flex items-center justify-center shrink-0 border border-orange-100">
              <AlertTriangle className="text-orange-500" size={20} />
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-lg font-bold text-slate-800">Files Already Exist</h3>
              <p className="text-sm text-slate-500 mt-1 leading-relaxed">
                The following files have already been uploaded. Do you want to replace them?
              </p>
              <div className="mt-4 bg-slate-50 rounded-xl p-3 max-h-36 overflow-y-auto border border-slate-100 space-y-1.5">
                {duplicates.map((f, i) => (
                  <div key={i} className="text-sm font-medium text-slate-700 flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-orange-300 shrink-0" />
                    <span className="truncate">{f.name}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
        <div className="px-6 py-4 bg-slate-50 border-t border-slate-100 flex items-center justify-end gap-3">
          <button onClick={onCancel} className="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-200 hover:text-slate-800 rounded-xl transition-colors">
            Cancel
          </button>
          <button onClick={onConfirm} className="px-4 py-2 text-sm font-medium text-white bg-orange-500 hover:bg-orange-600 shadow-sm shadow-orange-500/20 rounded-xl transition-colors">
            Replace Files
          </button>
        </div>
      </div>
    </div>
  )
}
