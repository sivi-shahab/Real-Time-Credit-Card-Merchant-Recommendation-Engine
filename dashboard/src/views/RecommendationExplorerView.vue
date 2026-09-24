<script setup lang="ts">
/** UI-006 — candidates, drop reasons, score components, promo terms, stage latency.
 *  AC-007: preview mode writes no impression and does not touch the production cache. */
import { computed, ref } from 'vue'
import { useMutation } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { post } from '@/lib/api'
import { count, money, ms, when } from '@/lib/format'
import StatePanel from '@/components/StatePanel.vue'

const form = ref({ customerId: '', cityCode: '', channel: null as string | null, limit: 10 })

const run = useMutation({
  mutationFn: () => post<any>('/admin/v1/recommendations/preview', {
    customerId: form.value.customerId,
    cityCode: form.value.cityCode || null,
    channel: form.value.channel,
    limit: form.value.limit,
  }),
})

const response = computed(() => run.data.value?.response)
const debug = computed(() => run.data.value?.debug ?? {})
const dropReasons = computed(() => {
  const tally: Record<string, number> = {}
  for (const d of debug.value.dropped ?? []) tally[d.reason] = (tally[d.reason] ?? 0) + 1
  return tally
})
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Recommendation Explorer</h2>
      <p>
        Mode preview: tidak membentuk impression bisnis dan tidak menimpa cache produksi.
        Skor bersifat relevansi relatif, bukan probabilitas klik.
      </p>
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <label class="field">Customer ID<InputText v-model="form.customerId" /></label>
      <label class="field">Kota (opsional)<InputText v-model="form.cityCode" /></label>
      <label class="field">
        Kanal<Select v-model="form.channel" showClear :options="['ONLINE', 'OFFLINE']" />
      </label>
      <label class="field">Limit<InputNumber v-model="form.limit" :min="1" :max="20" /></label>
      <Button label="Jalankan preview" :disabled="!form.customerId || run.isPending.value"
              :loading="run.isPending.value" @click="run.mutate()" />
    </div>
  </div>

  <StatePanel :loading="run.isPending.value" :error="run.error.value">
    <div v-if="response">
      <div class="cards">
        <div class="card"><div class="label">Source</div>
          <div class="value">{{ response.source }}</div></div>
        <div class="card"><div class="label">Kandidat</div>
          <div class="value">{{ count(debug.candidateCount) }}</div>
          <div class="sub">maksimum 200</div></div>
        <div class="card"><div class="label">Model</div>
          <div class="value" style="font-size:15px">{{ response.modelVersion }}</div>
          <div class="sub">ranking {{ response.rankingConfigVersion }}</div></div>
        <div class="card"><div class="label">Feature as-of</div>
          <div class="value" style="font-size:15px">{{ when(response.featureAsOf) }}</div></div>
      </div>

      <div class="panel">
        <h3>Latensi per tahap</h3>
        <div class="row">
          <div v-for="(v, k) in debug.stageLatencyMs" :key="k" class="card" style="min-width:130px">
            <div class="label">{{ k }}</div><div class="value">{{ ms(v as number) }}</div>
          </div>
        </div>
      </div>

      <div class="panel">
        <h3>Hasil ranking</h3>
        <table class="plain">
          <thead>
            <tr><th>#</th><th>Merchant</th><th>Kategori</th><th>Skor</th><th>Alasan</th>
              <th>Promo</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in response.recommendations" :key="r.merchantId">
              <td>{{ r.rank }}</td>
              <td>{{ r.merchantName }}<div class="mono">{{ r.merchantId }}</div></td>
              <td>{{ r.categoryCode }}</td>
              <td class="mono">{{ r.score.toFixed(4) }}</td>
              <td><span v-for="c in r.reasonCodes" :key="c" class="tag">{{ c }}</span></td>
              <td>
                <template v-if="r.promotion">
                  <Tag :value="r.promotion.benefitType" severity="success" />
                  <div class="sub">
                    {{ r.promotion.benefitValue }} · min belanja
                    {{ money(r.promotion.minSpendMinor) }}
                    <br />{{ r.promotion.conditionStatus }}
                  </div>
                </template>
                <span v-else class="sub">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="grid-2">
        <div class="panel">
          <h3>Kandidat dibuang</h3>
          <table class="plain">
            <thead><tr><th>Alasan</th><th>Jumlah</th></tr></thead>
            <tbody>
              <tr v-for="(v, k) in dropReasons" :key="k"><td>{{ k }}</td><td>{{ count(v) }}</td></tr>
            </tbody>
          </table>
        </div>
        <div class="panel">
          <h3>Promo tidak memenuhi syarat</h3>
          <table class="plain">
            <thead><tr><th>Promo</th><th>Alasan</th></tr></thead>
            <tbody>
              <tr v-for="p in (debug.promoRejections ?? []).slice(0, 20)" :key="p.promotionId">
                <td class="mono">{{ p.promotionId }}</td><td>{{ p.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h3>Komponen skor baseline (0,65 minat · 0,20 lokasi · 0,10 rating · 0,05 promo)</h3>
        <table class="plain">
          <thead><tr><th>Merchant</th><th>Skor</th><th>Minat</th><th>Lokasi</th><th>Rating</th>
            <th>Promo</th></tr></thead>
          <tbody>
            <tr v-for="s in (debug.scored ?? []).slice(0, 20)" :key="s.merchantId">
              <td class="mono">{{ s.merchantId }}</td>
              <td class="mono">{{ s.score.toFixed(4) }}</td>
              <td class="mono">{{ s.components.interest.toFixed(3) }}</td>
              <td class="mono">{{ s.components.location.toFixed(3) }}</td>
              <td class="mono">{{ s.components.rating.toFixed(3) }}</td>
              <td class="mono">{{ s.components.promo.toFixed(3) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <h3>Respons JSON</h3>
        <pre class="json">{{ JSON.stringify(response, null, 2) }}</pre>
      </div>
    </div>
    <div v-else class="state">Masukkan customerId lalu jalankan preview.</div>
  </StatePanel>
</template>
