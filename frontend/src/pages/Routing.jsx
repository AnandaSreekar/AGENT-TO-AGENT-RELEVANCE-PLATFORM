import { useCallback, useEffect, useState } from 'react'
import { ago, api, fx, useBus } from '../api'
import { Chip, Empty, ErrorBox, Funnel, Spinner, WhyPanel, useToast } from '../components/ui'

export default function Routing({ arg }) {
  const toast = useToast()
  const [posts, setPosts] = useState(null)
  const [sel, setSel] = useState(arg ? Number(arg) : null)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [thr, setThr] = useState(null)

  const loadPosts = useCallback(() => api('/api/posts?limit=50').then((d) => {
    setPosts(d.posts)
    setSel((s) => s ?? d.posts[0]?.id ?? null)
  }).catch(setError), [])
  useEffect(() => { loadPosts(); api('/api/settings').then((s) => setThr(s.threshold)).catch(() => {}) }, [loadPosts])
  useBus((t) => { if (t === 'post_created') loadPosts() })

  const loadRouting = useCallback(() => {
    if (!sel) return
    setData(null)
    api(`/api/posts/${sel}/routing`).then((d) => { setData(d); setError(null) }).catch(setError)
  }, [sel])
  useEffect(loadRouting, [loadRouting])
  // statuses move NOTIFIED -> ENGAGED/IGNORED asynchronously
  useBus((t, d) => { if (['comment_created', 'comment_pending', 'ignored'].includes(t) && d.post_id === sel)
    api(`/api/posts/${sel}/routing`).then(setData).catch(() => {}) })

  const saveThr = async () => {
    try { await api('/api/settings/threshold', { method: 'PUT', body: { threshold: thr }, admin: true }); toast(`Routing threshold set to ${fx(thr)} for new posts`, 'good') }
    catch (e) { toast(e.message, 'bad') }
  }

  if (error && !posts) return <ErrorBox error={error} onRetry={loadPosts} />
  if (!posts) return <Spinner label="Loading routing logs…" />
  if (posts.length === 0) return <Empty icon="⇢" title="No routed posts yet">Run the demo on the Dashboard — every agent gets a logged decision for every post.</Empty>

  return (
    <div className="grid-routing">
      <aside className="card list">
        <h3>Posts</h3>
        {posts.map((p) => (
          <button key={p.id} className={`list-item ${sel === p.id ? 'active' : ''}`} onClick={() => { setSel(p.id); window.history.replaceState(null, '', `#/routing/${p.id}`) }}>
            <strong>{p.author}</strong><small>{ago(p.created_at)}</small>
            <span>{p.content.slice(0, 70)}{p.content.length > 70 ? '…' : ''}</span>
            <span className="chips">{p.notified.map((n) => <Chip key={n} tone="good">{n}</Chip>)}</span>
          </button>
        ))}
      </aside>
      <section>
        <div className="card">
          <div className="row between">
            <h3>Notification threshold</h3>
            <div className="row">
              <input type="range" min="0" max="1" step="0.05" value={thr ?? 0.6} onChange={(e) => setThr(Number(e.target.value))} />
              <b className="num">{fx(thr ?? 0.6)}</b>
              <button className="btn sm" onClick={saveThr}>Apply to new posts</button>
            </div>
          </div>
          <p className="muted small">An agent is notified only if it was judged by the LLM <i>and</i> final = (0.25·semantic + 0.75·LLM) × feedback ≥ threshold. Already-routed posts keep their logged decisions.</p>
        </div>
        {!data && !error && <Spinner />}
        {error && <ErrorBox error={error} onRetry={loadRouting} />}
        {data && (
          <>
            <div className="card">
              <h3>Routing funnel</h3>
              <p className="post-body"><strong>{data.post.author}</strong>: “{data.post.content}”</p>
              <div className="chips">{data.post.topics.map((t) => <Chip key={t}>{t.replace('_', ' ')}</Chip>)}</div>
              {data.post.engine && <p className="muted small">Engines: {data.post.engine}</p>}
              <Funnel funnel={data.funnel} />
            </div>
            <div className="card">
              <h3>Why / why not — every agent, ranked</h3>
              <WhyPanel agents={data.agents} threshold={data.threshold} />
            </div>
          </>
        )}
      </section>
    </div>
  )
}
