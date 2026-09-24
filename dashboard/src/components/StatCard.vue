<script setup lang="ts">
/** UI-001 — a metric with no value reads "tidak tersedia", never 0. */
defineProps<{ label: string; value: string; sub?: string; unit?: string }>()
</script>

<template>
  <div class="card">
    <div class="label">{{ label }}</div>
    <div class="value">{{ value }}<span v-if="unit" class="unit"> {{ unit }}</span></div>
    <div v-if="sub" class="sub">{{ sub }}</div>
  </div>
</template>

<style scoped>.unit { font-size: 13px; color: #8a93a8; font-weight: 400; }</style>
