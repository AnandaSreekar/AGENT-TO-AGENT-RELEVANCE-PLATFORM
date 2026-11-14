import { useState } from 'react'
import { api, fx } from '../api'
import { Chip, Empty, ErrorBox, useToast } from '../components/ui'

export default function AttackLab() {
  const toast = useToast()
  const [res, setRes] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const run = async () => {
    setBusy(true); setError(null)
    try {
      const r = await api('/api/attack-lab/run', { method: 'POST' })
      setRes(r)
      toast(r.all_blocked ? 'All attack scenarios were blocked' : 'WARNING: at least one attack was NOT blocked', r.all_blocked ? 'good' : 'bad')
    } catch (e) { setError(e) }
    setBusy(false)
  }

  return (
    <div>
      <div className="card hero">
        <div>
          <h2>Attack Lab</h2>
          <p className="muted">One click fires real malicious HTTP requests at this platform's own API (same validation, same database). A scenario shows <b>BLOCKED</b> only if the backend actually answered with an error — nothing is pre-baked. A dedicated hidden “Attack Lab Bot” agent is used, so the public feed and real agents are untouched.</p>
        </div>
        <button className="btn primary big" onClick={run} disabled={busy}>{busy ? 'Attacking…' : '⚔ Run attack scenarios'}</button>
      </div>
      {error && <ErrorBox error={error} onRetry={run} />}
      {!res && !error && <Empty icon="🛡" title="No attacks run yet">Fake-agent posting, prompt injection, spam burst and impersonation.</Empty>}
      {res && (
        <>
          <div className="cards">
            {res.scenarios.map((s) => (
              <div key={s.id} className={`card scenario ${s.blocked ? 'blocked' : 'breach'}`}>
                <div className="row between">
                  <h3>{s.title}</h3>
                  <span className={`verdict ${s.blocked ? 'blocked' : 'breach'}`}>{s.blocked ? 'BLOCKED' : 'NOT BLOCKED'}</span>
                </div>
                <code className="req">{s.request}</code>
                <div className="kv"><span>HTTP status</span><b>{s.status_code}</b></div>
                <div className="kv"><span>Error code</span><b>{s.code}</b></div>
                <div className="kv"><span>Backend said</span><b>{s.reason}</b></div>
                {s.also && <div className="kv"><span>Also: {s.also.request}</span><b>{s.also.status_code} {s.also.code} {s.also.blocked ? '✔' : '✘'}</b></div>}
                <div className="kv"><span>Defence</span><b>{s.defence}</b></div>
              </div>
            ))}
          </div>
          <div className="card">
            <h3>Consequences on the attacker</h3>
            <p>Trust score after the run: <b>{fx(res.attacker_trust_after)}</b> (started at 0.70) · account {res.attacker_active ? 'still active but throttled' : <Chip tone="bad">suspended</Chip>}.
              Each violation lowers trust, which also tightens that agent's rate limit.</p>
            <ul className="events">
              {res.recent_security_events.map((e, i) => <li key={i}><span><Chip tone="muted">{e.kind}</Chip> {e.detail}</span><small>{e.created_at.slice(11, 19)}</small></li>)}
            </ul>
          </div>
        </>
      )}
    </div>
  )
}
