<script setup lang="ts">
/** SDD 13.3 — every chart ships a textual summary for non-visual access. */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ title: string; data: Record<string, number>; unit?: string }>()

const option = computed(() => ({
  grid: { left: 48, right: 12, top: 16, bottom: 48 },
  tooltip: { trigger: 'axis' },
  xAxis: { type: 'category', data: Object.keys(props.data), axisLabel: { rotate: 40 } },
  yAxis: { type: 'value' },
  series: [{ type: 'bar', data: Object.values(props.data), itemStyle: { color: '#2b6cf6' } }],
}))

const summary = computed(() => {
  const entries = Object.entries(props.data)
  if (!entries.length) return `${props.title}: tidak ada data.`
  const sorted = [...entries].sort((a, b) => b[1] - a[1])
  const total = entries.reduce((acc, [, v]) => acc + v, 0)
  return `${props.title}: total ${total} ${props.unit ?? ''}. Tertinggi ${sorted[0][0]} `
    + `(${sorted[0][1]}), terendah ${sorted[sorted.length - 1][0]} `
    + `(${sorted[sorted.length - 1][1]}).`
})
</script>

<template>
  <figure style="margin:0">
    <figcaption style="font-size:13px;color:#65708a;margin-bottom:6px">{{ title }}</figcaption>
    <VChart :option="option" style="height:240px" autoresize role="img" :aria-label="summary" />
    <p class="visually-hidden">{{ summary }}</p>
  </figure>
</template>
