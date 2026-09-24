import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@/lib/api'
import StatePanel from '@/components/StatePanel.vue'

const slot = { default: '<p>isi</p>' }

describe('StatePanel — required UI states (SDD 13.3)', () => {
  it('shows loading and hides content', () => {
    const w = mount(StatePanel, { props: { loading: true }, slots: slot })
    expect(w.text()).toContain('Memuat')
    expect(w.text()).not.toContain('isi')
  })
  it('shows a forbidden message distinct from a generic error', () => {
    const err = new ApiError(403, 'FORBIDDEN', 'role Analyst may not audit:read')
    const w = mount(StatePanel, { props: { error: err }, slots: slot })
    expect(w.text()).toContain('tidak memiliki izin')
    expect(w.text()).not.toContain('isi')
  })
  it('shows the error message and traceId for diagnosis', () => {
    const err = new ApiError(500, 'ERROR', 'boom', 'trace-123')
    const w = mount(StatePanel, { props: { error: err }, slots: slot })
    expect(w.text()).toContain('boom')
    expect(w.text()).toContain('trace-123')
    expect(w.find('[role="alert"]').exists()).toBe(true)
  })
  it('shows an empty state and renders content otherwise', () => {
    expect(mount(StatePanel, { props: { empty: true }, slots: slot }).text())
      .toContain('Tidak ada data')
    expect(mount(StatePanel, { props: {}, slots: slot }).text()).toContain('isi')
  })
})
