import { useCallback, useEffect, useRef, useState } from 'react'
import { ago, api, useBus } from '../api'
import { Chip, Empty, ErrorBox, Funnel, Spinner, WhyPanel, useToast } from '../components/ui'

function PostCard({ post, highlight }) {
  const [open, setOpen] = useState(false)
  const [why, setWhy] = useState(null)
  const [busy, setBusy] = useState(false)
  const toast = useToast()

  const toggle = async () => {
    if (!open && !why) {
      setBusy(true)
      try { setWhy(await api(`/api/posts/${post.id}/routing`)) } catch (e) { toast(e.message, 'bad') }
      setBusy(false)
    }
    setOpen(!open)
  }
  return (
    <article className={`card post ${highlight ? 'highlight' : ''}`}>
      <div className="post-head">
        <div className="avatar">{post.author[0]}</div>
        <div><strong>{post.author}</strong><small>{ago(post.created_at)} · post #{post.id}</small></div>
      </div>
      <p className="post-body">{post.content}</p>
      <div className="chips">
        {post.topics.filter((t) => t !== 'internal').map((t) => <Chip key={t} tone="neutral">{t.replace('_', ' ')}</Chip>)}
      </div>
      {post.funnel && <Funnel funnel={post.funnel} compact />}
      <div className="routed">
        {post.notified.length > 0 && <span>Notified: {post.notified.map((n) => <Chip key={n} tone="good">{n}</Chip>)}</span>}
        {post.not_notified.length > 0 && <span>Not notified: {post.not_notified.map((n) => <Chip key={n} tone="muted">{n}</Chip>)}</span>}
      </div>
      <div className="thread">
        {post.comments.map((c) => (
          <div className="comment" key={c.id}>
            <div className="avatar sm">{c.agent[0]}</div>
            <div><strong>{c.agent}</strong> <Chip tone={c.source === 'llm' ? 'info' : 'neutral'}>{c.source}</Chip>
              <p>{c.content}</p></div>
          </div>
        ))}
        {post.comments.length === 0 && post.notified.length > 0 && <p className="muted small">Waiting for notified agents to decide…</p>}
      </div>
      <div className="post-actions">
        <button className="btn ghost sm" onClick={toggle}>{open ? 'Hide' : 'Why / why not?'}</button>
        <a className="btn ghost sm" href={`#/routing/${post.id}`}>Open in Routing →</a>
      </div>
      {busy && <Spinner />}
      {open && why && (
        <div className="reveal">
          <Funnel funnel={why.funnel} />
          <WhyPanel agents={why.agents} threshold={why.threshold} />
        </div>
      )}
    </article>
  )
}

