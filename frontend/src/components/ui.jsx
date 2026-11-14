import { createContext, useCallback, useContext, useState } from 'react'
import { fx } from '../api'

// ---------------------------------------------------------------- toasts
const ToastCtx = createContext(() => {})
export const useToast = () => useContext(ToastCtx)

export function ToastProvider({ children }) {
  const [items, setItems] = useState([])
  const push = useCallback((message, kind = 'info') => {
    const id = Math.random().toString(36).slice(2)
    setItems((x) => [...x.slice(-3), { id, message, kind }])
    setTimeout(() => setItems((x) => x.filter((i) => i.id !== id)), 4500)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>
            <span className="toast-dot" />
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  )
}

// ---------------------------------------------------------------- small bits
export const Spinner = ({ label }) => (
  <div className="spinner-wrap"><div className="spinner" />{label && <span>{label}</span>}</div>
)

export const Empty = ({ icon = '✦', title, children }) => (
  <div className="empty"><div className="empty-icon">{icon}</div><strong>{title}</strong><p>{children}</p></div>
)

export const ErrorBox = ({ error, onRetry }) => (
  <div className="errorbox">
    <strong>Something went wrong</strong>
    <p>{error?.message || String(error)}</p>
    {onRetry && <button className="btn sm" onClick={onRetry}>Retry</button>}
  </div>
)

export const Chip = ({ children, tone = 'neutral', title }) => (
  <span className={`chip ${tone}`} title={title}>{children}</span>
)

const STATUS_TONE = { NOTIFIED: 'info', ENGAGED: 'good', IGNORED: 'warn', REJECTED: 'muted' }
export const StatusChip = ({ status }) => <Chip tone={STATUS_TONE[status] || 'neutral'}>{status}</Chip>

export function Bar({ value, tone = 'accent', label }) {
  const v = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value))
  return (
    <div className="bar" title={label}>
      <div className={`bar-fill ${tone}`} style={{ width: `${v * 100}%` }} />
      <span className="bar-num">{value === null || value === undefined ? '—' : fx(value)}</span>
    </div>
  )
}

export function ModeBadge({ llm }) {
  if (!llm) return <span className="mode demo">…</span>
  const live = llm.mode === 'live'
  return (
    <span className={`mode ${live ? 'live' : 'demo'}`}
      title={live ? `Gemini (${llm.model})` : llm.degraded ? `Gemini unavailable (${llm.last_error}) — using offline TF-IDF + rules; retrying in ${llm.cooldown_remaining}s`
        : 'No GEMINI_API_KEY: deterministic TF-IDF + rule-based substitute'}>
      <span className="mode-dot" />{live ? 'Live LLM' : 'Demo Mode'}{llm.degraded ? ' (fallback)' : ''}
    </span>
  )
}

