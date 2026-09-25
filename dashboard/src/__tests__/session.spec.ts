import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { post } from '@/lib/api'
import { useSession } from '@/stores/session'

const me = { subject: 'analyst', role: 'Analyst', kind: 'admin', csrfToken: 'csrf-1',
             permissions: ['metrics:read', 'recommendation:preview'] }
const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response

describe('session (SDD 13.4, SEC-001)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    sessionStorage.clear()
    localStorage.clear()
  })

  it('reflects backend permissions and stores no credential in the browser', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(ok({})).mockResolvedValueOnce(ok(me)))
    const s = useSession()
    await s.devLogin('analyst-token')
    expect(s.can('recommendation:preview')).toBe(true)
    expect(s.can('audit:read')).toBe(false)
    expect(sessionStorage.length + localStorage.length).toBe(0)
  })

  it('sends the CSRF token on mutations and cookies on every request', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(ok({})).mockResolvedValueOnce(ok(me))
      .mockResolvedValue(ok({}))
    vi.stubGlobal('fetch', fetch)
    await useSession().devLogin('analyst-token')
    await post('/admin/v1/recommendations/preview', {})
    const [, init] = fetch.mock.calls.at(-1)!
    expect(init.credentials).toBe('include')
    expect(init.headers['x-csrf-token']).toBe('csrf-1')
  })

  it('surfaces a failed login and treats anonymous as permitting nothing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 401, statusText: 'Unauthorized',
      json: async () => ({ code: 'UNAUTHENTICATED', message: 'invalid token' }),
    } as Response))
    const s = useSession()
    await expect(s.devLogin('bad')).rejects.toThrow('invalid token')
    expect(s.can('metrics:read')).toBe(false)
  })
})
