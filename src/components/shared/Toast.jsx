/* eslint-disable react-refresh/only-export-components */
import React, { useEffect, useState } from 'react'
import { CheckCircle2, X, AlertCircle, Info } from 'lucide-react'

let toastIdCounter = 0
let globalAddToast = null

export function toast(message, type = 'success') {
  if (globalAddToast) globalAddToast({ message, type, id: ++toastIdCounter })
}

const ICONS = {
  success: <CheckCircle2 size={16} className="text-emerald-500 shrink-0" />,
  error:   <AlertCircle  size={16} className="text-rose-500 shrink-0" />,
  info:    <Info         size={16} className="text-brand-500 shrink-0" />,
}

const COLORS = {
  success: 'border-emerald-200 bg-emerald-50 text-emerald-900',
  error:   'border-rose-200 bg-rose-50 text-rose-900',
  info:    'border-brand-200 bg-brand-50 text-brand-900',
}

function ToastItem({ toast: t, onRemove }) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true))
    const timer = setTimeout(() => {
      setVisible(false)
      setTimeout(() => onRemove(t.id), 300)
    }, 3200)
    return () => clearTimeout(timer)
  }, [onRemove, t.id])

  return (
    <div className={`flex items-center gap-3 px-4 py-3 rounded-xl border shadow-lg text-sm font-semibold transition-all duration-300 max-w-xs
      ${COLORS[t.type] || COLORS.info}
      ${visible ? 'opacity-100 translate-x-0' : 'opacity-0 translate-x-8'}`}
    >
      {ICONS[t.type]}
      <span className="flex-1">{t.message}</span>
      <button onClick={() => onRemove(t.id)} className="text-slate-400 hover:text-slate-700 transition-colors">
        <X size={13} />
      </button>
    </div>
  )
}

export function ToastContainer() {
  const [toasts, setToasts] = useState([])

  useEffect(() => {
    globalAddToast = (t) => setToasts((prev) => [...prev.slice(-2), t])
    return () => { globalAddToast = null }
  }, [])

  const remove = (id) => setToasts((prev) => prev.filter((t) => t.id !== id))

  if (!toasts.length) return null

  return (
    <div aria-live="polite" className="fixed top-6 right-6 z-50 flex flex-col gap-2">
      {toasts.map((t) => <ToastItem key={t.id} toast={t} onRemove={remove} />)}
    </div>
  )
}
