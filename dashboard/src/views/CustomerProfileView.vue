<script setup lang="ts">
/** UI-005 — windows, category interest, affinity, freshness, consent status. */
import { computed, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import { get } from '@/lib/api'
import { count, money, percent, when } from '@/lib/format'
import { debounce, useUrlFilters } from '@/lib/useUrlFilters'
import BarChart from '@/components/BarChart.vue'
import StatCard from '@/components/StatCard.vue'
import StatePanel from '@/components/StatePanel.vue'

const filters = useUrlFilters({ search: '', customerId: '' })
const search = ref(filters.search)
watch(search, debounce((v: string) => (filters.search = v), 350))

const customers = useQuery({
  queryKey: ['customers', computed(() => filters.search)],
  queryFn: ({ signal }) =>
    get<any[]>(`/admin/v1/customers?limit=25&search=${encodeURIComponent(filters.search)}`, signal),
})
const profile = useQuery({
  queryKey: ['features', computed(() => filters.customerId)],
  queryFn: ({ signal }) =>
    get<any>(`/admin/v1/customers/${filters.customerId}/features`, signal),
  enabled: computed(() => !!filters.customerId),
})

const interest = computed(() => {
  const raw: Record<string, number> = profile.data.value?.categoryInterest ?? {}
  return Object.fromEntries(
    Object.entries(raw).sort((a, b) => b[1] - a[1]).slice(0, 10)
      .map(([k, v]) => [k, Number(v.toFixed(4))]))
})
const affinity = computed(() =>
  Object.entries(profile.data.value?.merchantAffinity ?? {})
    .sort((a: any, b: any) => b[1].count - a[1].count).slice(0, 10))
const hours = computed(() => profile.data.value?.activeHourDistribution ?? {})
const w = (d: string) => profile.data.value?.windows?.[d] ?? {}
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Profil nasabah</h2>
      <p>
        Setiap pembukaan profil dicatat di audit trail. Pencarian massal dan ekspor dibatasi.
      </p>
    </div>
  </div>

  <div class="grid-2">
    <div class="panel">
      <h3>Cari nasabah</h3>
      <InputText v-model="search" placeholder="customerId" style="width:100%" />
      <StatePanel :loading="customers.isPending.value" :error="customers.error.value"
                  :empty="!customers.data.value?.length">
        <table class="plain" style="margin-top:10px">
          <thead><tr><th>Customer</th><th>Kota</th><th>Tier</th><th>Event</th></tr></thead>
          <tbody>
            <tr v-for="c in customers.data.value" :key="c.customer_id"
                style="cursor:pointer" @click="filters.customerId = c.customer_id">
              <td class="mono">{{ c.customer_id }}</td>
              <td>{{ c.city_code }}</td>
              <td>{{ c.card_tier }}</td>
              <td>{{ count(Number(c.event_count)) }}</td>
            </tr>
          </tbody>
        </table>
      </StatePanel>
    </div>

    <div class="panel">
      <h3>Metadata fitur</h3>
      <div v-if="!filters.customerId" class="state">Pilih nasabah untuk melihat profil.</div>
      <StatePanel v-else :loading="profile.isPending.value" :error="profile.error.value">
        <table class="plain">
          <tbody>
            <tr><th>Feature as-of</th><td>{{ when(profile.data.value.featureAsOf) }}</td></tr>
            <tr><th>Event terakhir</th>
                <td>{{ when(profile.data.value.lastEventOccurredAt) }}</td></tr>
            <tr><th>Feature version</th><td>{{ profile.data.value.featureVersion }}</td></tr>
            <tr><th>Schema</th><td>{{ profile.data.value.featureSchemaVersion }}</td></tr>
            <tr><th>Kedalaman histori</th>
                <td>{{ count(profile.data.value.historyDepthDays) }} hari</td></tr>
            <tr><th>Personalisasi</th><td>
              <Tag :value="profile.data.value.personalizationAllowed ? 'DIIZINKAN' : 'DITOLAK'"
                   :severity="profile.data.value.personalizationAllowed ? 'success' : 'warn'" />
            </td></tr>
            <tr><th>Cold start</th><td>
              <Tag :value="profile.data.value.coldStartFlag ? 'YA' : 'TIDAK'"
                   :severity="profile.data.value.coldStartFlag ? 'warn' : 'success'" />
            </td></tr>
          </tbody>
        </table>
      </StatePanel>
    </div>
  </div>

  <StatePanel v-if="filters.customerId" :loading="profile.isPending.value"
              :error="profile.error.value">
    <div class="cards" style="margin-top:16px">
      <StatCard v-for="d in ['1', '7', '30', '90']" :key="d" :label="`Net spend ${d} hari`"
                :value="money(w(d).netSpendMinor)"
                :sub="`${count(w(d).transactionCount)} transaksi · rata-rata ${money(w(d).averageSpendMinor)}`" />
      <StatCard label="Kota favorit" :value="profile.data.value.preferredCity ?? '—'" />
      <StatCard label="Hari sejak transaksi terakhir"
                :value="count(profile.data.value.daysSinceLastTransaction)" />
    </div>

    <div class="grid-2" style="margin-top:16px">
      <div class="panel">
        <BarChart title="Minat kategori (I = 0,4F + 0,3M + 0,3R)" :data="interest" />
      </div>
      <div class="panel"><BarChart title="Distribusi jam aktif (UTC)" :data="hours" /></div>
    </div>

    <div class="panel">
      <h3>Merchant affinity</h3>
      <table class="plain">
        <thead><tr><th>Merchant</th><th>Frekuensi</th><th>Recency</th></tr></thead>
        <tbody>
          <tr v-for="[mid, a] in affinity" :key="mid">
            <td class="mono">{{ mid }}</td>
            <td>{{ count((a as any).count) }}</td>
            <td>{{ percent((a as any).recency, 1) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </StatePanel>
</template>
