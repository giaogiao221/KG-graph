<script setup lang="ts">
defineProps<{
  label: string
  value: string | number
  caption?: string
  tone?: 'default' | 'success' | 'warning' | 'danger' | 'accent'
}>()
</script>

<template>
  <div class="stat-card" :class="`stat-card--${tone ?? 'default'}`">
    <p class="stat-card__label">{{ label }}</p>
    <p class="stat-card__value">{{ value }}</p>
    <p v-if="caption" class="stat-card__caption">{{ caption }}</p>
  </div>
</template>
