import { render, screen, waitFor } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import AdminView from '../views/AdminView.vue'
import FactsView from '../views/FactsView.vue'
import JobCreateView from '../views/JobCreateView.vue'
import ProjectsView from '../views/ProjectsView.vue'
import UsageView from '../views/UsageView.vue'
import JobsView from '../views/JobsView.vue'
import ReviewsView from '../views/ReviewsView.vue'
import { useAuthStore } from '../stores/auth'

const project = { id: 'p1', name: '项目一', description: null, created_at: '2026-07-19T00:00:00Z' }
const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))

beforeEach(() => setActivePinia(createPinia()))

async function routerFor(path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/admin', component: AdminView },
      { path: '/reviews', name: 'reviews', component: ReviewsView },
      { path: '/reviews/:batchId', name: 'review-batch', component: ReviewsView },
    ],
  })
  await router.push(path)
  await router.isReady()
  return router
}

it('loads safe model summaries and permits selecting an existing model', async () => {
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url === '/api/admin/models?limit=100') return json([{ id: 'm1', name: '现有模型', provider: 'openai-compatible', endpoint: 'https://models.example/v1', model_name: 'extractor', api_key: '********', allowed_hosts: ['models.example'], allow_private_network: false, allow_insecure_http: false, provider_supports_idempotency: true, is_enabled: true, cipher_version: 1, key_id: 'primary', created_at: '', updated_at: '' }])
    if (url === '/api/projects') return json([])
    if (url.startsWith('/api/admin/users')) return json([])
    return json({})
  }))
  const router = await routerFor('/admin?section=models')
  render(AdminView, { props: { authorized: true }, global: { plugins: [router] } })
  expect(await screen.findByRole('cell', { name: '现有模型' })).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: '编辑' }))
  expect(screen.getByDisplayValue('现有模型')).toBeVisible()
})

it('reuses an existing profile without creating a duplicate', async () => {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url === '/api/projects') return json([project])
    if (url === '/api/models') return json([])
    if (url === '/api/projects/p1/documents') return json([{ id: 'd1', document_id: 'doc1', project_id: 'p1', version_number: 1, original_filename: '论文.pdf', sha256: 'a'.repeat(64), size_bytes: 12, mime_type: 'application/pdf', is_extractable: true, uploader_id: 'u1', created_at: '' }])
    if (url === '/api/projects/p1/profiles') return json([{ id: 'profile1', project_id: 'p1', name: '规则方案', latest_version: { id: 'pv1', profile_id: 'profile1', version_number: 1, snapshot: { preset: 'rule' }, snapshot_sha256: 'x', created_by_id: 'u1', created_at: '' } }])
    if (url === '/api/projects/p1/batches' && init?.method === 'POST') return json({ id: 'b1' }, 201)
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
  render(JobCreateView)
  await screen.findByLabelText('论文.pdf')
  await userEvent.selectOptions(screen.getByLabelText('方案来源'), 'existing')
  await userEvent.selectOptions(screen.getByLabelText('已有抽取方案'), 'pv1')
  await userEvent.click(screen.getByLabelText('论文.pdf'))
  await userEvent.click(screen.getByRole('button', { name: '确认并提交任务' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => url === '/api/projects/p1/batches' && init?.method === 'POST')).toBe(true))
  expect(fetchMock.mock.calls.some(([url, init]) => url === '/api/projects/p1/profiles' && init?.method === 'POST')).toBe(false)
})

it('exposes every supported usage filter in the request', async () => {
  const fetchMock = vi.fn((url: string) => url === '/api/projects' ? json([project]) : json({ items: [], total: 0, offset: 0, limit: 100 }))
  vi.stubGlobal('fetch', fetchMock)
  render(UsageView)
  await screen.findByLabelText('用户编号')
  await userEvent.type(screen.getByLabelText('用户编号'), 'u1')
  await userEvent.type(screen.getByLabelText('批次编号'), 'b1')
  await userEvent.type(screen.getByLabelText('文档编号'), 'd1')
  await userEvent.type(screen.getByLabelText('模型编号'), 'm1')
  await userEvent.type(screen.getByLabelText('用途'), 'extract')
  await userEvent.selectOptions(screen.getByLabelText('调用状态'), 'success')
  await userEvent.click(screen.getByRole('button', { name: '应用筛选' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes('user_id=u1') && String(url).includes('batch_id=b1') && String(url).includes('document_id=d1') && String(url).includes('model_config_id=m1') && String(url).includes('purpose=extract') && String(url).includes('status=success'))).toBe(true))
})

