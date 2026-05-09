/**
 * AgentSettings.jsx — DS-STAR per-run LLM configuration panel.
 *
 * Renders a compact collapsible settings drawer that lets the user
 * override max_rounds, reasoning model, coder model, and temperature
 * before starting an agent run.
 */

import React, { useState } from 'react'
import { Settings2, ChevronDown, ChevronUp, RotateCcw } from 'lucide-react'

// ── Available model options ──────────────────────────────────────────────────
const REASONING_MODELS = [
  { value: 'meta/llama-3.1-70b-instruct',  label: 'Llama 3.1 70B (default)' },
  { value: 'meta/llama-3.3-70b-instruct',  label: 'Llama 3.3 70B' },
  { value: 'mistralai/mistral-7b-instruct-v0.3', label: 'Mistral 7B' },
  { value: 'nvidia/llama-3.1-nemotron-70b-instruct', label: 'Nemotron 70B' },
  { value: 'google/gemma-4-31b-it', label: 'Gemma 4 31B IT' },
]

const CODER_MODELS = [
  { value: 'meta/codellama-70b-instruct',           label: 'CodeLlama 70B (default)' },
  { value: 'qwen/qwen2.5-coder-32b-instruct',       label: 'Qwen 2.5 Coder 32B' },
  { value: 'meta/llama-3.1-70b-instruct',           label: 'Llama 3.1 70B (general)' },
  { value: 'google/gemma-4-31b-it',                 label: 'Gemma 4 31B IT' },
]

export const DEFAULT_SETTINGS = {
  maxRounds:   10,
  model:       'meta/llama-3.1-70b-instruct',
  coderModel:  'meta/codellama-70b-instruct',
  temperature: 0.1,
}

// ── Small labelled slider ────────────────────────────────────────────────────
function Slider({ id, label, min, max, step, value, onChange, format, disabled = false }) {
  const percentage = ((value - min) / (max - min)) * 100;
  
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <label htmlFor={id} className="text-[11px] font-semibold text-slate-600 uppercase tracking-wider">
          {label}
        </label>
        <span className="text-[12px] font-mono font-bold text-brand-600 bg-brand-50 px-2 py-0.5 rounded-md">
          {format ? format(value) : value}
        </span>
      </div>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        disabled={disabled}
        className="w-full h-1.5 bg-slate-200 rounded-full appearance-none cursor-pointer
                   accent-brand-500 hover:accent-brand-600 disabled:opacity-50 disabled:cursor-not-allowed"
        style={{
          background: `linear-gradient(to right, #3b82f6 0%, #3b82f6 ${percentage}%, #e2e8f0 ${percentage}%, #e2e8f0 100%)`
        }}
      />
      <div className="flex justify-between text-[10px] text-slate-400 font-medium">
        <span>{format ? format(min) : min}</span>
        <span>{format ? format(max) : max}</span>
      </div>
    </div>
  )
}

// ── Select dropdown ──────────────────────────────────────────────────────────
function Select({ id, label, value, onChange, options, disabled = false }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="text-[11px] font-semibold text-slate-600 uppercase tracking-wider">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className="w-full text-[12px] font-medium text-slate-700 bg-white border border-slate-200
                   rounded-lg px-3 py-2 cursor-pointer focus:outline-none focus:ring-2
                   focus:ring-brand-300 focus:border-brand-400 transition-all
                   disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>{opt.label}</option>
        ))}
      </select>
    </div>
  )
}

