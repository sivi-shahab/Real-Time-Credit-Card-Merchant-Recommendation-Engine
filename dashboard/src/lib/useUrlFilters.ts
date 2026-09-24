import { reactive, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

/** SDD 13.3 — primary filters live in the URL so views are shareable/reloadable. */
export function useUrlFilters<T extends Record<string, string | number | undefined>>(
  defaults: T,
) {
  const route = useRoute()
  const router = useRouter()
  const state = reactive({ ...defaults }) as T
  for (const key of Object.keys(defaults)) {
    const fromUrl = route.query[key]
    if (typeof fromUrl === 'string' && fromUrl !== '') {
      ;(state as any)[key] = typeof defaults[key] === 'number' ? Number(fromUrl) : fromUrl
    }
  }
  watch(state, (value) => {
    const query: Record<string, string> = {}
    for (const [k, v] of Object.entries(value)) {
      if (v !== undefined && v !== '' && v !== null) query[k] = String(v)
    }
    router.replace({ query })
  }, { deep: true })
  return state
}

export function debounce<A extends unknown[]>(fn: (...args: A) => void, wait = 300) {
  let timer: ReturnType<typeof setTimeout> | undefined
  return (...args: A) => {
    clearTimeout(timer)
    timer = setTimeout(() => fn(...args), wait)
  }
}
