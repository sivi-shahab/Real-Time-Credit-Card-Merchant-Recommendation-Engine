import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSession } from '@/stores/session'

describe('session permissions gate the UI (SEC-001)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    sessionStorage.clear()
  })

  it('reflects backend-issued permissions and drops the token on failure', async () => {
    const me = { subject: 'analyst', role: 'Analyst', kind: 'admin',
                 permissions: ['metrics:read', 'recommendation:preview'] }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      { ok: true, status: 200, json: async () => me } as Response))
    const s = useSession()
    await s.login('analyst-token')
    expect(s.can('recommendation:preview')).toBe(true)
    expect(s.can('audit:read')).toBe(false)
    expect(sessionStorage.getItem('rec.token')).toBe('analyst-token')

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 401, statusText: 'Unauthorized',
      json: async () => ({ code: 'UNAUTHENTICATED', message: 'invalid token' }),
    } as Response))
    await expect(useSession().login('bad')).rejects.toThrow()
    expect(sessionStorage.getItem('rec.token')).toBeNull()
  })

  it('treats an anonymous session as permitting nothing', () => {
    expect(useSession().can('metrics:read')).toBe(false)
  })
})
