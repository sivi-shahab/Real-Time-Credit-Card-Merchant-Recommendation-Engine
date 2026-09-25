<script setup lang="ts">
/** UI-003 — state machine CREATED → RUNNING ↔ PAUSED → COMPLETED/STOPPED/FAILED.
 *  Buttons follow the state; the backend still rejects invalid commands (409). */
import { computed, ref } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import ProgressBar from 'primevue/progressbar'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'
import { get, post } from '@/lib/api'
import { count, when } from '@/lib/format'
import StatePanel from '@/components/StatePanel.vue'

const toast = useToast()
const qc = useQueryClient()
const datasetId = ref<string | null>(null)
const targetTps = ref(200)
const speed = ref(1)

const datasets = useQuery({
  queryKey: ['datasets'],
  queryFn: ({ signal }) => get<any[]>('/admin/v1/datasets?limit=25', signal),
})
const completed = computed(() =>
  (datasets.data.value ?? []).filter((d) => d.status === 'COMPLETED').map((d) => d.dataset_id))

const runs = useQuery({
  queryKey: ['simulations'],
  queryFn: ({ signal }) => get<any[]>('/admin/v1/simulations', signal),
  refetchInterval: 2000,
})

const create = useMutation({
  mutationFn: () => post<any>('/admin/v1/simulations',
    { datasetId: datasetId.value, targetTps: targetTps.value, speedMultiplier: speed.value }),
  onSuccess: () => qc.invalidateQueries({ queryKey: ['simulations'] }),
  onError: (e: Error) =>
    toast.add({ severity: 'error', summary: 'Gagal', detail: e.message, life: 6000 }),
})
const command = useMutation({
  mutationFn: ({ id, cmd }: { id: string; cmd: string }) =>
    post(`/admin/v1/simulations/${id}/${cmd}`),
  onSuccess: () => qc.invalidateQueries({ queryKey: ['simulations'] }),
  onError: (e: Error) =>
    toast.add({ severity: 'warn', summary: 'Perintah ditolak', detail: e.message, life: 6000 }),
})

const allowed: Record<string, string[]> = {
  CREATED: ['start', 'stop'],
  RUNNING: ['pause', 'stop'],
  PAUSED: ['resume', 'stop'],
  FAILED: ['resume'],  // resumes from the checkpoint (SIM-002)
  COMPLETED: [], STOPPED: [],
}
const label: Record<string, string> = {
  start: 'Mulai', pause: 'Jeda', resume: 'Lanjut', stop: 'Hentikan',
}
const severity = (s: string) =>
  ({ RUNNING: 'success', PAUSED: 'warn', FAILED: 'danger', COMPLETED: 'info' } as any)[s]
  ?? 'secondary'
const progress = (r: any) =>
  r.total_events ? Math.round((Number(r.offset_pos) / Number(r.total_events)) * 100) : 0
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Transaction Simulator</h2>
      <p>
        Replay dataset ke Kafka dengan kontrol kecepatan dan checkpoint. Stop menghentikan
        publikasi baru; pesan yang sudah masuk Kafka tetap diproses (SIM-003).
      </p>
    </div>
  </div>

  <div class="panel">
    <h3>Jalankan replay</h3>
    <div class="row">
      <label class="field">
        Dataset
        <Select v-model="datasetId" :options="completed" placeholder="Pilih dataset selesai"
                style="min-width:280px" />
      </label>
      <label class="field">Target TPS<InputNumber v-model="targetTps" :min="1" :max="5000" /></label>
      <label class="field">
        Speed multiplier
        <InputNumber v-model="speed" :min="0.1" :max="100" :maxFractionDigits="1" />
      </label>
      <Button label="Buat run" :disabled="!datasetId || create.isPending.value"
              :loading="create.isPending.value" @click="create.mutate()" />
    </div>
  </div>

  <div class="panel">
    <h3>Run</h3>
    <StatePanel :loading="runs.isPending.value" :error="runs.error.value"
                :empty="!runs.data.value?.length" empty-text="Belum ada simulasi.">
      <table class="plain">
        <thead>
          <tr>
            <th>Run</th><th>Status</th><th>Progress</th><th>Target TPS</th><th>Actual TPS</th>
            <th>Terkirim</th><th>Gagal</th><th>Sisa</th><th>Diperbarui</th><th>Aksi</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs.data.value" :key="r.run_id">
            <td class="mono">{{ r.run_id.slice(0, 8) }}…</td>
            <td><Tag :value="r.status" :severity="severity(r.status)" /></td>
            <td style="min-width:140px">
              <ProgressBar :value="progress(r)" style="height:10px" />
              <span class="mono">checkpoint {{ count(Number(r.offset_pos)) }}</span>
            </td>
            <td>{{ count(r.target_tps) }}</td>
            <td>{{ count(Number(r.actual_tps)) }}</td>
            <td>{{ count(Number(r.sent_count)) }}</td>
            <td>{{ count(Number(r.failed_count)) }}</td>
            <td>{{ count(Number(r.total_events) - Number(r.offset_pos)) }}</td>
            <td>{{ when(r.updated_at) }}</td>
            <td>
              <Button v-for="cmd in allowed[r.status]" :key="cmd" :label="label[cmd]" text
                      size="small" @click="command.mutate({ id: r.run_id, cmd })" />
              <span v-if="!allowed[r.status].length" class="sub">terminal</span>
            </td>
          </tr>
        </tbody>
      </table>
    </StatePanel>
  </div>
</template>
