/** ADR-0011 maker-checker panel and the Pembelajaran page. The backend enforces every
 *  rule; these check the UI offers the right actions to the right role and sends only
 *  what changed. */
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ConfirmationService from 'primevue/confirmationservice'
import ToastService from 'primevue/toastservice'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import LearningSettingsPanel from '@/components/LearningSettingsPanel.vue'
import { useSession } from '@/stores/session'
import LearningView from '@/views/LearningView.vue'

const effective = {
  auto_retrain_interval_hours: 0, auto_retrain_min_new_impressions: 500,
  auto_retrain_keep_exports: 3, online_bandit_enabled: false,
  online_bandit_exploration: 1, promo_holdout_percent: 0,
}
const pendingBy = (who: string) => ({
  request_id: 'req-1', status: 'PENDING', requested_by: who, reason: 'Q4 uplift',
  requested_at: '2026-09-26T10:00:00Z', decided_by: null,
  changes: { promo_holdout_percent: 5 },
})
const settingsView = (requests: unknown[] = []) => ({
  effective, requestTtlDays: 7, source: 'env', version: null, requests,
})

let calls: { url: string; method: string; body: any }[] = []
function stubApi(routes: Record<string, unknown>) {
  calls = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = (init.method ?? 'GET').toUpperCase()
    calls.push({ url, method, body: init.body ? JSON.parse(String(init.body)) : undefined })
    const key = Object.keys(routes).find((path) => url.endsWith(path))
    return { ok: true, status: 200, json: async () => (key ? routes[key] : {}) } as Response
  }))
}

async function mountAs(component: any, subject: string, permissions: string[]) {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSession().me = { subject, role: 'x', kind: 'admin', permissions, csrfToken: 't' }
  const w = mount(component, {
    global: {
      plugins: [pinia, PrimeVue, ToastService, ConfirmationService,
        [VueQueryPlugin, { queryClient: new QueryClient({
          defaultOptions: { queries: { retry: false } } }) }]],
      stubs: { BarChart: true, LearningSettingsPanel: component !== LearningSettingsPanel },
    },
  })
  await flushPromises()
  await flushPromises()
  return w
}

const button = (w: any, label: string) =>
  w.findAll('button').find((b: any) => b.text().includes(label))

describe('learning settings maker-checker panel (ADR-0011)', () => {
  beforeEach(() => stubApi({ '/admin/v1/learning/settings': settingsView() }))

  it('lets a requester file only what changed, with a reason', async () => {
    const w = await mountAs(LearningSettingsPanel, 'mlops', ['model:read', 'learning:request'])
    expect(w.text()).toContain('promo_holdout_percent')
    const submit = button(w, 'Ajukan perubahan')
    expect(submit.attributes('disabled')).toBeDefined()   // nothing changed, no reason

    const vm = w.vm as any
    vm.draft.promo_holdout_percent = 5
    vm.reason = 'Mulai eksperimen uplift Q4'
    await flushPromises()
    expect(submit.attributes('disabled')).toBeUndefined()
    await submit.trigger('click')
    await flushPromises()

    const filed = calls.find((c) => c.method === 'POST')!
    expect(filed.url).toMatch(/\/admin\/v1\/learning\/settings\/requests$/)
    expect(filed.body).toEqual({ changes: { promo_holdout_percent: 5 },
                                 reason: 'Mulai eksperimen uplift Q4' })
  })

  it('shows an Approver the pending change, but never lets them approve their own', async () => {
    stubApi({ '/admin/v1/learning/settings': settingsView([pendingBy('mlops')]) })
    let w = await mountAs(LearningSettingsPanel, 'approver', ['model:read', 'learning:approve'])
    expect(w.text()).toContain('Menunggu persetujuan')
    expect(w.text()).toContain('Kedaluwarsa')
    expect(button(w, 'Setujui').attributes('disabled')).toBeUndefined()
    expect(button(w, 'Tolak')).toBeDefined()

    stubApi({ '/admin/v1/learning/settings': settingsView([pendingBy('approver')]) })
    w = await mountAs(LearningSettingsPanel, 'approver', ['model:read', 'learning:approve'])
    expect(button(w, 'Setujui').attributes('disabled')).toBeDefined()
  })

  it('offers no form while a change is pending, and no decision without the role', async () => {
    stubApi({ '/admin/v1/learning/settings': settingsView([pendingBy('ops')]) })
    const w = await mountAs(LearningSettingsPanel, 'mlops', ['model:read', 'learning:request'])
    expect(button(w, 'Ajukan perubahan')).toBeUndefined()
    expect(button(w, 'Setujui')).toBeUndefined()
    expect(w.text()).toContain('Keputusan memerlukan peran Approver')
  })
})

describe('Pembelajaran page', () => {
  const status = (holdout: number, uplift: unknown = null) => ({
    autoRetrain: { enabled: false, intervalHours: 0, minNewImpressions: 500, keepExports: 3,
                   lastJob: null },
    bandit: { enabled: true, version: 'online-ucb', exploration: 1, learnIntervalSeconds: 300,
              learnedUntil: null, shadow24h: { comparisons: 40, avg_rank_agreement: 0.5,
                                               top1_agreement_rate: 0.4, model_latency_p95: 4 } },
    promoHoldout: { percent: holdout, arms: { TREATMENT: 90, HOLDOUT: 10 } },
    uplift,
  })

  it('warns that uplift needs a holdout while the split is 0%', async () => {
    stubApi({ '/admin/v1/learning/status': status(0) })
    const w = await mountAs(LearningView, 'analyst', ['model:read'])
    expect(w.text()).toContain('Holdout nonaktif (0%)')
    expect(w.text()).toContain('Belum ada laporan uplift')
    expect(w.text()).toContain('40 perbandingan 24 jam')
  })

  it('turns the uplift segments into percentages for the chart', async () => {
    stubApi({ '/admin/v1/learning/status': status(10, {
      createdAt: '2026-09-26T10:00:00Z', customers: 3000, heldOut: 900,
      conversion: { treatment: 0.36, holdout: 0.2 }, averageEffect: 0.16,
      qini: { model: 0.29, random: -0.04 },
      segments: { persuadable: 0.7, noEffect: 0.03, sleepingDog: 0.27 } }) })
    const w = await mountAs(LearningView, 'analyst', ['model:read'])
    expect(w.text()).not.toContain('Holdout nonaktif')
    expect((w.vm as any).segments).toEqual({ responsif: 70, 'tanpa efek': 3,
                                             'efek negatif': 27 })
  })
})
