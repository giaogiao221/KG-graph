import { render, screen, waitFor } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import DashboardView from '../views/DashboardView.vue'
import { useAuthStore } from '../stores/auth'

// ECharts uses canvas which jsdom does not implement; stub the module so the
// dashboard can be tested for data flow and structure without a real renderer.
vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(), getDom: vi.fn() })),
}))
vi.mock('echarts/charts', () => ({
  BarChart: {}, LineChart: {}, PictorialBarChart: {}, PieChart: {},
}))
vi.mock('echarts/components', () => ({
  GridComponent: {}, LegendComponent: {}, TitleComponent: {}, TooltipComponent: {},
}))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

const projects = [
  { id: 'p1', name: '项目一', description: null, created_at: '2026-07-19T00:00:00Z' },
  { id: 'p2', name: '项目二', description: null, created_at: '2026-07-19T00:00:00Z' },
]

const stats = {
  jobs_total: 5,
  jobs_by_status: { queued: 1, running: 1, partial_success: 0, completed: 2, retryable_failed: 0, permanent_failed: 1, cancelled: 0 },
  document_jobs_total: 40,
  facts_total: 320,
  facts_by_status: { candidate: 120, candidate_review: 20, approved: 170, rejected: 10 },
  facts_by_route: { rule_text: 140, rule_table: 80, llm_text: 70, llm_table: 30 },
  avg_confidence: 0.87,
  review_tasks_pending: 25,
  review_tasks_completed: 75,
  review_progress_pct: 75.0,
  tokens: [
    { total_tokens: 15000, cost: '0.1200000000', currency: 'CNY' },
  ],
}

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function fetcher() {
  return vi.fn((target: string) => {
    if (target === '/api/projects') return json(projects)
    if (target === '/api/dashboard/stats') return json(stats)
    if (target.startsWith('/api/dashboard/stats?project_id=')) return json(stats)
    return json({})
  })
}

async function renderDashboard() {
  const mocked = fetcher()
  vi.stubGlobal('fetch', mocked)
  const auth = useAuthStore()
  auth.user = { id: 'u1', username: 'operator', roles: ['operator'], permissions: ['records:view', 'records:create'] }
  render(DashboardView)
  await screen.findByText('320')
  return mocked
}

describe('dashboard statistics', () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.unstubAllGlobals() })

  it('renders stat cards with aggregated numbers', async () => {
    await renderDashboard()
    expect(screen.getByText('320')).toBeInTheDocument()          // facts_total
    expect(screen.getByText('75.0%')).toBeInTheDocument()        // review progress
    expect(screen.getByText('5')).toBeInTheDocument()            // jobs_total
    expect(screen.getByText('15,000')).toBeInTheDocument()       // tokens
  })

  it('renders chart containers for each panel', async () => {
    await renderDashboard()
    expect(screen.getByLabelText('评审状态环形图')).toBeInTheDocument()
    expect(screen.getByLabelText('任务状态象形柱图')).toBeInTheDocument()
    expect(screen.getByLabelText('事实来源路线条形图')).toBeInTheDocument()
    expect(screen.getByLabelText('Token 用量与费用图')).toBeInTheDocument()
  })

  it('refetches stats when project filter changes', async () => {
    const mocked = await renderDashboard()
    const select = screen.getByLabelText('统计范围')
    await userEvent.selectOptions(select, 'p2')
    await waitFor(() => {
      expect(mocked.mock.calls.some((c) => String(c[0]).includes('project_id=p2'))).toBe(true)
    })
  })

  it('hides the stats panel without records:view permission', async () => {
    vi.stubGlobal('fetch', fetcher())
    const auth = useAuthStore()
    auth.user = { id: 'u2', username: 'nobody', roles: [], permissions: [] }
    render(DashboardView)
    await waitFor(() => expect(screen.queryByLabelText('评审状态环形图')).toBeNull())
    expect(screen.queryByText('统计范围')).toBeNull()
  })
})
