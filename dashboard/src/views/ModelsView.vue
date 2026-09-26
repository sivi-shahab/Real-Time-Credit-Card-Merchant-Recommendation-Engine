<script setup lang="ts">
/** UI-008 — model version, dataset lineage, feature schema, training params, holdout
 *  metrics vs baseline, segment performance, deployment status, approval history.
 *  Promote/rollback are hidden by role here and re-checked by the backend. */
import { computed, ref } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { get, post } from '@/lib/api'
import { count, metric, ms, percent, when } from '@/lib/format'
import BarChart from '@/components/BarChart.vue'
import StatCard from '@/components/StatCard.vue'
import StatePanel from '@/components/StatePanel.vue'
import { useSession } from '@/stores/session'

const session = useSession()
const confirm = useConfirm()
const toast = useToast()
const qc = useQueryClient()

const selected = ref<string | null>(null)
const trainDataset = ref<string | null>(null)
const numRounds = ref(300)
const tuneTrials = ref(0)
const promoteMode = ref<'SHADOW' | 'CANARY' | 'FULL'>('SHADOW')
const canaryPercent = ref(10)

const models = useQuery({
  queryKey: ['models'],
  queryFn: ({ signal }) => get<any>('/admin/v1/models?limit=50', signal),
  refetchInterval: 8000,
})
const jobs = useQuery({
  queryKey: ['training-jobs'],
  queryFn: ({ signal }) => get<any[]>('/admin/v1/training-jobs?limit=15', signal),
  refetchInterval: 5000,
})
const datasets = useQuery({
  queryKey: ['datasets'],
  queryFn: ({ signal }) => get<any[]>('/admin/v1/datasets?limit=25', signal),
})
const shadow = useQuery({
  queryKey: ['shadow'],
  queryFn: ({ signal }) => get<any>('/admin/v1/models/shadow/summary?hours=24', signal),
  refetchInterval: 10000,
})
const detail = useQuery({
  queryKey: ['model', selected],
  queryFn: ({ signal }) => get<any>(`/admin/v1/models/${selected.value}`, signal),
  enabled: computed(() => !!selected.value),
})

const completedDatasets = computed(() =>
  (datasets.data.value ?? []).filter((d) => d.status === 'COMPLETED').map((d) => d.dataset_id))
const deployment = computed(() => models.data.value?.deployment ?? {})

const startTraining = useMutation({
  mutationFn: () => post<any>('/admin/v1/training-jobs',
    { datasetId: trainDataset.value, numRounds: numRounds.value,
      tuneTrials: tuneTrials.value }),
  onSuccess: (r) => {
    toast.add({ severity: 'success', summary: 'Training dimulai', detail: r.jobId, life: 4000 })
    qc.invalidateQueries({ queryKey: ['training-jobs'] })
  },
  onError: (e: Error) =>
    toast.add({ severity: 'error', summary: 'Gagal', detail: e.message, life: 7000 }),
})

const promote = useMutation({
  mutationFn: (modelVersion: string) => post(`/admin/v1/models/${modelVersion}/promote`,
    { mode: promoteMode.value, canaryPercent: canaryPercent.value }),
  onSuccess: () => {
    toast.add({ severity: 'success', summary: 'Deployment diperbarui', life: 4000 })
    qc.invalidateQueries({ queryKey: ['models'] })
  },
  onError: (e: Error) =>
    toast.add({ severity: 'error', summary: 'Promosi ditolak', detail: e.message, life: 9000 }),
})

const rollback = useMutation({
  mutationFn: (modelVersion: string) => post(`/admin/v1/models/${modelVersion}/rollback`),
  onSuccess: () => {
    toast.add({ severity: 'warn', summary: 'Rollback dijalankan', life: 4000 })
    qc.invalidateQueries({ queryKey: ['models'] })
  },
  onError: (e: Error) =>
    toast.add({ severity: 'error', summary: 'Gagal', detail: e.message, life: 7000 }),
})

