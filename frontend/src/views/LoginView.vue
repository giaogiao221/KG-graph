<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { safeInternalRoute } from '../router'
import { useAuthStore } from '../stores/auth'

const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

async function submit() {
  error.value = ''
  busy.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    await router.replace(safeInternalRoute(route.query.redirect))
  } catch (reason) {
    error.value = reason instanceof ApiError && reason.status === 401
      ? '用户名或密码不正确'
      : '暂时无法登录，请稍后重试'
  } finally {
    password.value = ''
    busy.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-card" aria-labelledby="login-title">
      <div class="brand login-brand">
        <span class="brand-mark" aria-hidden="true">抽</span>
        <span class="brand-name">抽取评审系统</span>
      </div>
      <p class="eyebrow">单位内网协作平台</p>
      <h1 id="login-title">登录抽取评审系统</h1>
      <p class="muted">使用管理员分配的本地账号登录。</p>
      <form @submit.prevent="submit">
        <label for="username">用户名</label>
        <input id="username" v-model="username" name="username" autocomplete="username" required autofocus />
        <label for="password">密码</label>
        <input id="password" v-model="password" name="password" type="password" autocomplete="current-password" required />
        <p v-if="error" class="message error" role="alert">{{ error }}</p>
        <button class="primary" type="submit" :disabled="busy">
          {{ busy ? '正在登录…' : '登录' }}
        </button>
      </form>
    </section>
  </main>
</template>
