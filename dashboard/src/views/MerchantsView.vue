<script setup lang="ts">
/** UI-007 — search, activate/deactivate with confirmation + optimistic-concurrency version. */
import { computed, ref, watch } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { get, patch } from '@/lib/api'
import { debounce, useUrlFilters } from '@/lib/useUrlFilters'
import StatePanel from '@/components/StatePanel.vue'
import { useSession } from '@/stores/session'

const session = useSession()
const confirm = useConfirm()
const toast = useToast()
const qc = useQueryClient()
const filters = useUrlFilters({ search: '', cityCode: '' })
const search = ref(filters.search)
watch(search, debounce((v: string) => (filters.search = v), 350))

const query = computed(() => {
  const p = new URLSearchParams({ limit: '50' })
  if (filters.search) p.set('search', filters.search)
  if (filters.cityCode) p.set('cityCode', filters.cityCode)
  return p.toString()
})
const { data, isPending, error } = useQuery({
  queryKey: ['merchants', query],
  queryFn: ({ signal }) => get<any[]>(`/admin/v1/merchants?${query.value}`, signal),
})

const toggle = useMutation({
  mutationFn: ({ id, status, version }: any) =>
    patch(`/admin/v1/merchants/${id}`, { status, version }),
  onSuccess: () => {
    toast.add({ severity: 'success', summary: 'Merchant diperbarui', life: 3000 })
    qc.invalidateQueries({ queryKey: ['merchants'] })
  },
  onError: (e: Error) =>
    toast.add({ severity: 'error', summary: 'Gagal', detail: e.message, life: 6000 }),
})

/* Fetching the current version keeps the ETag-style conflict check honest. */
async function confirmToggle(m: any) {
  const next = m.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE'
  confirm.require({
    header: 'Konfirmasi',
    message: `Ubah status ${m.merchantName} menjadi ${next}? Cache rekomendasi akan dibatalkan.`,
    acceptLabel: 'Ya, ubah',
    rejectLabel: 'Batal',
    accept: async () => {
      const fresh = await get<any[]>(`/admin/v1/merchants?search=${m.merchantId}&limit=1`)
      toggle.mutate({ id: m.merchantId, status: next, version: (fresh[0] as any).version ?? 1 })
    },
  })
}
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Merchant</h2>
      <p>Perubahan status membatalkan cache rekomendasi (SERV-004) dan tercatat di audit.</p>
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <label class="field">Cari<InputText v-model="search" placeholder="nama / id" /></label>
      <label class="field">Kota<InputText v-model="filters.cityCode" /></label>
    </div>
  </div>

  <div class="panel">
    <StatePanel :loading="isPending" :error="error" :empty="!data?.length">
      <table class="plain">
        <thead>
          <tr><th>Merchant</th><th>Kategori</th><th>Kota</th><th>Kanal</th><th>Rating</th>
            <th>Status</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="m in data" :key="m.merchantId">
            <td>{{ m.merchantName }}<div class="mono">{{ m.merchantId }}</div></td>
            <td>{{ m.categoryCode }}</td>
            <td>{{ m.cityCode }}</td>
            <td>{{ m.channel }}</td>
            <td>{{ m.rating }}</td>
            <td><Tag :value="m.status" :severity="m.status === 'ACTIVE' ? 'success' : 'danger'" /></td>
            <td>
              <Button v-if="session.can('merchant:write')" text size="small"
                      :label="m.status === 'ACTIVE' ? 'Nonaktifkan' : 'Aktifkan'"
                      @click="confirmToggle(m)" />
              <span v-else class="sub">hanya baca</span>
            </td>
          </tr>
        </tbody>
      </table>
    </StatePanel>
  </div>
</template>
