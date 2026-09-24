/** SDD 13.3 — money, numbers and dates render in Indonesian locale. */
const idr = new Intl.NumberFormat('id-ID', {
  style: 'currency', currency: 'IDR', maximumFractionDigits: 0,
})
const num = new Intl.NumberFormat('id-ID')
const dt = new Intl.DateTimeFormat('id-ID', { dateStyle: 'medium', timeStyle: 'medium' })

/** amountMinor is an integer; IDR has no sub-unit in this system (SDD 6.1). */
export const money = (minor: number | null | undefined) =>
  minor === null || minor === undefined ? '—' : idr.format(minor)
export const count = (n: number | null | undefined) =>
  n === null || n === undefined ? '—' : num.format(n)
export const percent = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`
export const when = (iso: string | null | undefined) =>
  iso ? dt.format(new Date(iso)) : '—'
export const ms = (v: number | null | undefined) =>
  v === null || v === undefined ? '—' : `${num.format(Math.round(v))} ms`
/** UI-001: an unavailable metric must never render as zero. */
export const metric = (v: number | null | undefined, fmt: (n: number) => string) =>
  v === null || v === undefined ? 'tidak tersedia' : fmt(v)