function confirmPromote(model: any) {
  const scope = promoteMode.value === 'CANARY'
    ? `${canaryPercent.value}% nasabah`
    : promoteMode.value === 'SHADOW' ? 'tanpa memengaruhi trafik' : 'seluruh trafik'
  confirm.require({
    header: 'Konfirmasi promosi model',
    message: `Promosikan ${model.model_version} ke mode ${promoteMode.value} (${scope})? `
      + 'Cache rekomendasi akan dibatalkan.',
    acceptLabel: 'Promosikan', rejectLabel: 'Batal',
    accept: () => promote.mutate(model.model_version),
  })
}

function confirmRollback() {
  confirm.require({
    header: 'Konfirmasi rollback',
    message: 'Kembalikan serving ke model sebelumnya atau ke baseline?',
    acceptLabel: 'Rollback', rejectLabel: 'Batal',
    accept: () => rollback.mutate(deployment.value.model_version ?? 'baseline'),
  })
}

const modeSeverity = (mode: string) =>
  ({ FULL: 'success', CANARY: 'warn', SHADOW: 'info' } as any)[mode] ?? 'secondary'
const jobSeverity = (status: string) =>
  ({ COMPLETED: 'success', FAILED: 'danger', RUNNING: 'info' } as any)[status] ?? 'secondary'

