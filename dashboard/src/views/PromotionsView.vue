<script setup lang="ts">
/** UI-007 — promo lifecycle + eligibility preview with the reason code. */
import { ref } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { get, patch, post } from '@/lib/api'
import { count, money, when } from '@/lib/format'
import StatePanel from '@/components/StatePanel.vue'
import { useSession } from '@/stores/session'

const session = useSession()
const confirm = useConfirm()
const toast = useToast()
const qc = useQueryClient()

const { data, isPending, error } = useQuery({
  queryKey: ['promotions'],
  queryFn: ({ signal }) => get<any[]>('/admin/v1/promotions?limit=100', signal),
})

const update = useMutation({
  mutationFn: ({ id, body }: any) => patch(`/admin/v1/promotions/${id}`, body),
  onSuccess: () => {
    toast.add({ severity: 'success', summary: 'Promo diperbarui', life: 3000 })
    qc.invalidateQueries({ queryKey: ['promotions'] })
  },
  onError: (e: Error) =>
    toast.add({ severity: 'error', summary: 'Gagal', detail: e.message, life: 6000 }),
})

const probe = ref({ promotionId: '', customerId: '' })
const check = useMutation({
  mutationFn: () => post<any>('/admin/v1/promotions/eligibility-preview', probe.value),
})

async function setStatus(p: any, status: string) {
  confirm.require({
    header: 'Konfirmasi perubahan promo',
    message: `Ubah ${p.promotionId} menjadi ${status}?`,
    acceptLabel: 'Ya', rejectLabel: 'Batal',
    accept: async () => {
      const fresh = (await get<any[]>('/admin/v1/promotions?limit=500'))
        .find((x: any) => x.promotionId === p.promotionId)
      update.mutate({ id: p.promotionId, body: { status, version: (fresh as any).version ?? 1 } })
    },
  })
}
const quotaLeft = (p: any) => (p.campaignQuota ? p.campaignQuota - p.quotaUsed : null)
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Promo</h2>
      <p>
        Kuota diperiksa saat rekomendasi tetapi tidak dipesan; konsumsi kuota bersifat atomik
        di layanan redemption (PROMO-001).
      </p>
    </div>
  </div>

  <div class="panel">
    <h3>Preview kelayakan</h3>
    <div class="row">
      <label class="field">Promotion ID<InputText v-model="probe.promotionId" /></label>
      <label class="field">Customer ID<InputText v-model="probe.customerId" /></label>
      <Button label="Cek" :disabled="!probe.promotionId || !probe.customerId"
              :loading="check.isPending.value" @click="check.mutate()" />
    </div>
    <div v-if="check.data.value" class="card" style="margin-top:12px">
      <Tag :value="check.data.value.eligible ? 'LAYAK' : 'TIDAK LAYAK'"
           :severity="check.data.value.eligible ? 'success' : 'danger'" />
      <span class="mono" style="margin-left:8px">{{ check.data.value.reasonCode }}</span>
    </div>
    <p v-if="check.error.value" class="state error">{{ check.error.value.message }}</p>
  </div>

  <div class="panel">
    <StatePanel :loading="isPending" :error="error" :empty="!data?.length">
      <table class="plain">
        <thead>
          <tr><th>Promo</th><th>Merchant</th><th>Benefit</th><th>Min belanja</th><th>Periode</th>
            <th>Kuota sisa</th><th>Tier</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="p in data" :key="p.promotionId">
            <td class="mono">{{ p.promotionId }}</td>
            <td class="mono">{{ p.merchantId }}</td>
            <td>{{ p.benefitType }} {{ p.benefitValue }}</td>
            <td>{{ money(p.minSpendMinor) }}</td>
            <td>{{ when(p.startsAt) }}<br />→ {{ when(p.endsAt) }}</td>
            <td>{{ quotaLeft(p) === null ? 'tanpa kuota' : count(quotaLeft(p)) }}</td>
            <td><span v-for="t in p.eligibleCardTiers" :key="t" class="tag">{{ t }}</span></td>
            <td>
              <Tag :value="p.status"
                   :severity="p.status === 'ACTIVE' ? 'success'
                     : p.status === 'EXPIRED' ? 'danger' : 'warn'" />
            </td>
            <td>
              <template v-if="session.can('promotion:write')">
                <Button v-if="p.status === 'ACTIVE'" label="Jeda" text size="small"
                        @click="setStatus(p, 'PAUSED')" />
                <Button v-else-if="p.status === 'PAUSED'" label="Aktifkan" text size="small"
                        @click="setStatus(p, 'ACTIVE')" />
              </template>
              <span v-else class="sub">hanya baca</span>
            </td>
          </tr>
        </tbody>
      </table>
    </StatePanel>
  </div>
</template>
