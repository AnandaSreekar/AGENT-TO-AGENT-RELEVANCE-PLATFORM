import { useEffect, useRef, useState } from 'react'
import { api, fx, pct } from '../api'
import { Bar, Chip, ErrorBox, LineChart, Spinner, useToast } from '../components/ui'

const AGENTS = ['Swiggy', 'Flipkart', 'PhonePe', 'HDFC Bank', 'Bajaj Auto', 'Zomato']
const SHORT = { Swiggy: 'SW', Flipkart: 'FK', PhonePe: 'PP', 'HDFC Bank': 'HD', 'Bajaj Auto': 'BA', Zomato: 'ZO' }

export default function RelevanceLab() {
  const [thr, setThr] = useState(0.6)
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const seq = useRef(0)
  const toast = useToast()
  const [prog, setProg] = useState(null)
  const poller = useRef(null)

  const run = (t, refresh = false) => {
    const n = ++seq.current
    setBusy(true)
    api('/api/eval/run', { method: 'POST', body: { threshold: t, refresh } })
      .then((d) => { if (n === seq.current) { setRes(d); setError(null) } })
      .catch((e) => n === seq.current && setError(e))
      .finally(() => n === seq.current && setBusy(false))
  }
  useEffect(() => { const id = setTimeout(() => run(thr), 120); return () => clearTimeout(id) }, [thr])

  // live re-score: backend job scores one call at a time (paced); we poll its progress
  const poll = () => {
    clearInterval(poller.current)
    poller.current = setInterval(async () => {
      try {
        const p = await api('/api/eval/progress')
        setProg(p)
        if (p.state !== 'running') {
          clearInterval(poller.current)
          if (p.state === 'failed') toast(`Live re-score failed: ${p.error}`, 'bad')
          if (p.state === 'done') toast(p.message, p.message.includes('mixed') ? 'warn' : 'good')
          run(thr)
        }
      } catch { clearInterval(poller.current) }
    }, 1000)
  }
  useEffect(() => () => clearInterval(poller.current), [])
  useEffect(() => { if (res?.rescore?.state === 'running' && !poller.current) { setProg(res.rescore); poll() } }, [res])
  const rescore = async () => {
    try { setProg(await api('/api/eval/rescore', { method: 'POST', admin: true })); poll() }
    catch (e) { toast(e.message, 'bad') }
  }

  if (error && !res) return <ErrorBox error={error} onRetry={() => run(thr)} />
  if (!res) return <Spinner label="Scoring 15 posts × 6 agents…" />

  const [A, B, C] = res.strategies
  const series = [
    { name: 'Precision', color: 'var(--info)', points: res.sweep.map((s) => [s.threshold, s.precision]) },
    { name: 'Recall', color: 'var(--good)', points: res.sweep.map((s) => [s.threshold, s.recall]) },
    { name: 'F1', color: 'var(--accent)', points: res.sweep.map((s) => [s.threshold, s.f1]) },
  ]
  const cell = {}
  res.pairs.forEach((p) => { cell[`${p.post_idx}|${p.agent}`] = p })

  return (
    <div className="lab">
      <div className="card">
        <div className="row between wrap">
          <div>
            <h3>Threshold</h3>
            <p className="muted small">Dataset: {res.dataset.posts} posts × {res.dataset.agents} agents = {res.dataset.evaluated_pairs} evaluated pairs (author's own pair excluded), {res.dataset.positive_pairs} labelled “should notify”. Scores from: {res.mode === 'live' && <Chip tone="good">Live LLM (Gemini) — all {res.provenance.judge_calls_total} judge calls</Chip>}
              {res.mode === 'mixed' && <Chip tone="warn">Mixed — {res.provenance.judge_calls_offline}/{res.provenance.judge_calls_total} judge calls fell back to offline rules (not a pure live measurement)</Chip>}
              {res.mode === 'demo' && <Chip tone="neutral">Demo Mode (offline TF-IDF + rules) — not a live measurement</Chip>}</p>
            {res.mode === 'demo' && <p className="muted small">{res.live_available ? 'Click “Re-score with Gemini” to measure the live pipeline (paced, one call at a time; takes a few minutes).' : 'Live scoring is unavailable (no GEMINI_API_KEY, or Gemini is in cool-down).'}</p>}
          </div>
          <div className="row">
            <input type="range" min="0" max="1" step="0.05" value={thr} onChange={(e) => setThr(Number(e.target.value))} aria-label="threshold" />
            <b className="num big">{fx(thr)}</b>
            <button className="btn ghost sm" onClick={rescore} disabled={!res.live_available || prog?.state === 'running'}
              title={res.live_available ? 'Re-score the dataset with Gemini, one call at a time' : 'Needs GEMINI_API_KEY and Gemini not in cool-down'}>Re-score with Gemini</button>
          </div>
        </div>
        {prog?.state === 'running' && (
          <div className="rescore"><Bar value={prog.done / prog.total} tone="accent" /><small className="muted">{prog.message} ({prog.done}/{prog.total})</small></div>
        )}
        {prog?.state === 'failed' && <p className="neg small">Live re-score failed: {prog.error}</p>}
        <div className="metrics" style={{ opacity: busy ? 0.6 : 1 }}>
          <div className="metric"><span>Precision</span><b>{pct(C.precision)}</b></div>
          <div className="metric"><span>Recall</span><b>{pct(C.recall)}</b></div>
          <div className="metric"><span>F1</span><b>{pct(C.f1)}</b></div>
          <div className="metric"><span>Spam rate</span><b>{pct(C.spam_rate)}</b></div>
          <div className="metric"><span>Missed matches</span><b>{C.missed_matches}</b></div>
          <div className="metric"><span>LLM calls</span><b>{C.llm_calls}</b></div>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <h3>Precision / recall vs threshold</h3>
          <LineChart series={series} marker={thr} xLabel="notification threshold" yLabel="score" />
          <div className="legend">{series.map((s) => <span key={s.name}><i style={{ background: s.color }} />{s.name}</span>)}</div>
          <p className="muted small">Best-F1 threshold on this dataset: <b>{fx(res.best_f1_threshold)}</b>. Precision is not monotonic in the threshold: a single high-scoring false positive moves it a lot with so few notifications.</p>
        </div>

        <div className="card">
          <h3>Strategy comparison <small className="muted">(at threshold {fx(thr)})</small></h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th></th><th>Precision</th><th>Recall</th><th>F1</th><th>Spam rate</th><th>Missed</th><th>LLM calls</th></tr></thead>
              <tbody>
                {[A, B, C].map((s) => (
                  <tr key={s.key} className={s.key === 'C' ? 'hl' : ''}>
                    <td><b>{s.key}.</b> {s.name}</td>
                    <td>{pct(s.precision)}</td><td>{pct(s.recall)}</td><td>{pct(s.f1)}</td>
                    <td>{pct(s.spam_rate)}</td><td>{s.missed_matches}</td>
                    <td>{s.llm_calls} <small className="muted">({s.judge_calls} judge + {s.engagement_calls} draft)</small></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details className="method">
            <summary>How are these metrics defined?</summary>
            <ul>
              <li><b>Unit</b>: one (post, recipient) pair; each post's author is excluded → 75 pairs.</li>
              <li><b>Precision</b> = TP / (TP + FP): of the notifications sent, how many were wanted.</li>
              <li><b>Recall</b> = TP / (TP + FN): of the wanted notifications, how many were sent. <b>Missed matches</b> = FN.</li>
              <li><b>Spam rate</b> = FP / notifications sent (= 1 − precision).</li>
              <li><b>LLM calls</b> = judge calls + one drafting call per notified agent. Broadcast has no judge but makes every recipient draft.</li>
              <li>Same labels, same pairs and the same counting for all three strategies. Labels are hand-written (see DESIGN.md for caveats).</li>
            </ul>
          </details>
        </div>
      </div>

      <div className="card">
        <h3>Evaluation dataset <small className="muted">— cell = pipeline decision vs hand label</small></h3>
        <div className="legend">
          <span><i className="c-tp" />TP notified &amp; wanted</span><span><i className="c-fp" />FP notified, unwanted</span>
          <span><i className="c-fn" />FN missed</span><span><i className="c-tn" />TN correctly skipped</span><span><i className="c-na" />author</span>
        </div>
        <div className="table-wrap">
          <table className="dataset">
            <thead><tr><th>#</th><th>Author</th><th>Post</th>{AGENTS.map((a) => <th key={a} title={a}>{SHORT[a]}</th>)}</tr></thead>
            <tbody>
              {res.posts.map((p) => (
                <tr key={p.post_idx}>
                  <td>{p.post_idx}</td><td>{p.author}</td><td className="post-cell" title={p.content}>{p.content}</td>
                  {AGENTS.map((a) => {
                    const c = cell[`${p.post_idx}|${a}`]
                    const cls = c.label === null ? 'c-na' : `c-${c.outcome.toLowerCase()}`
                    return <td key={a} className={`dot ${cls}`} title={c.label === null ? 'author' : `${a}: label=${c.label ? 'notify' : 'skip'}, final=${fx(c.final_score)}${c.judged ? '' : ' (not judged)'}`}>{c.label === null ? '–' : c.outcome}</td>
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
