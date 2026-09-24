<script setup lang="ts">
/** UI-002 — configure, estimate, run, inspect manifest + quality report. */
import { computed, ref } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'
import { z } from 'zod'
import { get, post } from '@/lib/api'
import { count, when } from '@/lib/format'
import BarChart from '@/components/BarChart.vue'
import StatePanel from '@/components/StatePanel.vue'
import { useSession } from '@/stores/session'

const schema = z.object({
  seed: z.number().int().min(0),
  customerCount: z.number().int().min(1).max(1_000_000),
  merchantCount: z.number().int().min(1).max(100_000),
  promotionCount: z.number().int().min(0).max(10_000),
  transactionCount: z.number().int().min(1).max(20_000_000),
  historyDays: z.number().int().min(1).max(730),
  scenarioProfile: z.enum(['normal', 'mixed', 'failure']),
})

const form = ref({
  seed: 42, customerCount: 2000, merchantCount: 300, promotionCount: 60,
  transactionCount: 50000, historyDays: 180, scenarioProfile: 'mixed' as const,
})
const errors = computed(() => {
  const parsed = schema.safeParse(form.value)
  return parsed.success ? {} : parsed.error.flatten().fieldErrors
})
const valid = computed(() => Object.keys(errors.value).length === 0)

const session = useSession()
const toast = useToast()
const qc = useQueryClient()
const selected = ref<string | null>(null)
const estimate = ref<any>(null)

const list = useQuery({
  queryKey: ['datasets'],
  queryFn: ({ signal }) => get<any[]>('/admin/v1/datasets?limit=25', signal),
  refetchInterval: 4000,
})
const detail = useQuery({
  queryKey: ['dataset', selected],
  queryFn: ({ signal }) => get<any>(`/admin/v1/datasets/${selected.value}`, signal),
  enabled: computed(() => !!selected.value),
})

const doEstimate = useMutation({
  mutationFn: () => post<any>('/admin/v1/datasets/estimate', form.value),
  onSuccess: (r) => (estimate.value = r),
})
const create = useMutation({
  mutationFn: () => post<any>('/admin/v1/datasets', form.value,
    { 'idempotency-key': `ds-${form.value.seed}-${Date.now()}` }),
  onSuccess: (r) => {
    toast.add({ severity: 'success', summary: 'Job dibuat', detail: r.datasetId, life: 4000 })
    selected.value = r.datasetId
    qc.invalidateQueries({ queryKey: ['datasets'] })
  },
  onError: (e: Error) =>
    toast.add({ severity: 'error', summary: 'Gagal', detail: e.message, life: 6000 }),
})

const severity = (s: string) =>
  ({ COMPLETED: 'success', FAILED: 'danger', RUNNING: 'info' } as any)[s] ?? 'secondary'
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Synthetic Data Studio</h2>
      <p>
        Seed, reference time dan versi generator yang sama menghasilkan checksum identik
        (SYN-006). Dataset ini data simulasi, bukan estimasi perilaku nasabah nyata.
      </p>
    </div>
  </div>

  <div class="grid-2">
    <form class="panel" @submit.prevent="create.mutate()">
      <h3>Konfigurasi</h3>
      <div class="row">
        <label class="field">Seed<InputNumber v-model="form.seed" :useGrouping="false" /></label>
        <label class="field">Nasabah<InputNumber v-model="form.customerCount" /></label>
        <label class="field">Merchant<InputNumber v-model="form.merchantCount" /></label>
        <label class="field">Promo<InputNumber v-model="form.promotionCount" /></label>
        <label class="field">Transaksi<InputNumber v-model="form.transactionCount" /></label>
        <label class="field">Histori (hari)<InputNumber v-model="form.historyDays" /></label>
        <label class="field">
          Skenario
          <Select v-model="form.scenarioProfile" :options="['normal', 'mixed', 'failure']" />
        </label>
      </div>
      <p v-if="!valid" class="state error" style="padding:8px 0;text-align:left">
        Konfigurasi belum valid: {{ Object.keys(errors).join(', ') }}
      </p>
      <div class="row" style="margin-top:12px">
        <Button label="Hitung estimasi" severity="secondary" :loading="doEstimate.isPending.value"
                :disabled="!valid" @click="doEstimate.mutate()" />
        <Button type="submit" label="Jalankan job" :loading="create.isPending.value"
                :disabled="!valid || !session.can('dataset:create') || create.isPending.value" />
      </div>
      <p v-if="!session.can('dataset:create')" class="state" style="text-align:left;padding:8px 0">
        Peran Anda hanya dapat membaca dataset.
      </p>
      <div v-if="estimate" class="card" style="margin-top:12px">
        <div class="label">Estimasi sebelum submit</div>
        <div class="sub">
          {{ count(estimate.estimatedTransactionRows) }} baris transaksi ·
          {{ (estimate.estimatedBytes / 1e6).toFixed(1) }} MB ·
          ± {{ estimate.estimatedSeconds }} detik
        </div>
      </div>
    </form>

    <div class="panel">
      <h3>Job dataset</h3>
      <StatePanel :loading="list.isPending.value" :error="list.error.value"
                  :empty="!list.data.value?.length" empty-text="Belum ada dataset.">
        <table class="plain">
          <thead>
            <tr><th>Dataset</th><th>Status</th><th>Dibuat</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="d in list.data.value" :key="d.dataset_id">
              <td class="mono">{{ d.dataset_id.slice(0, 8) }}…</td>
              <td><Tag :value="d.status" :severity="severity(d.status)" /></td>
              <td>{{ when(d.created_at) }}</td>
              <td>
                <Button label="Detail" text size="small" @click="selected = d.dataset_id" />
              </td>
            </tr>
          </tbody>
        </table>
      </StatePanel>
    </div>
  </div>

  <div v-if="selected" class="panel">
    <h3>Manifest & quality report</h3>
    <StatePanel :loading="detail.isPending.value" :error="detail.error.value">
      <div v-if="detail.data.value?.manifest">
        <div class="cards">
          <div v-for="(v, k) in detail.data.value.manifest.rowCounts" :key="k" class="card">
            <div class="label">{{ k }}</div><div class="value">{{ count(v as number) }}</div>
          </div>
        </div>
        <div class="grid-2" style="margin-top:16px">
          <BarChart title="Distribusi kategori"
                    :data="detail.data.value.manifest.distributionSummary.byCategory" />
          <BarChart title="Distribusi jam (UTC)"
                    :data="detail.data.value.manifest.distributionSummary.byHourUtc" />
        </div>
        <h3 style="margin-top:16px">Quality report</h3>
        <pre class="json">{{ JSON.stringify(detail.data.value.manifest.qualityReport, null, 2) }}</pre>
        <h3>Checksum berkas</h3>
        <table class="plain">
          <thead><tr><th>Berkas</th><th>Baris</th><th>SHA-256</th></tr></thead>
          <tbody>
            <tr v-for="(f, name) in detail.data.value.manifest.files" :key="name">
              <td>{{ name }}</td><td>{{ count((f as any).rows) }}</td>
              <td class="mono">{{ (f as any).sha256.slice(0, 24) }}…</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="state">
        Job berstatus {{ detail.data.value?.status }} — manifest belum tersedia.
      </div>
    </StatePanel>
  </div>
</template>
