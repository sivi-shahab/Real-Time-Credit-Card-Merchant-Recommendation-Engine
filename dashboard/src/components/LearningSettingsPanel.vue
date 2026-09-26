<script setup lang="ts">
/** ADR-0011 — learning switches under maker-checker. One person files a change with a
 *  reason, an Approver (a different person) decides; the backend enforces both. */
import { computed, reactive, ref, watch } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { get, post } from '@/lib/api'
import { when } from '@/lib/format'
import StatePanel from '@/components/StatePanel.vue'
import { useSession } from '@/stores/session'

type Field = { label: string; kind: 'number' | 'bool'; min?: number; max?: number; step?: number }
/** Mirrors the backend's bounds (rec/api/learning_settings.py); the backend re-checks. */
const FIELDS: Record<string, Field> = {
  auto_retrain_interval_hours: { label: 'Interval auto-retrain (jam, 0 = mati)', kind: 'number', min: 0, max: 168, step: 0.5 },
  auto_retrain_min_new_impressions: { label: 'Minimal impression baru', kind: 'number', min: 1, max: 10_000_000 },
  auto_retrain_keep_exports: { label: 'Export yang disimpan', kind: 'number', min: 1, max: 50 },
  online_bandit_enabled: { label: 'Bandit online aktif', kind: 'bool' },
  online_bandit_exploration: { label: 'Eksplorasi bandit', kind: 'number', min: 0, max: 10, step: 0.1 },
  promo_holdout_percent: { label: 'Holdout promo (%)', kind: 'number', min: 0, max: 99 },
}

const session = useSession()
const confirm = useConfirm()
const toast = useToast()
const qc = useQueryClient()

const { data, isPending, error } = useQuery({
  queryKey: ['learning-settings'],
  queryFn: ({ signal }) => get<any>('/admin/v1/learning/settings', signal),
  refetchInterval: 15_000,
})
const pending = computed(() => data.value?.requests?.find((r: any) => r.status === 'PENDING'))

const draft = reactive<Record<string, any>>({})
const reason = ref('')
const note = ref('')
watch(() => data.value?.effective, (values) => { if (values) Object.assign(draft, values) },
      { immediate: true })
const changed = computed(() => Object.fromEntries(Object.entries(draft)
  .filter(([k, v]) => data.value && v !== data.value.effective[k])))

const done = (summary: string) => {
  toast.add({ severity: 'success', summary, life: 4000 })
  qc.invalidateQueries({ queryKey: ['learning-settings'] })
  qc.invalidateQueries({ queryKey: ['learning-status'] })
}
const failed = (e: Error) =>
  toast.add({ severity: 'error', summary: 'Ditolak', detail: e.message, life: 8000 })

const file = useMutation({
  mutationFn: () => post('/admin/v1/learning/settings/requests',
    { changes: changed.value, reason: reason.value }),
  onSuccess: () => { reason.value = ''; done('Perubahan diajukan, menunggu Approver') },
  onError: failed,
})
const decide = useMutation({
  mutationFn: (approve: boolean) =>
    post(`/admin/v1/learning/settings/requests/${pending.value.request_id}/decision`,
      { approve, note: note.value || null }),
  onSuccess: (r: any) => { note.value = ''; done(r.status === 'APPROVED' ? 'Disetujui dan berlaku' : 'Ditolak') },
  onError: failed,
})

function confirmDecision(approve: boolean) {
  confirm.require({
    header: approve ? 'Setujui perubahan pengaturan' : 'Tolak perubahan pengaturan',
    message: approve
      ? 'Perubahan berlaku di semua replica dalam 15 detik, tanpa restart.'
      : 'Pengaturan tidak berubah.',
    acceptLabel: approve ? 'Setujui' : 'Tolak', rejectLabel: 'Batal',
    accept: () => decide.mutate(approve),
  })
}
const show = (v: any) => (typeof v === 'boolean' ? (v ? 'ya' : 'tidak') : String(v))
const statusSeverity = (s: string) =>
  ({ APPROVED: 'success', REJECTED: 'danger', PENDING: 'warn' } as any)[s] ?? 'secondary'
</script>