it('hides exports without permission and shows include-unreviewed to authorized users', async () => {
  const fetchMock = vi.fn((url: string) => url === '/api/projects' ? json([project]) : url.includes('/facts?') ? json({ items: [], total: 0, offset: 0, limit: 100 }) : json({ items: [], total: 0, offset: 0, limit: 100 }))
  vi.stubGlobal('fetch', fetchMock)
  const auth = useAuthStore(); auth.user = { id: 'u1', username: 'viewer', roles: ['viewer'], permissions: ['records:view'] }
  const rendered = render(FactsView)
  await screen.findByText('事实结果')
  expect(screen.queryByRole('button', { name: '导出 TSV' })).toBeNull()
  rendered.unmount()
  auth.user = { ...auth.user, permissions: ['records:view', 'exports:create'] }
  render(FactsView)
  expect(await screen.findByLabelText('包含未评审结果')).toBeVisible()
})

it('accepts common office, text and table document formats', () => {
  vi.stubGlobal('fetch', vi.fn(() => json([])))
  const auth = useAuthStore(); auth.user = { id: 'u1', username: 'operator', roles: ['operator'], permissions: ['records:view', 'records:create'] }
  render(ProjectsView)
  const accept = (screen.getByLabelText('上传书籍或论文') as HTMLInputElement).accept
  for (const suffix of ['.pdf', '.docx', '.txt', '.csv', '.xlsx']) expect(accept).toContain(suffix)
})

it('subscribes every non-terminal batch instead of only the first one', async () => {
  const batch = (id: string, status = 'running') => ({ id, project_id: 'p1', profile_version_id: 'pv1', status, version: 1, created_at: '', document_jobs: [] })
  const fetchMock = vi.fn((url: string) => {
    if (url === '/api/projects') return json([project])
    if (url.includes('/batches?')) return json({ items: [batch('b1'), batch('b2')], total: 2, offset: 0, limit: 100 })
    if (url.endsWith('/batches/b1/events')) return Promise.resolve(new Response(`data: ${JSON.stringify(batch('b1', 'completed'))}\n\n`, { status: 200 }))
    if (url.endsWith('/batches/b2/events')) return Promise.resolve(new Response(`data: ${JSON.stringify(batch('b2', 'completed'))}\n\n`, { status: 200 }))
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock); const auth = useAuthStore(); auth.accessToken = 'safe-token'
  render(JobsView, { global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } } })
  await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/events'))).toHaveLength(2))
  await waitFor(() => expect(screen.getAllByText('已完成')).toHaveLength(2))
})

it('fences a late fact detail when the project changes', async () => {
  let resolveDetail!: (value: Response) => void
  const detail = new Promise<Response>((resolve) => { resolveDetail = resolve })
  const fact = { id: 'f1', subject: '旧项目主体', property: '关系', value: '值', unit: '', condition: '', source_type: 'text', confidence: 1, review_status: 'candidate', evidence_text: '证据', evidence_truncated: false, evidence_hash: 'h', extraction_source: 'rule', route: 'text_rule' }
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url === '/api/projects') return json([project, { ...project, id: 'p2', name: '项目二' }])
    if (url.includes('/projects/p1/facts?')) return json({ items: [fact], total: 1, offset: 0, limit: 100 })
    if (url.endsWith('/facts/f1')) return detail
    if (url.includes('/projects/p2/facts?')) return json({ items: [], total: 0, offset: 0, limit: 100 })
    return json({ items: [], total: 0, offset: 0, limit: 100 })
  }))
  const auth = useAuthStore(); auth.user = { id: 'u1', username: 'viewer', roles: ['viewer'], permissions: ['records:view'] }
  render(FactsView); await screen.findByText('旧项目主体'); await userEvent.click(screen.getByRole('button', { name: '完整结果及溯源信息' }))
  await userEvent.selectOptions(screen.getByLabelText('项目'), 'p2'); resolveDetail(new Response(JSON.stringify({ ...fact, row_json: { evidence: '不应显示' }, truncated_fields: [] }), { status: 200 }))
  await waitFor(() => expect(screen.queryByText('不应显示')).toBeNull())
})

