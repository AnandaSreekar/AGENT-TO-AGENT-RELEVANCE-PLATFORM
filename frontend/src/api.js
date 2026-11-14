import { useEffect, useRef } from 'react'

// Backend origin. Empty (default) = same-origin relative URLs (local dev proxy, or Render serving the UI itself).
// Set VITE_API_BASE at BUILD time (Vite bakes it in) when the UI is hosted elsewhere, e.g. on Vercel.
const BASE = (import.meta.env.VITE_API_BASE || '').trim().replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(message, status, code, details) {
    super(message)
    this.status = status
    this.code = code
    this.details = details
  }
}

export function getAdminToken() {
  try { return localStorage.getItem('a2a_admin_token') || '' } catch { return '' }
}
export function setAdminToken(v) {
  try { localStorage.setItem('a2a_admin_token', v) } catch { /* storage may be unavailable */ }
}

export async function api(path, { method = 'GET', body, headers = {}, admin = false } = {}) {
  const h = { ...headers }
  if (body !== undefined) h['Content-Type'] = 'application/json'
  if (admin && getAdminToken()) h['X-Admin-Token'] = getAdminToken()
  let res
  try {
    res = await fetch(`${BASE}${path}`, { method, headers: h, body: body !== undefined ? JSON.stringify(body) : undefined })
  } catch {
    throw new ApiError('Cannot reach the backend. Is it running on port 8000?', 0, 'network')
  }
  let data = null
  try { data = await res.json() } catch { /* empty body */ }
  if (!res.ok) {
    const e = data?.error || {}
    const detail = e.details?.map((d) => `${d.field}: ${d.problem}`).join('; ')
    throw new ApiError(e.message ? (detail ? `${e.message} ${detail}` : e.message) : `Request failed (${res.status})`,
      res.status, e.code, e.details)
  }
  return data
}

/** Subscribe to the backend SSE stream; handler(type, payload) is called for each event. */
export function useEvents(handler, onStatus) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const types = ['post_created', 'notified', 'comment_created', 'comment_pending', 'comment_rejected', 'ignored', 'agent_registered']
    const es = new EventSource(`${BASE}/api/events`)
    const listeners = types.map((t) => {
      const fn = (e) => { try { ref.current(t, JSON.parse(e.data).data) } catch { /* ignore malformed */ } }
      es.addEventListener(t, fn)
      return [t, fn]
    })
    es.onopen = () => onStatus && onStatus(true)
    es.onerror = () => onStatus && onStatus(false)
    return () => { listeners.forEach(([t, fn]) => es.removeEventListener(t, fn)); es.close() }
  }, [])
}

export const pct = (v) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(0)}%`)
export const fx = (v, d = 2) => (v === null || v === undefined ? '—' : Number(v).toFixed(d))
export const ago = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

/** Listen to the single app-wide event stream (see App.jsx). */
export function useBus(handler) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const fn = (e) => ref.current(e.detail.type, e.detail.data)
    window.addEventListener('a2a', fn)
    return () => window.removeEventListener('a2a', fn)
  }, [])
}