<template>
  <div class="panel">
    <h3>Pengaturan pembelajaran</h3>
    <p class="state" style="text-align:left;padding:4px 0 12px">
      Perubahan diajukan dengan alasan dan berlaku setelah disetujui Approver (orang yang
      berbeda). Setelah persetujuan pertama, nilai tersimpan mengalahkan env deployment.
    </p>
    <StatePanel :loading="isPending" :error="error">
      <div v-if="data">
        <p class="state" style="text-align:left;padding:0 0 8px">
          Sumber: <Tag :value="data.source === 'approved' ? 'DISETUJUI' : 'ENV'"
                       :severity="data.source === 'approved' ? 'success' : 'secondary'" />
          <template v-if="data.version">
            · versi {{ data.version }} oleh {{ data.updatedBy }}, {{ when(data.updatedAt) }}
          </template>
        </p>
        <table class="plain">
          <thead><tr><th>Pengaturan</th><th>Berlaku</th><th v-if="session.can('learning:request') && !pending">Usulan</th></tr></thead>
          <tbody>
            <tr v-for="(f, name) in FIELDS" :key="name">
              <td>{{ f.label }}<div class="mono sub">{{ name }}</div></td>
              <td class="mono">{{ show(data.effective[name]) }}</td>
              <td v-if="session.can('learning:request') && !pending">
                <ToggleSwitch v-if="f.kind === 'bool'" v-model="draft[name]" />
                <InputNumber v-else v-model="draft[name]" :min="f.min" :max="f.max"
                             :step="f.step ?? 1" :minFractionDigits="0" :maxFractionDigits="2"
                             showButtons style="width:170px" />
              </td>
            </tr>
          </tbody>
        </table>

        <div v-if="session.can('learning:request') && !pending" class="row" style="margin-top:12px">
          <label class="field" style="flex:1">
            Alasan
            <InputText v-model="reason" placeholder="mis. mulai eksperimen uplift Q4" />
          </label>
          <Button label="Ajukan perubahan"
                  :disabled="!Object.keys(changed).length || reason.trim().length < 3"
                  :loading="file.isPending.value" @click="file.mutate()" />
        </div>

        <div v-if="pending" class="panel" style="margin-top:12px;background:#fffaf0">
          <h3>Menunggu persetujuan</h3>
          <p class="sub">
            Diajukan oleh <b>{{ pending.requested_by }}</b>, {{ when(pending.requested_at) }}:
            “{{ pending.reason }}”
          </p>
          <table class="plain">
            <thead><tr><th>Pengaturan</th><th>Sekarang</th><th>Menjadi</th></tr></thead>
            <tbody>
              <tr v-for="(value, name) in pending.changes" :key="name">
                <td class="mono">{{ name }}</td>
                <td class="mono">{{ show(data.effective[name]) }}</td>
                <td class="mono"><b>{{ show(value) }}</b></td>
              </tr>
            </tbody>
          </table>
          <div v-if="session.can('learning:approve')" class="row" style="margin-top:10px">
            <label class="field" style="flex:1">
              Catatan
              <InputText v-model="note" placeholder="opsional" />
            </label>
            <Button label="Setujui" :loading="decide.isPending.value"
                    :disabled="pending.requested_by === session.me?.subject"
                    @click="confirmDecision(true)" />
            <Button label="Tolak" severity="danger" outlined :loading="decide.isPending.value"
                    @click="confirmDecision(false)" />
          </div>
          <p v-else class="sub">Keputusan memerlukan peran Approver.</p>
        </div>

        <h3 style="margin-top:16px">Riwayat permintaan</h3>
        <table class="plain" v-if="data.requests.length">
          <thead><tr><th>Diajukan</th><th>Oleh</th><th>Perubahan</th><th>Status</th><th>Diputuskan</th></tr></thead>
          <tbody>
            <tr v-for="r in data.requests" :key="r.request_id">
              <td>{{ when(r.requested_at) }}</td>
              <td>{{ r.requested_by }}</td>
              <td class="mono">{{ Object.entries(r.changes).map(([k, v]) => `${k}=${show(v)}`).join(', ') }}</td>
              <td><Tag :value="r.status" :severity="statusSeverity(r.status)" /></td>
              <td>{{ r.decided_by ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="state">Belum ada permintaan.</div>
      </div>
    </StatePanel>
  </div>
</template>
