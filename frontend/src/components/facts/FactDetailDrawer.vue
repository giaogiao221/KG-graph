<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
const props = defineProps<{ open: boolean; fact: { id: string; row_json?: Record<string, string> } | null }>()
const emit = defineEmits<{ close: [] }>()
const panel = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null
function close() { emit('close') }
function keydown(event: KeyboardEvent) { if (event.key === 'Escape' && props.open) close() }
watch(() => props.open, async (open) => {
  if (open) { previousFocus = document.activeElement as HTMLElement; await nextTick(); panel.value?.focus() }
  else previousFocus?.focus()
}, { immediate: true })
document.addEventListener('keydown', keydown)
onBeforeUnmount(() => document.removeEventListener('keydown', keydown))
</script>
<template>
  <div v-if="open && fact" class="drawer-backdrop" @click.self="close">
    <aside ref="panel" class="drawer" role="dialog" aria-modal="true" aria-labelledby="fact-detail-title" tabindex="-1">
      <header class="drawer-head"><h2 id="fact-detail-title">59 列结果详情</h2><button type="button" @click="close">关闭</button></header>
      <dl class="fact-fields">
        <template v-for="(value, key) in fact.row_json || {}" :key="key">
          <dt>{{ key }}</dt><dd class="evidence-text">{{ value }}</dd>
        </template>
      </dl>
    </aside>
  </div>
</template>
