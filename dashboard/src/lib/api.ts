/**
 * API client. Errors surface the backend's {code,message,traceId} shape so every
 * view can render a real error state instead of a blank panel.
 *
 * Auth (SDD 13.4): the BFF session lives in an HttpOnly cookie the browser sends on its
 * own; JS never sees a credential. Mutations echo the session's CSRF token, which is
 * held in memory only (it is not a credential on its own).
 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly traceId?: string,
    readonly fieldErrors: unknown[] = [],
  ) {
    super(message)
  }
  get forbidden() { return this.status === 403 }
  get unauthenticated() { return this.status === 401 }
}

const BASE = import.meta.env.VITE_API_BASE ?? ''

let csrf = ''
export function setCsrf(value: string) { csrf = value }

const SAFE = new Set(['GET', 'HEAD', 'OPTIONS'])

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase()
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      'content-type': 'application/json',
      ...(SAFE.has(method) ? {} : { 'x-csrf-token': csrf }),
      ...(init.headers ?? {}),
    },
  })
  if (!res.ok) {
    let body: any = {}
    try { body = await res.json() } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, body.code ?? 'ERROR',
      body.message ?? res.statusText, body.traceId, body.fieldErrors ?? [])
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

export const get = <T>(path: string, signal?: AbortSignal) => api<T>(path, { signal })
export const post = <T>(path: string, body?: unknown, headers?: HeadersInit) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}), headers })
export const patch = <T>(path: string, body: unknown) =>
  api<T>(path, { method: 'PATCH', body: JSON.stringify(body) })

/** SSE with backoff; callers fall back to polling while `connected` is false. */
export function subscribe(onEvent: (e: unknown) => void, onState: (up: boolean) => void) {
  let attempt = 0
  let closed = false
  let es: EventSource | null = null
  const open = () => {
    if (closed) return
    es = new EventSource(`${BASE}/admin/v1/events`, { withCredentials: true })
    es.onopen = () => { attempt = 0; onState(true) }
    es.onmessage = (ev) => { try { onEvent(JSON.parse(ev.data)) } catch { /* keepalive */ } }
    es.onerror = () => {
      onState(false)
      es?.close()
      attempt = Math.min(attempt + 1, 6)
      setTimeout(open, Math.min(1000 * 2 ** attempt, 30_000))
    }
  }
  open()
  return () => { closed = true; es?.close() }
}