it('selects the current facts page and sends one audited batch-delete request', async () => {
  const facts = [
    { id: 'f1', subject: '主体一', property: '关系', value: '值', unit: '', condition: '', source_type: 'text', confidence: 1, review_status: 'candidate', evidence_text: '证据', evidence_truncated: false, evidence_hash: 'h', extraction_source: 'rule', route: 'text_rule' },
    { id: 'f2', subject: '主体二', property: '关系', value: '值', unit: '', condition: '', source_type: 'text', confidence: 1, review_status: 'candidate', evidence_text: '证据', evidence_truncated: false, evidence_hash: 'h', extraction_source: 'rule', route: 'text_rule' },
  ]
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url === '/api/projects') return json([project])
    if (url.includes('/projects/p1/facts?')) return json({ items: facts, total: 2, offset: 0, limit: 50 })
    if (url === '/api/projects/p1/facts' && init?.method === 'DELETE') return new Response(null, { status: 204 })
    return json({ items: [], total: 0, offset: 0, limit: 50 })
  })
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('confirm', vi.fn(() => true))
  const auth = useAuthStore(); auth.user = { id: 'a1', username: 'admin', roles: ['admin'], permissions: ['records:view', 'records:review'] }
  render(FactsView)
  await screen.findByText('主体一')
  await userEvent.click(screen.getByLabelText('全选当前页事实'))
  expect(screen.getByText('已选 2 条')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: '删除所选' }))
  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    '/api/projects/p1/facts',
    expect.objectContaining({ method: 'DELETE', body: JSON.stringify({ fact_ids: ['f1', 'f2'] }) }),
  ))
})

it('restores a review claimed by the current reviewer after refresh', async () => {
  const task = { id: 't1', project_id: 'p1', root_id: 'r1', raw_fact_id: null, batch_id: 'b1', subject: '手工主体', property: '关系', value: '值', reviewer_id: 'u1', lease_expires_at: '2026-07-20T00:00:00Z', lease_version: 1, status: 'claimed', created_at: '' }
  const fetchMock = vi.fn((url: string) => {
    if (url === '/api/projects') return json([project])
    if (url.includes('/review-batches')) return json({ items: [{ batch_id: 'b1', created_at: '2026-07-20T00:00:00Z', preset: 'rule', total: 1, pending: 0, claimed: 1, completed: 0 }], total: 1 })
    if (url.includes('status=available')) return json({ items: [], total: 0, offset: 0, limit: 25 })
    if (url.includes('status=claimed') && url.includes('reviewer_id=u1')) return json({ items: [task], total: 1, offset: 0, limit: 25 })
    if (url.includes('/review-facts/r1/versions')) return json({ items: [{ id: 'v1', root_id: 'r1', version_number: 1, action: 'manual_create', row_json: {}, editable_values: { subject: '手工主体', property: '关系', value: '值', evidence: '证据' } }] })
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock); const auth = useAuthStore(); auth.user = { id: 'u1', username: 'reviewer', roles: ['reviewer'], permissions: ['records:view', 'records:review'] }
  const router = await routerFor('/reviews/b1?project=p1')
  render(ReviewsView, { global: { plugins: [router] } })
  await userEvent.click(await screen.findByRole('button', { name: '我的任务' }))
  await userEvent.click(await screen.findByRole('button', { name: '继续' }))
  expect(await screen.findByDisplayValue('手工主体')).toBeVisible()
  expect(screen.getByRole('button', { name: '放回队列' })).toBeVisible()
})