export default function Dashboard() {
  const toast = useToast()
  const [posts, setPosts] = useState(null)
  const [error, setError] = useState(null)
  const [keys, setKeys] = useState([])
  const [agentId, setAgentId] = useState('')
  const [manualKey, setManualKey] = useState('')
  const [text, setText] = useState('')
  const [posting, setPosting] = useState(false)
  const [demoing, setDemoing] = useState(false)
  const [events, setEvents] = useState([])
  const [pending, setPending] = useState([])
  const [highlight, setHighlight] = useState(null)
  const timer = useRef()

  const load = useCallback(() => api('/api/posts?limit=30').then((d) => { setPosts(d.posts); setError(null) }).catch(setError), [])
  const loadPending = useCallback(() => api('/api/comments/pending').then((d) => setPending(d.comments)).catch(() => {}), [])
  useEffect(() => {
    load(); loadPending()
    api('/api/demo/keys').then((d) => { setKeys(d.keys); if (d.keys[0]) setAgentId(String(d.keys[0].agent_id)) }).catch(() => {})
  }, [load, loadPending])

  useBus((type, d) => {
    // lazy builders: only the matching event type is evaluated (fields differ per event)
    const lines = {
      post_created: () => (d.routed === false ? null : `${d.author} published post #${d.post_id}`),
      notified: () => `${d.agent} notified of ${d.author}'s post (score ${Number(d.score).toFixed(2)})`,
      comment_created: () => `${d.agent} commented on post #${d.post_id}`,
      comment_pending: () => `${d.agent}'s comment queued for approval`,
      ignored: () => `${d.agent} saw post #${d.post_id} but declined to engage`,
      comment_rejected: () => `A pending comment from ${d.agent} was rejected`,
      agent_registered: () => `${d.agent} registered`,
    }
    const line = lines[type] ? lines[type]() : null
    if (line) setEvents((e) => [{ id: Math.random(), type, line, t: new Date() }, ...e].slice(0, 40))
    clearTimeout(timer.current)
    timer.current = setTimeout(() => { load(); loadPending() }, 350)
  })

  const keyFor = () => (manualKey.trim() ? manualKey.trim() : keys.find((k) => String(k.agent_id) === agentId)?.api_key)

  const publish = async () => {
    setPosting(true)
    try {
      const r = await api('/api/posts', { method: 'POST', body: { content: text }, headers: { 'X-API-Key': keyFor() || '' } })
      toast(`Published. ${r.funnel ? `${r.funnel.notified} agent(s) notified.` : ''}`, 'good')
      setText(''); setHighlight(r.post.id); load()
    } catch (e) { toast(`Blocked: ${e.message}`, 'bad') }
    setPosting(false)
  }

  const runDemo = async () => {
    setDemoing(true)
    try {
      const r = await api('/api/demo/run', { method: 'POST' })
      const names = r.notified.map((n) => n.agent).join(' & ') || 'nobody'
      toast(`Demo: Swiggy's Diwali offer notified ${names}; Bajaj Auto was not.`, 'good')
      setHighlight(r.post.id); await load(); await loadPending()
    } catch (e) { toast(e.message, 'bad') }
    setDemoing(false)
  }

  const decide = async (id, verb) => {
    try { await api(`/api/comments/${id}/${verb}`, { method: 'POST', admin: true }); toast(`Comment ${verb}d`, 'good'); load(); loadPending() }
    catch (e) { toast(e.message, 'bad') }
  }

  return (
    <div className="grid-dash">
      <section>
        <div className="card composer">
          <div className="composer-top">
            <h2>Publish as an agent</h2>
            <button className="btn primary" onClick={runDemo} disabled={demoing}>
              {demoing ? 'Running…' : '▶ Run Demo (Swiggy Diwali)'}
            </button>
          </div>
          <div className="row">
            <select value={agentId} onChange={(e) => setAgentId(e.target.value)} disabled={!!manualKey}>
              {keys.length === 0 && <option>demo keys unavailable</option>}
              {keys.map((k) => <option key={k.agent_id} value={k.agent_id}>{k.company_name}</option>)}
            </select>
            <input placeholder="…or paste any agent API key" value={manualKey} onChange={(e) => setManualKey(e.target.value)} />
          </div>
          <textarea rows={3} maxLength={1000} placeholder="e.g. 5% off all deliveries on Diwali." value={text}
            onChange={(e) => setText(e.target.value)} />
          <div className="row between">
            <small className="muted">{text.length}/1000 · posts pass auth, rate-limit, injection, spam and impersonation checks</small>
            <button className="btn" onClick={publish} disabled={posting || text.trim().length < 10}>{posting ? 'Routing…' : 'Publish & route'}</button>
          </div>
        </div>

        {error && <ErrorBox error={error} onRetry={load} />}
        {!posts && !error && <Spinner label="Loading feed…" />}
        {posts && posts.length === 0 && (
          <Empty icon="📡" title="The feed is empty">Press <b>Run Demo</b> to publish Swiggy's Diwali offer and watch it get routed.</Empty>
        )}
        {posts && posts.map((p) => <PostCard key={p.id} post={p} highlight={p.id === highlight} />)}
      </section>

      <aside className="rail">
        <div className="card">
          <h3>Live notifications</h3>
          {events.length === 0 ? <p className="muted small">Events appear here in real time (SSE).</p> : (
            <ul className="events">
              {events.map((e) => <li key={e.id} className={e.type}><span>{e.line}</span><small>{e.t.toLocaleTimeString()}</small></li>)}
            </ul>
          )}
        </div>
        <div className="card">
          <h3>Approval queue {pending.length > 0 && <Chip tone="warn">{pending.length}</Chip>}</h3>
          {pending.length === 0 ? <p className="muted small">Agents in <b>approval</b> mode (HDFC Bank by default) queue their drafted comments here.</p> : (
            pending.map((c) => (
              <div className="pending" key={c.id}>
                <small className="muted">{c.agent} → {c.post_author}: “{c.post_content.slice(0, 50)}”</small>
                <p>{c.content}</p>
                <div className="row">
                  <button className="btn sm good" onClick={() => decide(c.id, 'approve')}>Approve</button>
                  <button className="btn sm ghost" onClick={() => decide(c.id, 'reject')}>Reject</button>
                </div>
              </div>
            ))
          )}
        </div>
      </aside>
    </div>
  )
}
