import { defineStore } from 'pinia'
import { get } from '@/lib/api'

export interface Me { subject: string; role: string; kind: string; permissions: string[] }

export const useSession = defineStore('session', {
  state: () => ({ me: null as Me | null, loading: false, error: '' }),
  getters: {
    authenticated: (s) => s.me !== null,
    can: (s) => (action: string) => s.me?.permissions.includes(action) ?? false,
  },
  actions: {
    async login(token: string) {
      sessionStorage.setItem('rec.token', token)
      this.loading = true
      this.error = ''
      try {
        this.me = await get<Me>('/admin/v1/me')
      } catch (e) {
        sessionStorage.removeItem('rec.token')
        this.me = null
        this.error = (e as Error).message
        throw e
      } finally {
        this.loading = false
      }
    },
    async restore() {
      if (!sessionStorage.getItem('rec.token')) return
      try { this.me = await get<Me>('/admin/v1/me') } catch { this.logout() }
    },
    logout() {
      sessionStorage.removeItem('rec.token')
      this.me = null
    },
  },
})
