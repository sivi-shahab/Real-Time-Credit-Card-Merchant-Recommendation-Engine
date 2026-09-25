import { defineStore } from 'pinia'
import { get, post, setCsrf } from '@/lib/api'

export interface Me {
  subject: string; role: string; kind: string; permissions: string[]; csrfToken: string | null
}
export interface AuthConfig { sso: boolean; devLogin: boolean }

/** No credential is stored client-side: the session is the HttpOnly cookie (SDD 13.4). */
export const useSession = defineStore('session', {
  state: () => ({ me: null as Me | null, loading: false, error: '' }),
  getters: {
    authenticated: (s) => s.me !== null,
    can: (s) => (action: string) => s.me?.permissions.includes(action) ?? false,
  },
  actions: {
    async refresh() {
      this.me = await get<Me>('/admin/v1/me')
      setCsrf(this.me.csrfToken ?? '')
    },
    /** Local/test only — the backend refuses it elsewhere. */
    async devLogin(token: string) {
      this.loading = true
      this.error = ''
      try {
        await post('/bff/dev-login', { token })
        await this.refresh()
      } catch (e) {
        this.me = null
        this.error = (e as Error).message
        throw e
      } finally {
        this.loading = false
      }
    },
    async restore() {
      try { await this.refresh() } catch { this.me = null }
    },
    async logout() {
      const out = await post<{ endSessionUrl: string | null }>('/bff/logout').catch(() => null)
      this.me = null
      setCsrf('')
      if (out?.endSessionUrl) window.location.assign(out.endSessionUrl)
    },
  },
})
