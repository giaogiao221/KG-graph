<script setup lang="ts">
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
</script>

<template>
  <main v-if="!auth.initialized" class="app-loading" role="status" aria-live="polite" aria-busy="true">
    <span class="spinner" aria-hidden="true"></span>
    正在验证登录状态…
  </main>
  <RouterView v-else />
</template>