// ── Main component ───────────────────────────────────────────────────────────
export function AgentSettings({ settings, onChange, disabled = false, placement = 'inline' }) {
  const [open, setOpen] = useState(false)
  const [draftSettings, setDraftSettings] = useState(settings)

  React.useEffect(() => {
    if (open) {
      setDraftSettings(settings)
    }
  }, [open, settings])

  React.useEffect(() => {
    if (!open) return
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        setOpen(false)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open])

  const updateField = (key, val) => setDraftSettings({ ...draftSettings, [key]: val })

  const isSavedModified = (
    settings.maxRounds   !== DEFAULT_SETTINGS.maxRounds   ||
    settings.model       !== DEFAULT_SETTINGS.model       ||
    settings.coderModel  !== DEFAULT_SETTINGS.coderModel  ||
    settings.temperature !== DEFAULT_SETTINGS.temperature
  )

  const isDraftModified = (
    draftSettings.maxRounds   !== DEFAULT_SETTINGS.maxRounds   ||
    draftSettings.model       !== DEFAULT_SETTINGS.model       ||
    draftSettings.coderModel  !== DEFAULT_SETTINGS.coderModel  ||
    draftSettings.temperature !== DEFAULT_SETTINGS.temperature
  )

  const handleResetSaved = (e) => {
    e.stopPropagation()
    onChange({ ...DEFAULT_SETTINGS })
  }

  const handleResetDraft = (e) => {
    e.stopPropagation()
    setDraftSettings({ ...DEFAULT_SETTINGS })
  }

  const handleApply = () => {
    onChange(draftSettings)
    setOpen(false)
  }

  const handleCancel = () => {
    setOpen(false)
  }

  const isAbsolute = placement === 'header';

  return (
    <div className={`relative transition-all duration-200 ${
      !isAbsolute
        ? `rounded-xl border ${open ? 'border-brand-200 bg-brand-50/40' : 'border-slate-200 bg-white hover:border-brand-200'}`
        : ''
    }`}>
      {/* Header Button */}
      {isAbsolute ? (
        <button
          onClick={() => setOpen(!open)}
          disabled={disabled}
          className={`p-2 rounded-xl border flex items-center justify-center transition-colors shadow-sm
            ${open ? 'bg-brand-50 border-brand-200 text-brand-600' : 'bg-white border-slate-200 text-slate-500 hover:bg-slate-50 hover:text-slate-700'}
            disabled:opacity-50 disabled:cursor-not-allowed`}
          title="Agent Settings"
        >
          <Settings2 size={16} />
          {isSavedModified && (
            <div className="absolute -top-1 -right-1 w-2.5 h-2.5 bg-brand-500 rounded-full border-2 border-white" />
          )}
        </button>
      ) : (
        <button
          id="agent-settings-toggle"
          onClick={() => setOpen((o) => !o)}
          disabled={disabled}
          className="w-full flex items-center gap-2.5 px-4 py-2.5 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Settings2 size={13} className={open ? 'text-brand-500' : 'text-slate-400'} />
          <span className={`text-[12px] font-semibold flex-1 text-left ${
            open ? 'text-brand-700' : 'text-slate-600'
          }`}>
            Agent Settings
          </span>

          {isSavedModified && (
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-full
                             bg-brand-100 text-brand-600 uppercase tracking-wider">
              Modified
            </span>
          )}

          {isSavedModified && !disabled && (
            <button
              onClick={handleResetSaved}
              title="Reset to defaults"
              className="p-1 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors"
            >
              <RotateCcw size={11} />
            </button>
          )}

          {open
            ? <ChevronUp size={13} className="text-slate-400 shrink-0" />
            : <ChevronDown size={13} className="text-slate-400 shrink-0" />}
        </button>
      )}

      {/* Body */}
      {open && (
        <div className={`
          ${isAbsolute
            ? 'absolute top-full right-0 mt-2 w-[480px] bg-white/95 backdrop-blur-md shadow-xl border border-slate-200/80 rounded-2xl px-6 pb-6 pt-5 space-y-6 z-50'
            : 'px-4 pb-4 pt-1 border-t border-brand-100 space-y-5'}
          animate-fade-in
        `}>
          {isAbsolute && (
            <div className="flex items-center justify-between pb-2 border-b border-slate-100">
              <span className="text-[13px] font-bold text-slate-800">Agent Settings</span>
              {isDraftModified && !disabled && (
                <button
                  onClick={handleResetDraft}
                  className="flex items-center gap-1.5 text-[11px] font-semibold text-slate-400 hover:text-brand-600 transition-colors"
                >
                  <RotateCcw size={12} />
                  Reset Defaults
                </button>
              )}
            </div>
          )}

          {/* Max Rounds */}
          <Slider
            id="setting-max-rounds"
            label="Max Rounds"
            min={1}
            max={10}
            step={1}
            value={draftSettings.maxRounds}
            onChange={(v) => updateField('maxRounds', v)}
            format={(v) => `${v} round${v !== 1 ? 's' : ''}`}
            disabled={disabled}
          />

          {/* Temperature */}
          <Slider
            id="setting-temperature"
            label="Temperature"
            min={0.0}
            max={1.0}
            step={0.05}
            value={draftSettings.temperature}
            onChange={(v) => updateField('temperature', v)}
            format={(v) => v.toFixed(2)}
            disabled={disabled}
          />

          {/* Reasoning Model */}
          <Select
            id="setting-reasoning-model"
            label="Reasoning Model (Planner / Verifier / Router)"
            value={draftSettings.model}
            onChange={(v) => updateField('model', v)}
            options={REASONING_MODELS}
            disabled={disabled}
          />

          {/* Coder Model */}
          <Select
            id="setting-coder-model"
            label="Coder Model"
            value={draftSettings.coderModel}
            onChange={(v) => updateField('coderModel', v)}
            options={CODER_MODELS}
            disabled={disabled}
          />

          <p className="text-[10px] text-slate-400 leading-relaxed pt-1">
            Settings apply to the next run only. Lower temperature = more deterministic output.
            Higher max rounds gives the agent more time to self-correct.
          </p>

          <div className="flex items-center justify-end gap-2 pt-4 border-t border-slate-100">
            <button
              onClick={handleCancel}
              className="px-4 py-1.5 rounded-lg text-[12px] font-semibold text-slate-600 hover:bg-slate-100 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleApply}
              disabled={disabled}
              className="px-4 py-1.5 rounded-lg text-[12px] font-semibold bg-brand-500 text-white hover:bg-brand-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
            >
              Apply
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
