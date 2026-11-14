import { useCallback, useEffect, useState } from 'react'
import { api, fx, pct, useBus } from '../api'
import { Bar, Chip, Empty, ErrorBox, Spinner, useToast } from '../components/ui'

export default function Feedback() {
  const toast = useToast()
  const [stats, setStats] = useState(null)
  const [error, setError] = useState(null)
  const [sim, setSim] = useState({ agent_id: '', topic: 'food_delivery', ignores: 5 })
  const [topics, setTopics] = useState([])

  const load = useCallback(() => api('/api/stats').then((s) => {
    setStats(s); setError(null)
    setSim((x) => ({ ...x, agent_id: x.agent_id || String(s.agents[0]?.id || '') }))
  }).catch(setError), [])
  useEffect(() => { load(); api('/api/topics').then((d) => setTopics(d.topics)).catch(() => {}) }, [load])
  useBus((t) => { if (['comment_created', 'ignored', 'comment_pending'].includes(t)) load() })

  const simulate = async () => {
    try {
      const r = await api('/api/feedback/simulate', { method: 'POST', admin: true, body: { ...sim, agent_id: Number(sim.agent_id), ignores: Number(sim.ignores) } })
      toast(`${r.agent} / ${sim.topic}: score multiplier ${fx(r.multiplier_before)} → ${fx(r.multiplier_after)}`, 'good'); load()
    } catch (e) { toast(e.message, 'bad') }
  }

  if (error && !stats) return <ErrorBox error={error} onRetry={load} />
  if (!stats) return <Spinner label="Loading analytics…" />
  const fb = stats.feedback
  const pol = stats.feedback_policy

  return (
    <div>
      <div className="card">
        <h3>How the feedback loop works</h3>
        <p className="muted">When a notified agent declines to engage, its <b>ignore streak</b> for that topic grows; every ignore lowers that topic's relevance multiplier by {pol.step_pct_per_ignore}% up to a hard floor of −{pol.max_penalty_pct}%. Engaging relaxes the streak by {pol.engagement_relaxes_streak_by}. The penalty applies to a post's <i>specific</i> topics (generic ones like offers/festive only when nothing specific matched). The floor means a genuinely relevant match is never eliminated — only de-prioritised and eventually pushed under the threshold if it was borderline.</p>
        <div className="row wrap">
          <select value={sim.agent_id} onChange={(e) => setSim({ ...sim, agent_id: e.target.value })}>
            {stats.agents.map((a) => <option key={a.id} value={a.id}>{a.company_name}</option>)}
          </select>
          <select value={sim.topic} onChange={(e) => setSim({ ...sim, topic: e.target.value })}>
            {topics.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
          </select>
          <input type="number" min="1" max="20" value={sim.ignores} onChange={(e) => setSim({ ...sim, ignores: e.target.value })} style={{ width: 80 }} />
          <button className="btn" onClick={simulate}>Simulate ignored notifications</button>
          <small className="muted">Simulated ignores go through the real feedback code and are flagged below.</small>
        </div>
      </div>

      {fb.length === 0 ? (
        <Empty icon="↻" title="No feedback recorded yet">Run the demo (real notifications and engagements are tracked automatically) or simulate ignored notifications above.</Empty>
      ) : (
        <>
          <div className="grid-2">
            <div className="card">
              <h3>Engagement rate per agent × topic</h3>
              {fb.map((f) => (
                <div className="fb-row" key={`${f.agent_id}-${f.topic}`}>
                  <span>{f.agent} · {f.topic.replace('_', ' ')}</span>
                  <Bar value={f.engagement_rate} tone={f.engagement_rate !== null && f.engagement_rate < 0.5 ? 'warn' : 'good'} />
                  <small>{f.engaged} engaged / {f.ignored} ignored{f.simulated_ignores ? ` (${f.simulated_ignores} simulated)` : ''}</small>
                </div>
              ))}
            </div>
            <div className="card">
              <h3>Score before vs after feedback</h3>
              <p className="muted small">Before = average raw (pre-feedback) score of this agent's routed posts on the topic; After = projected with today's multiplier.</p>
              {fb.filter((f) => f.multiplier < 1 || f.avg_score_before !== null).map((f) => (
                <div className="ba-row" key={`${f.agent_id}-${f.topic}`}>
                  <span>{f.agent} · {f.topic.replace('_', ' ')}</span>
                  <div className="ba-bars">
                    <Bar value={f.avg_score_before} tone="info" label="before" />
                    <Bar value={f.projected_score_after} tone={f.multiplier < 1 ? 'bad' : 'good'} label="after" />
                  </div>
                  <small className={f.multiplier < 1 ? 'neg' : ''}>{f.score_change_pct}% {f.would_notify_before !== f.would_notify_after ? `· would notify ${f.would_notify_before} → ${f.would_notify_after} posts` : ''}</small>
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <h3>Topic-level detail</h3>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Agent</th><th>Topic</th><th>Notified</th><th>Engaged</th><th>Ignored</th><th>Engagement</th><th>Ignore streak</th><th>Multiplier</th><th>Δ score</th><th>Before → after</th></tr></thead>
                <tbody>
                  {fb.map((f) => (
                    <tr key={`${f.agent_id}-${f.topic}`}>
                      <td>{f.agent}</td><td>{f.topic.replace('_', ' ')}</td><td>{f.notified}</td><td>{f.engaged}</td>
                      <td>{f.ignored}{f.simulated_ignores > 0 && <Chip tone="muted" title="includes simulated ignores">sim</Chip>}</td>
                      <td>{pct(f.engagement_rate)}</td><td>{f.ignore_streak}</td>
                      <td><b>×{fx(f.multiplier)}</b></td>
                      <td className={f.score_change_pct < 0 ? 'neg' : ''}>{f.score_change_pct}%</td>
                      <td>{f.avg_score_before !== null ? `${fx(f.avg_score_before)} → ${fx(f.projected_score_after)}` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
