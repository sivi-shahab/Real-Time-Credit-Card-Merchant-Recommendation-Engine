<script setup lang="ts">
/** ADR-0007/0010 — what the learning loops are set to and what they last did. Settings
 *  change only through the maker-checker panel below (ADR-0011). */
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import Tag from 'primevue/tag'
import { get } from '@/lib/api'
import { count, metric, ms, percent, when } from '@/lib/format'
import BarChart from '@/components/BarChart.vue'
import LearningSettingsPanel from '@/components/LearningSettingsPanel.vue'
import StatCard from '@/components/StatCard.vue'
import StatePanel from '@/components/StatePanel.vue'

const { data, isPending, error } = useQuery({
  queryKey: ['learning-status'],
  queryFn: ({ signal }) => get<any>('/admin/v1/learning/status', signal),
  refetchInterval: 15_000,
})

const onOff = (on: boolean | undefined) => (on ? 'Aktif' : 'Nonaktif')
const arms = computed(() => data.value?.promoHoldout?.arms ?? {})
const report = computed(() => data.value?.uplift)
const segments = computed((): Record<string, number> => {
  const s = report.value?.segments
  if (!s) return {}
  return {
    // persuadable / no effect / sleeping dog, short enough for the rotated axis
    responsif: Number((s.persuadable * 100).toFixed(1)),
    'tanpa efek': Number((s.noEffect * 100).toFixed(1)),
    'efek negatif': Number((s.sleepingDog * 100).toFixed(1)),
  }
})
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Pembelajaran berkelanjutan</h2>
      <p>
        Model dilatih ulang dari feedback live, bandit online belajar di mode shadow, dan
        holdout promo mengukur uplift. Sistem hanya boleh sampai SHADOW; CANARY, FULL dan
        penargetan promo tetap keputusan manusia, dan pengaturannya berubah hanya lewat
        persetujuan Approver (ADR-0007, ADR-0010, ADR-0011).
      </p>
    </div>
  </div>

  <StatePanel :loading="isPending" :error="error">
    <div class="cards">
      <StatCard label="Auto-retrain" :value="onOff(data.autoRetrain.enabled)"
                :sub="data.autoRetrain.enabled
                  ? `tiap ${data.autoRetrain.intervalHours} jam · ≥ ${count(data.autoRetrain.minNewImpressions)} impression baru`
                  : 'interval 0 jam (mati)'" />
      <StatCard label="Bandit online (shadow)" :value="onOff(data.bandit.enabled)"
                :sub="`${metric(data.bandit.shadow24h.comparisons, count)} perbandingan 24 jam`" />
      <StatCard label="Holdout promo" :value="`${data.promoHoldout.percent}%`"
                :sub="`${count(arms.TREATMENT)} treatment · ${count(arms.HOLDOUT)} holdout`" />
      <StatCard label="Laporan uplift terakhir"
                :value="report ? when(report.createdAt) : 'tidak tersedia'"
                :sub="report ? `${count(report.customers)} nasabah` : 'python -m rec.ml.uplift'" />
    </div>

    <div class="grid-2">
      <div class="panel">
        <h3>Auto-retrain dari feedback live</h3>
        <p class="state" style="text-align:left;padding:4px 0 12px">
          Export Postgres → training job biasa (gerbang, MLflow, registry yang sama). Model yang
          lulus masuk SHADOW hanya bila belum ada model yang melayani. Disimpan
          {{ data.autoRetrain.keepExports }} export terbaru, ditambah yang masih dipakai.
        </p>
        <table class="plain" v-if="data.autoRetrain.lastJob">
          <thead><tr><th>Job terakhir</th><th>Dataset</th><th>Status</th><th>Model</th><th>Gerbang</th></tr></thead>
          <tbody>
            <tr>
              <td class="mono">{{ data.autoRetrain.lastJob.job_id.slice(0, 8) }}…</td>
              <td class="mono">{{ data.autoRetrain.lastJob.dataset_id }}</td>
              <td>{{ data.autoRetrain.lastJob.status }} · {{ when(data.autoRetrain.lastJob.created_at) }}</td>
              <td class="mono">{{ data.autoRetrain.lastJob.model_version ?? '—' }}</td>
              <td>
                <Tag v-if="data.autoRetrain.lastJob.approved === true" value="LULUS" severity="success" />
                <Tag v-else-if="data.autoRetrain.lastJob.approved === false" value="DITOLAK" severity="danger" />
                <span v-else class="sub">—</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="state">Belum ada training otomatis.</div>
      </div>

      <div class="panel">
        <h3>Bandit online <span class="mono">{{ data.bandit.version }}</span></h3>
        <p class="state" style="text-align:left;padding:4px 0 12px">
          UCB (river) atas vektor fitur serving; membandingkan urutannya dengan yang disajikan
          dan tidak pernah melayani nasabah.
        </p>
        <div class="cards">
          <StatCard label="Kesepakatan peringkat"
                    :value="percent(data.bandit.shadow24h.avg_rank_agreement)" sub="24 jam" />
          <StatCard label="Top-1 sama"
                    :value="percent(data.bandit.shadow24h.top1_agreement_rate)" sub="24 jam" />
          <StatCard label="Latensi p95" :value="metric(data.bandit.shadow24h.model_latency_p95, ms)"
                    sub="di luar jalur respons" />
          <StatCard label="Dipelajari hingga" :value="when(data.bandit.learnedUntil)"
                    :sub="`eksplorasi ${data.bandit.exploration} · tiap ${data.bandit.learnIntervalSeconds} dtk`" />
        </div>
      </div>
    </div>

    <div class="panel">
      <h3>Holdout promo dan uplift</h3>
      <p v-if="!data.promoHoldout.percent" class="state" style="text-align:left;padding:8px 0">
        Holdout nonaktif (0%): tanpa grup kontrol, uplift tidak dapat diestimasi. Mengaktifkannya
        memerlukan persetujuan di panel pengaturan di bawah.
      </p>
      <div v-if="report" class="grid-2">
        <div class="cards">
          <StatCard label="Konversi treatment" :value="percent(report.conversion?.treatment)" />
          <StatCard label="Konversi holdout" :value="percent(report.conversion?.holdout)" />
          <StatCard label="Efek rata-rata" :value="percent(report.averageEffect)"
                    sub="treatment − holdout" />
          <StatCard label="Qini model vs acak"
                    :value="`${report.qini?.model?.toFixed(3)} / ${report.qini?.random?.toFixed(3)}`"
                    :sub="`dinilai pada ${count(report.heldOut)} nasabah held-out`" />
        </div>
        <div>
          <BarChart title="Segmen efek (% nasabah held-out)" :data="segments" unit="%" />
          <p class="state" style="text-align:left;padding:4px 0 12px">
            Estimasi saja: belum ada kebijakan yang menahan promo berdasarkan skor ini.
          </p>
        </div>
      </div>
      <div v-else class="state">
        Belum ada laporan uplift. Jalankan <span class="mono">python -m rec.ml.uplift</span>
        setelah jendela outcome 14 hari tertutup.
      </div>
    </div>
  </StatePanel>

  <LearningSettingsPanel />
</template>
