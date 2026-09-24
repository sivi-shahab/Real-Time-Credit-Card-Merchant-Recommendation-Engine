/**
 * API client. Errors surface the backend's {code,message,traceId} shape so every
 * view can render a real error state instead of a blank panel.
 *
 * NOTE (SDD 13.4): production must move to the BFF session cookie + CSRF token.
 * This bearer scheme is the Fase 1-3 stand-in; it is the one known deviation and
 * is tracked as Fase 5 work in README.md.
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

export function token(): string {
  return sessionStorage.getItem('rec.token') ?? ''
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${token()}`,
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
    // EventSource cannot set headers; the token rides as a query param here and
    // moves to the session cookie with the BFF.
    es = new EventSource(`${BASE}/admin/v1/events?access_token=${encodeURIComponent(token())}`)
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