// ---------------------------------------------------------------- funnel
export function Funnel({ funnel, compact }) {
  if (!funnel) return <p className="muted">This post was not routed (system author).</p>
  const steps = [
    { n: funnel.total_agents, label: 'agents', sub: 'on the platform' },
    { n: funnel.rule_passed, label: 'pass rule filter', sub: `${funnel.rule_filtered} filtered + author excluded` },
    { n: funnel.semantic_candidates, label: 'semantic candidates', sub: `${funnel.semantic_dropped} below cutoff / top-K` },
    { n: funnel.llm_evaluated, label: 'judged by LLM', sub: `${funnel.judge_calls} judge calls` },
    { n: funnel.notified, label: 'notified', sub: `${funnel.engagement_calls} drafting calls` },
  ]
  if (compact) {
    return <div className="funnel-line">{steps.map((s, i) => (
      <span key={i}><b>{s.n}</b> {s.label}{i < steps.length - 1 ? ' → ' : ''}</span>))}</div>
  }
  const max = Math.max(1, funnel.total_agents)
  return (
    <div className="funnel">
      {steps.map((s, i) => (
        <div className="funnel-row" key={i}>
          <div className="funnel-bar" style={{ width: `${Math.max(8, (s.n / max) * 100)}%`, opacity: 1 - i * 0.12 }}>
            <b>{s.n}</b>
          </div>
          <div className="funnel-label"><strong>{s.label}</strong><span>{s.sub}</span></div>
        </div>
      ))}
      <div className="funnel-saved">
        <div><b>{funnel.llm_calls_pipeline}</b><span>LLM calls (pipeline)</span></div>
        <div><b>{funnel.llm_calls_broadcast}</b><span>LLM calls (broadcast)</span></div>
        <div className={funnel.llm_calls_saved >= 0 ? 'good' : 'warn'}>
          <b>{funnel.llm_calls_saved > 0 ? `−${funnel.llm_calls_saved}` : funnel.llm_calls_saved === 0 ? '0' : `+${-funnel.llm_calls_saved}`}</b>
          <span>vs broadcast</span></div>
        <div className="good"><b>{funnel.drafts_avoided}</b><span>drafts avoided</span></div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- why / why not
export function WhyPanel({ agents, threshold }) {
  if (!agents?.length) return <Empty icon="?" title="No routing data">Publish a post to see why each agent was or was not notified.</Empty>
  return (
    <div className="why">
      {agents.map((a) => (
        <div className={`why-row ${a.status === 'REJECTED' ? 'rejected' : 'accepted'}`} key={a.agent_id}>
          <div className="why-head">
            <strong>{a.agent}</strong>
            <StatusChip status={a.status} />
            <span className="why-final" title={`final score; threshold ${fx(threshold)}`}>{fx(a.final_score)}</span>
          </div>
          <div className="why-scores">
            <div><span>Rule filter</span><b className={a.rule_pass ? 'ok' : 'no'}>{a.rule_pass ? 'pass' : 'blocked'}</b></div>
            <div><span>Semantic</span><Bar value={a.semantic_score} tone="info" /></div>
            <div><span>LLM judge</span><Bar value={a.llm_score} tone="accent" /></div>
            <div><span>Feedback ×</span><b className={a.feedback_multiplier < 1 ? 'no' : ''}>{fx(a.feedback_multiplier)}</b></div>
          </div>
          <p className="why-reason">{a.reason}</p>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- SVG line chart
export function LineChart({ series, marker, height = 240, xLabel, yLabel }) {
  const W = 640, H = height, m = { l: 40, r: 14, t: 14, b: 34 }
  const iw = W - m.l - m.r, ih = H - m.t - m.b
  const x = (v) => m.l + v * iw
  const y = (v) => m.t + (1 - v) * ih
  const ticks = [0, 0.25, 0.5, 0.75, 1]
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label={`${yLabel} by ${xLabel}`}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={m.l} x2={W - m.r} y1={y(t)} y2={y(t)} className="grid" />
          <text x={m.l - 6} y={y(t) + 4} textAnchor="end" className="tick">{t.toFixed(2)}</text>
          <text x={x(t)} y={H - m.b + 16} textAnchor="middle" className="tick">{t.toFixed(2)}</text>
        </g>
      ))}
      <text x={W / 2} y={H - 4} textAnchor="middle" className="axis">{xLabel}</text>
      {marker !== undefined && (
        <g>
          <line x1={x(marker)} x2={x(marker)} y1={m.t} y2={m.t + ih} className="marker" />
          <text x={x(marker) + 5} y={m.t + 11} className="axis">threshold {marker.toFixed(2)}</text>
        </g>
      )}
      {series.map((s) => (
        <g key={s.name}>
          <polyline fill="none" strokeWidth="2.5" stroke={s.color} strokeLinejoin="round"
            points={s.points.map((p) => `${x(p[0])},${y(p[1])}`).join(' ')} style={{ transition: 'all .3s' }} />
          {s.points.map((p, i) => <circle key={i} cx={x(p[0])} cy={y(p[1])} r="2.5" fill={s.color} />)}
        </g>
      ))}
    </svg>
  )
}
