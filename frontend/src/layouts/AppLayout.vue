<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const menuOpen = ref(false)
const adminMenuOpen = ref(route.path.startsWith('/admin'))

const icons: Record<string, string> = {
  dashboard: 'M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z',
  projects: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  jobs: 'M6 4h4v16H6zM14 4h4v16h-4z',
  facts: 'M3 5h18v14H3zM3 10h18M9 5v14M15 5v14',
  reviews: 'M9 11l3 3 8-8M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9',
  usage: 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  admin: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4',
}

const items = computed(() => [
  { to: '/dashboard', label: '工作台', icon: icons.dashboard, show: true },
  { to: '/projects', label: '项目与文档', icon: icons.projects, show: auth.hasPermission('records:view') },
  { to: '/jobs', label: '抽取任务', icon: icons.jobs, show: auth.hasPermission('records:create') },
  { to: '/facts', label: '事实结果', icon: icons.facts, show: auth.hasPermission('records:view') },
  { to: '/reviews', label: '结果评审', icon: icons.reviews, show: auth.hasPermission('records:review') },
  { to: '/usage', label: '用量统计', icon: icons.usage, show: auth.hasPermission('records:view') },
  { to: '/admin?section=users', label: '系统管理', icon: icons.admin, show: auth.hasRole('admin') && auth.hasPermission('users:create'), children: [
    { to: '/admin?section=users', label: '用户管理' },
    { to: '/admin?section=members', label: '项目成员' },
    { to: '/admin?section=models', label: '模型配置' },
    { to: '/admin/prompts', label: '提示词模板' },
  ] },
].filter((item) => item.show))

const pageTitle = computed(() => route.path.startsWith('/admin') ? '系统管理' : items.value.find((item) => route.path.startsWith(item.to))?.label ?? '')

async function logout() {
  auth.logout()
  await router.replace('/login')
}

async function toggleAdminMenu() {
  adminMenuOpen.value = !adminMenuOpen.value
  if (!route.path.startsWith('/admin')) await router.push('/admin?section=users')
}
</script>

<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell">
    <aside class="sidebar" :class="{ open: menuOpen }">
      <div class="brand">
        <span class="brand-mark brand-graph" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 7 17 5M7 8l4 8M13 15l4-8" /><circle cx="6" cy="7" r="2.4" fill="currentColor" /><circle cx="18" cy="5" r="2.4" fill="currentColor" /><circle cx="12" cy="16" r="2.4" fill="currentColor" /></svg></span>
        <span class="brand-name">抽取评审系统</span>
      </div>
      <nav aria-label="主导航">
        <template v-for="item in items" :key="item.to">
          <button v-if="item.children" class="nav-parent" type="button" :aria-expanded="adminMenuOpen" @click="toggleAdminMenu">
            <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="item.icon" /></svg>
            <span>{{ item.label }}</span><span class="nav-chevron" aria-hidden="true">{{ adminMenuOpen ? '⌄' : '›' }}</span>
          </button>
          <RouterLink v-else :to="item.to" @click="menuOpen = false">
            <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="item.icon" /></svg>
            {{ item.label }}
          </RouterLink>
          <div v-if="item.children && adminMenuOpen" class="nav-submenu">
            <RouterLink v-for="child in item.children" :key="child.to" :to="child.to" @click="menuOpen = false">{{ child.label }}</RouterLink>
          </div>
        </template>
      </nav>
      <div class="sidebar-footer">
        <p class="authority-note">页面入口按账号权限显示，最终操作权限仍由服务端校验。</p>
        <div class="user-card">
          <span class="avatar" aria-hidden="true">{{ auth.user?.username?.slice(0, 1).toUpperCase() }}</span>
          <span class="name">{{ auth.user?.username }}</span>
          <button type="button" class="quiet" @click="logout">退出登录</button>
        </div>
      </div>
    </aside>
    <header class="topbar">
      <button class="menu-button" type="button" :aria-label="menuOpen ? '关闭主导航' : '打开主导航'" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen">☰</button>
      <span class="page-title">{{ pageTitle }}</span>
      <span class="spacer"></span>
      <span class="environment">内网环境</span>
    </header>
    <main id="main-content" class="content" tabindex="-1"><RouterView /></main>
  </div>
</template>
