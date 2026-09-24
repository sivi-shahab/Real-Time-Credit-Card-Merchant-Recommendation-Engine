<script setup lang="ts">
/** UI-001 — throughput, freshness, cache/fallback, quarantine. */
import { computed, onUnmounted, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { get, subscribe } from '@/lib/api'
import { count, metric, percent, when } from '@/lib/format'
import BarChart from '@/components/BarChart.vue'
import StatCard from '@/components/StatCard.vue'
import StatePanel from '@/components/StatePanel.vue'

const live = ref(false)
const stop = subscribe(() => refetch(), (up) => (live.value = up))
onUnmounted(stop)

const { data, isPending, error, refetch, dataUpdatedAt } = useQuery({
  queryKey: ['metrics'],
  queryFn: ({ signal }) => get<any>('/admin/v1/metrics/overview', signal),
  // SSE is the primary signal; polling is the documented fallback (SDD 13.3)
  refetchInterval: computed(() => (live.value ? 15_000 : 5_000)),
})

const quarantine = computed(() => data.value?.quarantineByReason ?? {})
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Ikhtisar operasional</h2>
      <p>
        Jendela 15 menit terakhir kecuali disebutkan lain. Metrik yang belum tersedia
        ditandai “tidak tersedia”, bukan nol.
      </p>
    </div>
    <div style="font-size:12px;color:#65708a;text-align:right">
      <div>{{ live ? 'SSE tersambung' : 'SSE terputus — polling 5 detik' }}</div>
      <div>Refresh terakhir: {{ when(new Date(dataUpdatedAt).toISOString()) }}</div>
    </div>
  </div>

  <StatePanel :loading="isPending" :error="error">
    <div class="cards">
      <StatCard label="Throughput" :value="metric(data.ingestion.throughputPerSecond, count)"
                unit="tps" sub="rata-rata 60 detik terakhir" />
      <StatCard label="Event diterapkan" :value="count(data.ingestion.appliedEvents)"
                sub="kumulatif" />
      <StatCard label="Event dikarantina" :value="count(data.ingestion.quarantinedEvents)"
                sub="tidak mencemari feature store" />
      <StatCard label="Duplikat ditolak" :value="count(data.ingestion.duplicateEvents)"
                sub="dedup eventId + transactionId" />
      <StatCard label="Event→serving freshness"
                :value="metric(data.freshness.eventToServingSeconds, (v) => `${v}`)"
                unit="detik" sub="rata-rata 15 menit" />
      <StatCard label="Cache hit rate"
                :value="metric(data.serving.cacheHitRate, (v) => percent(v))"
                sub="TTL 5 menit" />
      <StatCard label="Fallback rate"
                :value="metric(data.serving.fallbackRate, (v) => percent(v))"
                sub="SERV-003" />
      <StatCard label="Rekomendasi disajikan"
                :value="count(data.serving.recommendationsServed)" sub="kumulatif" />
      <StatCard label="Model aktif" :value="data.serving.activeModel"
                :sub="`ranking ${data.serving.rankingConfigVersion}`" />
      <StatCard label="Impression / klik"
                :value="`${count(data.feedback.impressions)} / ${count(data.feedback.clicks)}`"
                sub="kumulatif" />
    </div>

    <div class="panel" v-if="Object.keys(quarantine).length">
      <h3>Karantina menurut alasan</h3>
      <BarChart title="Event dikarantina" :data="quarantine" unit="event" />
    </div>
    <div class="panel" v-else>
      <h3>Karantina menurut alasan</h3>
      <div class="state">Belum ada event dikarantina pada rentang ini.</div>
    </div>
  </StatePanel>
</template>
