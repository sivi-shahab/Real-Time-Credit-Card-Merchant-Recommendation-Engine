<script setup lang="ts">
/** UI-004 — filter, cursor pagination, redacted envelope, quarantine reason. */
import { computed, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { get } from '@/lib/api'
import { money, when } from '@/lib/format'
import { debounce, useUrlFilters } from '@/lib/useUrlFilters'
import StatePanel from '@/components/StatePanel.vue'

const filters = useUrlFilters({
  customerId: '', merchantId: '', transactionId: '', outcome: '', limit: 50,
})
const applied = ref({ ...filters })
const push = debounce(() => (applied.value = { ...filters }), 350)
watch(filters, push, { deep: true })

const cursor = ref<number | null>(null)
watch(applied, () => (cursor.value = null))

const query = computed(() => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(applied.value)) if (v) p.set(k, String(v))
  if (cursor.value) p.set('cursor', String(cursor.value))
  return p.toString()
})

/* TanStack cancels the in-flight request when the key changes (SDD 13.3). */
const { data, isPending, isFetching, error } = useQuery({
  queryKey: ['transactions', query],
  queryFn: ({ signal }) => get<any>(`/admin/v1/transactions?${query.value}`, signal),
})

const selected = ref<any>(null)
const detail = useQuery({
  queryKey: ['transaction', selected],
  queryFn: ({ signal }) => get<any>(`/admin/v1/transactions/${selected.value}`, signal),
  enabled: computed(() => !!selected.value),
})

const severity = (o: string) =>
  o === 'APPLIED' ? 'success' : o === 'QUARANTINED' ? 'danger' : 'warn'
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Transaction Explorer</h2>
      <p>Envelope ditampilkan setelah reduksi data sensitif. Filter tersimpan di URL.</p>
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <label class="field">Customer<InputText v-model="filters.customerId" /></label>
      <label class="field">Merchant<InputText v-model="filters.merchantId" /></label>
      <label class="field">Transaction ID<InputText v-model="filters.transactionId" /></label>
      <label class="field">
        Outcome
        <Select v-model="filters.outcome" showClear
                :options="['APPLIED', 'QUARANTINED', 'DUPLICATE_EVENT', 'DUPLICATE_TRANSACTION']" />
      </label>
      <span v-if="isFetching" class="sub">memuat…</span>
    </div>
  </div>

  <div class="panel">
    <StatePanel :loading="isPending" :error="error" :empty="!data?.items?.length"
                empty-text="Tidak ada transaksi yang cocok dengan filter.">
      <table class="plain">
        <thead>
          <tr>
            <th>Waktu kejadian</th><th>Diterima</th><th>Customer</th><th>Merchant</th>
            <th>Jenis</th><th>Nilai</th><th>Outcome</th><th>Alasan</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in data.items" :key="t.event_id">
            <td>{{ when(t.occurred_at) }}</td>
            <td>{{ when(t.received_at) }}</td>
            <td class="mono">{{ t.customer_id }}</td>
            <td class="mono">{{ t.merchant_id }}</td>
            <td>{{ t.txn_type }}</td>
            <td>{{ money(Number(t.amount_minor)) }}</td>
            <td><Tag :value="t.outcome" :severity="severity(t.outcome)" /></td>
            <td class="mono">{{ t.reject_code ?? '—' }}</td>
            <td><Button label="Envelope" text size="small" @click="selected = t.event_id" /></td>
          </tr>
        </tbody>
      </table>
      <div class="row" style="margin-top:12px">
        <Button label="Halaman berikutnya" size="small" :disabled="!data.nextCursor"
                @click="cursor = data.nextCursor" />
        <Button v-if="cursor" label="Kembali ke awal" text size="small" @click="cursor = null" />
      </div>
    </StatePanel>
  </div>

  <Dialog :visible="!!selected" modal header="Detail event" style="width:min(760px,92vw)"
          @update:visible="selected = null">
    <StatePanel :loading="detail.isPending.value" :error="detail.error.value">
      <p v-if="detail.data.value?.reject_detail" class="state error" style="text-align:left">
        {{ detail.data.value.reject_code }} — {{ detail.data.value.reject_detail }}
      </p>
      <table class="plain">
        <tbody>
          <tr><th>Diterima</th><td>{{ when(detail.data.value?.received_at) }}</td></tr>
          <tr><th>Kejadian</th><td>{{ when(detail.data.value?.occurred_at) }}</td></tr>
          <tr><th>Correlation</th><td class="mono">{{ detail.data.value?.correlation_id }}</td></tr>
        </tbody>
      </table>
      <pre class="json">{{ JSON.stringify(detail.data.value?.envelope, null, 2) }}</pre>
    </StatePanel>
  </Dialog>
</template>
