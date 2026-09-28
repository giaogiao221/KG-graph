import type { Pinia } from 'pinia'
import {
  createRouter,
  createWebHistory,
  type RouterHistory,
  type RouteRecordRaw,
} from 'vue-router'
import AppLayout from '../layouts/AppLayout.vue'
import DashboardView from '../views/DashboardView.vue'
import LoginView from '../views/LoginView.vue'
import ProjectsView from '../views/ProjectsView.vue'
import JobCreateView from '../views/JobCreateView.vue'
import JobsView from '../views/JobsView.vue'
import FactsView from '../views/FactsView.vue'
import ReviewsView from '../views/ReviewsView.vue'
import UsageView from '../views/UsageView.vue'
import AdminView from '../views/AdminView.vue'
import PromptTemplatesView from '../views/PromptTemplatesView.vue'
import { useAuthStore } from '../stores/auth'

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: LoginView },
  {
    path: '/', component: AppLayout, meta: { requiresAuth: true }, children: [
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', name: 'dashboard', component: DashboardView },
      { path: 'projects', name: 'projects', component: ProjectsView, meta: { permission: 'records:view' } },
      { path: 'jobs', name: 'jobs', component: JobsView, meta: { permission: 'records:create' } },
      { path: 'jobs/new', name: 'job-create', component: JobCreateView, meta: { permission: 'records:create' } },
      { path: 'facts', name: 'facts', component: FactsView, meta: { permission: 'records:view' } },
      { path: 'reviews', name: 'reviews', component: ReviewsView, meta: { permission: 'records:review' } },
      { path: 'reviews/:batchId', name: 'review-batch', component: ReviewsView, meta: { permission: 'records:review' } },
      { path: 'usage', name: 'usage', component: UsageView, meta: { permission: 'records:view' } },
      { path: 'admin', name: 'admin', component: AdminView, meta: { permission: 'users:create', role: 'admin' } },
      { path: 'admin/prompts', name: 'admin-prompts', component: PromptTemplatesView, meta: { permission: 'users:create', role: 'admin' } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

export function safeInternalRoute(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return '/dashboard'
  try {
    const parsed = new URL(value, window.location.origin)
    if (parsed.origin !== window.location.origin || parsed.pathname === '/login') return '/dashboard'
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return '/dashboard'
  }
}

export function createAppRouter(pinia: Pinia, history: RouterHistory = createWebHistory()) {
  const router = createRouter({ history, routes })
  const auth = useAuthStore(pinia)
  auth.configure(() => {
    if (router.currentRoute.value.name !== 'login') void router.replace('/login')
  })
  router.beforeEach(async (to) => {
    await auth.restore()
    if (to.meta.requiresAuth && !auth.isAuthenticated) {
      return { name: 'login', query: { redirect: safeInternalRoute(to.fullPath) } }
    }
    if (to.name === 'login' && auth.isAuthenticated) return safeInternalRoute(to.query.redirect)
    if (to.meta.permission && !auth.hasPermission(String(to.meta.permission))) return '/dashboard'
    if (to.meta.role && !auth.hasRole(String(to.meta.role))) return '/dashboard'
    return true
  })
  return router
}
