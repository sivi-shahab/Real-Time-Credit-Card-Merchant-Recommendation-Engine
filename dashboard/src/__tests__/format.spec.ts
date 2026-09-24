import { describe, expect, it } from 'vitest'
import { count, metric, money, percent, when } from '@/lib/format'

describe('locale formatting (SDD 13.3)', () => {
  it('renders IDR from integer minor units', () => {
    expect(money(150000).replace(/ /g, ' ')).toContain('150.000')
    expect(money(0).replace(/ /g, ' ')).toContain('0')
  })
  it('never renders an unavailable metric as zero (UI-001)', () => {
    expect(metric(null, count)).toBe('tidak tersedia')
    expect(metric(undefined, count)).toBe('tidak tersedia')
    expect(metric(0, count)).toBe('0')
  })
  it('renders missing values as em dash, not blank', () => {
    expect(money(null)).toBe('—')
    expect(count(undefined)).toBe('—')
    expect(percent(null)).toBe('—')
    expect(when(null)).toBe('—')
  })
  it('formats percentages and thousands in id-ID', () => {
    expect(percent(0.1234)).toBe('12.3%')
    expect(count(1234567)).toBe('1.234.567')
  })
})
