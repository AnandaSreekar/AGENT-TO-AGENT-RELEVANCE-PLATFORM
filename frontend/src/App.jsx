import { useCallback, useEffect, useState } from 'react'
import { api, getAdminToken, setAdminToken, useEvents } from './api'
import { ModeBadge, ToastProvider, useToast } from './components/ui'
import Dashboard from './pages/Dashboard'
import Agents from './pages/Agents'
import Routing from './pages/Routing'
import RelevanceLab from './pages/RelevanceLab'
import AttackLab from './pages/AttackLab'
import Feedback from './pages/Feedback'

const NAV = [
  ['dashboard', 'Dashboard', '◉'],
  ['agents', 'Agents', '☷'],
  ['routing', 'Routing', '⇢'],
  ['lab', 'Relevance Lab', '⚖'],
  ['attack', 'Attack Lab', '⛨'],
  ['feedback', 'Feedback', '↻'],
]

function parseHash() {
  const [, page = 'dashboard', arg] = window.location.hash.split('/')
  return { page: NAV.some((n) => n[0] === page) ? page : 'dashboard', arg }
}

function Shell() {
  const toast = useToast()
  const [route, setRoute] = useState(parseHash())
  const [llm, setLlm] = useState(null)
  const [live, setLive] = useState(false)
  const [menu, setMenu] = useState(false)
  const [token, setToken] = useState(getAdminToken())

  useEffect(() => {
    const on = () => { setRoute(parseHash()); setMenu(false) }
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  const refreshMode = useCallback(() => api('/api/health').then((h) => setLlm(h.llm)).catch(() => setLlm(null)), [])
  useEffect(() => { refreshMode(); const t = setInterval(refreshMode, 20000); return () => clearInterval(t) }, [refreshMode])

  // global event bus: pages subscribe via window events so there is exactly one EventSource
  useEvents((type, data) => {
    window.dispatchEvent(new CustomEvent('a2a', { detail: { type, data } }))
    if (type === 'notified') toast(`${data.agent} notified (score ${Number(data.score).toFixed(2)})`, 'info')
    if (type === 'comment_created') toast(`${data.agent} commented`, 'good')
    if (type === 'comment_pending') toast(`${data.agent}'s comment awaits approval`, 'warn')
  }, setLive)

  const nav = (p) => { window.location.hash = `#/${p}` }
  const Page = { dashboard: Dashboard, agents: Agents, routing: Routing, lab: RelevanceLab, attack: AttackLab, feedback: Feedback }[route.page]

  return (
    <div className="app">
      <aside className={`side ${menu ? 'open' : ''}`}>
        <div className="brand"><span className="logo">A2A</span><div><strong>Relevance</strong><small>agent-to-agent routing</small></div></div>
        <nav>
          {NAV.map(([k, label, icon]) => (
            <a key={k} className={route.page === k ? 'active' : ''} onClick={() => nav(k)} href={`#/${k}`}>
              <span className="nav-icon">{icon}</span>{label}
            </a>
          ))}
        </nav>
        <div className="side-foot">
          <label className="admin">Admin token <span className="muted">(only if ADMIN_TOKEN is set)</span>
            <input type="password" value={token} placeholder="optional"
              onChange={(e) => { setToken(e.target.value); setAdminToken(e.target.value) }} />
          </label>
        </div>
      </aside>
      <div className="main">
        <header className="top">
          <button className="burger" aria-label="Menu" onClick={() => setMenu((m) => !m)}>☰</button>
          <h1>{NAV.find((n) => n[0] === route.page)[1]}</h1>
          <div className="top-right">
            <span className={`live-dot ${live ? 'on' : ''}`} title={live ? 'Live event stream connected' : 'Event stream disconnected'}>
              {live ? 'live' : 'offline'}
            </span>
            <ModeBadge llm={llm} />
          </div>
        </header>
        <main className="content" key={route.page}>
          <Page arg={route.arg} llm={llm} />
        </main>
      </div>
    </div>
  )
}

export default function App() {
  return <ToastProvider><Shell /></ToastProvider>
}
