<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { getDashboardStats, type DashboardStats } from '../api/dashboard'
import { listProjects, type Project } from '../api/projects'
import StatCard from '../components/dashboard/StatCard.vue'
import StatsCharts from '../components/dashboard/StatsCharts.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()

const canViewStats = computed(() => auth.hasPermission('records:view'))

const cards = computed(() => [
  { to: '/projects', label: '项目与文档', icon: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z', desc: '管理项目空间，上传书籍、论文等待抽取文档。', show: auth.hasPermission('records:view') },
  { to: '/jobs', label: '抽取任务', icon: 'M6 4h4v16H6zM14 4h4v16h-4z', desc: '创建抽取批次，实时跟踪每个文档的处理进度。', show: auth.hasPermission('records:create') },
  { to: '/facts', label: '事实结果', icon: 'M3 5h18v14H3zM3 10h18M9 5v14M15 5v14', desc: '浏览抽取出的结构化事实，查看证据并导出。', show: auth.hasPermission('records:view') },
  { to: '/reviews', label: '结果评审', icon: 'M9 11l3 3 8-8M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9', desc: '领取评审任务，对抽取结果进行通过、修改或驳回。', show: auth.hasPermission('records:review') },
  { to: '/usage', label: '用量统计', icon: 'M4 20V10M10 20V4M16 20v-7M22 20H2', desc: '按模型、项目或批次统计调用量与成本。', show: auth.hasPermission('records:view') },
  { to: '/admin', label: '系统管理', icon: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4', desc: '维护本地账号、项目成员、模型配置与价格。', show: auth.hasRole('admin') && auth.hasPermission('users:create') },
].filter((card) => card.show))

const projects = ref<Project[]>([])
const selectedProjectId = ref('')
const stats = ref<DashboardStats | null>(null)
const loading = ref(false)
const loadError = ref('')

let abort: AbortController | null = null

async function loadStats() {
  if (!canViewStats.value) return
  abort?.abort()
  abort = new AbortController()
  loading.value = true
  loadError.value = ''
  try {
    stats.value = await getDashboardStats(selectedProjectId.value || undefined, abort.signal)
  } catch (error) {
    if ((error as Error).name !== 'AbortError') {
      loadError.value = '统计数据加载失败'
    }
  } finally {
    loading.value = false
  }
}

const tokenTotal = computed(() =>
  stats.value?.tokens?.reduce((sum, item) => sum + item.total_tokens, 0) ?? 0
)

const reviewCaption = computed(() =>
  stats.value
    ? `已完成 ${stats.value.review_tasks_completed} · 待评审 ${stats.value.review_tasks_pending}`
    : undefined
)

const jobsCaption = computed(() =>
  stats.value
    ? `共 ${stats.value.document_jobs_total} 个文档任务`
    : undefined
)

const tokenCaption = computed(() => {
  const parts = stats.value?.tokens?.map((t) => `${t.cost} ${t.currency}`) ?? []
  return parts.length ? parts.join(' · ') : '暂无模型调用'
})

onMounted(async () => {
  if (!canViewStats.value) return
  try {
    projects.value = await listProjects()
  } catch {
    projects.value = []
  }
  await loadStats()
})

onBeforeUnmount(() => abort?.abort())
</script>

<template>
  <section aria-labelledby="dashboard-title">
    <p class="eyebrow">工作概览</p>
    <h1 id="dashboard-title">工作台</h1>
    <div class="hero">
      <h2>欢迎使用抽取评审系统</h2>
      <p>在这里完成从文档上传、结构化抽取到多人评审的完整流程。可见入口由您的账号权限决定。</p>
    </div>

    <div v-if="canViewStats" class="stats-panel" aria-label="信息统计">
      <div class="stats-panel__toolbar">
        <label for="stats-project">统计范围</label>
        <select id="stats-project" v-model="selectedProjectId" @change="loadStats">
          <option value="">全部可见项目</option>
          <option v-for="project in projects" :key="project.id" :value="project.id">
            {{ project.name }}
          </option>
        </select>
        <button type="button" :disabled="loading" @click="loadStats">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>

      <p v-if="loadError" role="alert" class="message error">{{ loadError }}</p>

      <template v-if="stats">
        <div class="stat-grid">
          <StatCard label="事实总数" :value="stats.facts_total" caption="跨正文与表格路线" tone="accent" />
          <StatCard
            label="评审进度"
            :value="`${(stats.review_progress_pct ?? 0).toFixed(1)}%`"
            :caption="reviewCaption"
            tone="success"
          />
          <StatCard label="抽取批次" :value="stats.jobs_total" :caption="jobsCaption" />
          <StatCard label="Token 消耗" :value="tokenTotal.toLocaleString()" :caption="tokenCaption" tone="warning" />
        </div>

        <StatsCharts :stats="stats" />
      </template>
    </div>

    <div class="quick-grid">
      <RouterLink v-for="card in cards" :key="card.to" class="quick-card" :to="card.to" :aria-label="`${card.label}：${card.desc}`">
        <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="card.icon" /></svg>
        <div><strong>{{ card.label }}</strong><span>{{ card.desc }}</span></div>
      </RouterLink>
    </div>
  </section>
</template>