const segmentChart = computed(() => {
  const segments = detail.data.value?.segment_metrics?.segment_history
  if (!segments) return {}
  return Object.fromEntries(Object.entries(segments.model ?? {})
    .map(([k, v]: any) => [k, Number((v['ndcg@10'] ?? 0).toFixed(4))]))
})
const importanceChart = computed<Record<string, number>>(() => {
  const gain = detail.data.value?.artifacts?.featureImportanceGain as
    Record<string, number> | undefined
  if (!gain) return {}
  return Object.fromEntries(
    Object.entries(gain).slice(0, 12).map(([k, v]) => [k, Number(Number(v).toFixed(2))]))
})
/** ADR-0008: estimated click propensity by display position, relative to the top slot. */
const positionBiasChart = computed<Record<string, number>>(() => {
  const clicked = detail.data.value?.artifacts?.positionBias?.clicked as number[] | undefined
  if (!clicked) return {}
  return Object.fromEntries(clicked.map((v, i) => [`posisi ${i + 1}`, Number(v.toFixed(3))]))
})
const tuning = computed(() => detail.data.value?.lineage?.tuning)
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Model dan eksperimen</h2>
      <p>
        Skor offline yang tinggi tidak cukup untuk promosi: gerbang evaluasi harus lulus dan
        persetujuan diberikan oleh peran Approver, bukan oleh pelatih model (ML-006, SEC-001).
      </p>
    </div>
  </div>

  <StatePanel :loading="models.isPending.value" :error="models.error.value">
    <div class="cards">
      <StatCard label="Mode deployment" :value="deployment.mode ?? '—'"
                :sub="deployment.model_version ?? models.data.value.activeBaseline" />
      <StatCard label="Canary" :value="`${deployment.canary_percent ?? 0}%`"
                :sub="deployment.promoted_by ? `oleh ${deployment.promoted_by}` : 'belum ada promosi'" />
      <StatCard label="Feature schema serving"
                :value="models.data.value.servingFeatureSchemaVersion" />
      <StatCard label="Shadow perbandingan 24 jam"
                :value="metric(shadow.data.value?.comparisons, count)"
                :sub="`kesepakatan peringkat ${percent(shadow.data.value?.avg_rank_agreement)} · top-1 ${percent(shadow.data.value?.top1_agreement_rate)}`" />
      <StatCard label="Latensi model p95 (shadow)"
                :value="metric(shadow.data.value?.model_latency_p95, ms)" />
    </div>

    <div class="panel" v-if="deployment.mode !== 'BASELINE'">
      <div class="row" style="justify-content:space-between">
        <div>
          <Tag :value="deployment.mode" :severity="modeSeverity(deployment.mode)" />
          <span class="mono" style="margin-left:8px">{{ deployment.model_version }}</span>
          <span class="sub" style="margin-left:8px">
            dipromosikan {{ when(deployment.promoted_at) }}
            <template v-if="deployment.previous_version">
              · sebelumnya {{ deployment.previous_version }}
            </template>
          </span>
        </div>
        <Button v-if="session.can('model:rollback')" label="Rollback" severity="danger"
                outlined size="small" @click="confirmRollback()" />
      </div>
    </div>
  </StatePanel>

  <div class="grid-2">
    <div class="panel">
      <h3>Jalankan training</h3>
      <div class="row">
        <label class="field">
          Dataset
          <Select v-model="trainDataset" :options="completedDatasets"
                  placeholder="Pilih dataset selesai" style="min-width:260px" />
        </label>
        <label class="field">Boosting rounds<InputNumber v-model="numRounds" :min="10" :max="5000" /></label>
        <label class="field">
          Trial Optuna<InputNumber v-model="tuneTrials" :min="0" :max="200" />
        </label>
        <Button label="Latih model"
                :disabled="!trainDataset || !session.can('training:run') || startTraining.isPending.value"
                :loading="startTraining.isPending.value" @click="startTraining.mutate()" />
      </div>
      <p v-if="!session.can('training:run')" class="state" style="text-align:left;padding:8px 0">
        Hanya peran ML Engineer yang dapat menjalankan training.
      </p>
      <p class="state" style="text-align:left;padding:4px 0 12px">
        Trial Optuna &gt; 0 menala hyperparameter pada potongan validasi dari periode training;
        data uji hanya dipakai gerbang (ADR-0009).
      </p>
    </div>

    <div class="panel">
      <h3>Training job</h3>
      <StatePanel :loading="jobs.isPending.value" :error="jobs.error.value"
                  :empty="!jobs.data.value?.length" empty-text="Belum ada training job.">
        <div style="overflow-x:auto">
        <table class="plain">
          <thead>
            <tr><th>Job</th><th>Dataset</th><th>Status</th><th>Model</th><th>Gerbang</th></tr>
          </thead>
          <tbody>
            <tr v-for="j in jobs.data.value" :key="j.job_id">
              <td>
                <div class="mono">{{ j.job_id.slice(0, 8) }}…</div>
                <Tag :value="j.trigger === 'auto' ? 'OTOMATIS' : 'MANUAL'"
                     :severity="j.trigger === 'auto' ? 'info' : 'secondary'"
                     style="font-size:11px;margin-top:4px" />
                <span v-if="j.tune_trials" class="sub"> Optuna {{ j.tune_trials }}</span>
              </td>
              <td class="mono">{{ j.dataset_id.slice(0, 8) }}…</td>
              <td><Tag :value="j.status" :severity="jobSeverity(j.status)" /></td>
              <td class="mono">{{ j.model_version ?? '—' }}</td>
              <td>
                <Tag v-if="j.approved === true" value="LULUS" severity="success" />
                <Tag v-else-if="j.approved === false" value="DITOLAK" severity="danger" />
                <span v-else class="sub">—</span>
              </td>
            </tr>
          </tbody>
        </table>
        </div>
        <p v-if="jobs.data.value?.some((j: any) => j.error)" class="state error"
           style="text-align:left">
          {{ jobs.data.value.find((j: any) => j.error).error }}
        </p>
      </StatePanel>
    </div>
  </div>

  <div class="panel">
    <h3>Versi model</h3>
    <div class="row" v-if="session.can('model:promote')" style="margin-bottom:10px">
      <label class="field">
        Mode promosi
        <Select v-model="promoteMode" :options="['SHADOW', 'CANARY', 'FULL']" />
      </label>
      <label class="field" v-if="promoteMode === 'CANARY'">
        Canary %<InputNumber v-model="canaryPercent" :min="1" :max="100" />
      </label>
    </div>
    <StatePanel :loading="models.isPending.value" :error="models.error.value"
                :empty="!models.data.value?.models?.length"
                empty-text="Belum ada model terlatih.">
      <table class="plain">
        <thead>
          <tr>
            <th>Versi</th><th>Dataset</th><th>NDCG@10</th><th>Baseline</th><th>NDCG@5</th>
            <th>Latensi p95</th><th>Feature schema</th><th>Gerbang</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in models.data.value.models" :key="m.model_version"
              :style="m.model_version === deployment.model_version
                ? 'background:#f2f7ff' : ''">
            <td class="mono">{{ m.model_version }}</td>
            <td class="mono">{{ m.dataset_id.slice(0, 8) }}…</td>
            <td class="mono">{{ m.metrics['ndcg@10']?.toFixed(4) }}</td>
            <td class="mono">{{ m.baseline_metrics['ndcg@10']?.toFixed(4) }}</td>
            <td class="mono">{{ m.metrics['ndcg@5']?.toFixed(4) }}</td>
            <td>{{ ms(m.metrics.inferenceLatencyMsP95) }}</td>
            <td class="mono">{{ m.feature_schema_version }}</td>
            <td>
              <Tag :value="m.approved ? 'LULUS' : 'DITOLAK'"
                   :severity="m.approved ? 'success' : 'danger'" />
            </td>
            <td>
              <Button label="Detail" text size="small" @click="selected = m.model_version" />
              <Button v-if="session.can('model:promote')" label="Promosikan" text size="small"
                      :disabled="!m.approved" @click="confirmPromote(m)" />
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!session.can('model:promote')" class="state" style="text-align:left;padding:8px 0">
        Promosi model memerlukan peran Approver.
      </p>
    </StatePanel>
  </div>

  <div v-if="selected" class="panel">
    <h3>Detail {{ selected }}</h3>
    <StatePanel :loading="detail.isPending.value" :error="detail.error.value">
      <div v-if="detail.data.value">
        <h3>Gerbang evaluasi</h3>
        <table class="plain">
          <thead><tr><th>Gerbang</th><th>Hasil</th><th>Keterangan</th></tr></thead>
          <tbody>
            <tr v-for="g in detail.data.value.gates" :key="g.name">
              <td class="mono">{{ g.name }}</td>
              <td><Tag :value="g.passed ? 'PASS' : 'FAIL'"
                       :severity="g.passed ? 'success' : 'danger'" /></td>
              <td class="sub">{{ g.detail }}</td>
            </tr>
          </tbody>
        </table>

        <div class="grid-2" style="margin-top:16px">
          <div><BarChart title="NDCG@10 per kedalaman histori" :data="segmentChart" /></div>
          <div><BarChart title="Feature importance (gain)" :data="importanceChart" /></div>
          <div>
            <BarChart title="Bias posisi terestimasi (relatif ke posisi 1)"
                      :data="positionBiasChart" />
            <p class="state" style="text-align:left;padding:4px 0 12px">
              Peluang klik per posisi tampil yang diestimasi unbiased LambdaMART dan
              dipakai untuk men-debias label (ADR-0008).
            </p>
          </div>
          <div v-if="tuning">
            <h3>Tuning Optuna</h3>
            <p class="state" style="text-align:left;padding:4px 0 12px">
              {{ tuning.trials }} trial · {{ tuning.metric }} terbaik
              <span class="mono">{{ Number(tuning.bestValue).toFixed(4) }}</span>
              <template v-if="tuning.heldFixed?.length">
                · ditetapkan pemanggil: {{ tuning.heldFixed.join(', ') }}
              </template>
            </p>
            <pre class="json">{{ JSON.stringify(tuning.bestParams, null, 2) }}</pre>
          </div>
        </div>

        <h3 style="margin-top:16px">Dataset lineage dan parameter</h3>
        <p v-if="detail.data.value.lineage?.metricCaveat" class="state"
           style="text-align:left;padding:8px 0">
          {{ detail.data.value.lineage.metricCaveat }}
        </p>
        <pre class="json">{{ JSON.stringify({
          lineage: detail.data.value.lineage,
          metrics: detail.data.value.metrics,
          baseline: detail.data.value.baseline_metrics,
          mlflowRunId: detail.data.value.mlflow_run_id,
        }, null, 2) }}</pre>
      </div>
    </StatePanel>
  </div>
</template>
