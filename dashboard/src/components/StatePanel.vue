<script setup lang="ts">
/** SDD 13.3 — every data surface has loading / empty / error / forbidden states. */
import { computed } from 'vue'
import { ApiError } from '@/lib/api'

const props = defineProps<{
  loading?: boolean
  error?: unknown
  empty?: boolean
  emptyText?: string
}>()

const forbidden = computed(() => props.error instanceof ApiError && props.error.forbidden)
const message = computed(() => (props.error as Error | undefined)?.message ?? '')
const traceId = computed(() =>
  props.error instanceof ApiError ? props.error.traceId : undefined)
</script>

<template>
  <div v-if="loading" class="state" role="status" aria-live="polite">Memuat data…</div>
  <div v-else-if="forbidden" class="state error">
    Anda tidak memiliki izin untuk melihat data ini.
  </div>
  <div v-else-if="error" class="state error" role="alert">
    Gagal memuat: {{ message }}
    <div v-if="traceId" class="mono">traceId: {{ traceId }}</div>
  </div>
  <div v-else-if="empty" class="state">{{ emptyText ?? 'Tidak ada data.' }}</div>
  <slot v-else />
</template>
