import React, { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { X, FileSpreadsheet } from 'lucide-react'
import { ColumnViewer } from './ColumnViewer'

export function FileDetailsDialog({ isOpen, onClose, file }) {
  const closeRef = useRef(null)

  // Focus trap / escape key handling
  useEffect(() => {
    if (!isOpen) return

    const handleKeyDown = (e) => {
      if (e.key === 'Escape') onClose()
    }
    
    window.addEventListener('keydown', handleKeyDown)
    // Small timeout to allow render before focusing
    setTimeout(() => {
      closeRef.current?.focus()
    }, 10)
    
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  if (!isOpen || !file) return null

  let columns = file.metadata?.columns || []

  return createPortal(
    <div 
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby="dialog-title"
    >
      {/* Backdrop */}
      <div 
        className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm transition-opacity" 
        onClick={onClose}
        aria-hidden="true"
      />
      
      {/* Dialog Content */}
      <div className="relative w-full max-w-lg bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden flex flex-col max-h-[85vh] animate-in fade-in zoom-in-95 duration-200">
        <div className="flex items-center justify-between p-4 border-b border-slate-100 bg-slate-50/50">
          <div className="flex items-center gap-3 overflow-hidden">
            <div className="w-10 h-10 rounded-xl bg-brand-50 border border-brand-100 flex items-center justify-center shrink-0">
              <FileSpreadsheet size={18} className="text-brand-600" />
            </div>
            <div className="min-w-0">
              <h2 id="dialog-title" className="text-base font-bold text-slate-800 truncate">
                {file.name}
              </h2>
              <p className="text-xs text-slate-500 font-medium mt-0.5">
                {columns.length} Column{columns.length !== 1 ? 's' : ''}
              </p>
            </div>
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors focus:outline-none focus:ring-2 focus:ring-brand-500/40"
            aria-label="Close file details"
          >
            <X size={18} />
          </button>
        </div>
        
        <div className="flex-1 min-h-0 flex flex-col">
          <ColumnViewer columns={columns} />
        </div>
      </div>
    </div>,
    document.body
  )
}
