<script setup lang="ts">
/** UI-009 — actor, action, resource, time, changes, outcome. Read-only by design. */
import { computed, ref } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import Button from 'primevue/button'
import { get } from '@/lib/api'
import { when } from '@/lib/format'
import StatePanel from '@/components/StatePanel.vue'

const cursor = ref<number | null>(null)
const url = computed(() =>
  `/admin/v1/audit-events?limit=50${cursor.value ? `&cursor=${cursor.value}` : ''}`)
const { data, isPending, error } = useQuery({
  queryKey: ['audit', url],
  queryFn: ({ signal }) => get<any>(url.value, signal),
})
</script>

<template>
  <div class="page-head">
    <div>
      <h2>Audit trail</h2>
      <p>Catatan audit tidak dapat diubah atau dihapus melalui dashboard.</p>
    </div>
  </div>

  <div class="panel">
    <StatePanel :loading="isPending" :error="error" :empty="!data?.items?.length">
      <table class="plain">
        <thead>
          <tr><th>Waktu</th><th>Aktor</th><th>Peran</th><th>Aksi</th><th>Resource</th>
            <th>Hasil</th><th>Perubahan</th></tr>
        </thead>
        <tbody>
          <tr v-for="a in data.items" :key="a.id">
            <td>{{ when(a.occurred_at) }}</td>
            <td>{{ a.actor }}</td>
            <td>{{ a.actor_role }}</td>
            <td class="mono">{{ a.action }}</td>
            <td class="mono">{{ a.resource }}</td>
            <td>{{ a.outcome }}</td>
            <td class="mono">{{ a.changes ? JSON.stringify(a.changes).slice(0, 80) : '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div class="row" style="margin-top:12px">
        <Button label="Lebih lama" size="small" :disabled="!data.nextCursor"
                @click="cursor = data.nextCursor" />
        <Button v-if="cursor" label="Terbaru" text size="small" @click="cursor = null" />
      </div>
    </StatePanel>
  </div>
</template>
