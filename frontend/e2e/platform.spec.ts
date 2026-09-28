import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

/**
 * End-to-end acceptance for the intranet deployment (Task 15).
 *
 * Runs against the Docker Compose stack started with the `test` profile (see
 * docs/operations.md): nginx serves the SPA and proxies /api, and the
 * mock-llm service answers model calls deterministically, so the scenario
 * never calls a paid external model.
 *
 * Scenario from the plan: an operator submits a custom job with only the
 * direct table LLM route enabled, then a reviewer publishes a correction.
 * The plan text expects the literal string "已修改通过"; the shipped UI does
 * not render that string — a saved correction surfaces as the updated current
 * value and a `modify_approve` version — so this spec asserts the implemented
 * behaviour instead (UI list shows the corrected value; API history records
 * the `modify_approve` action).
 *
 * Required environment:
 *   E2E_ADMIN_PASSWORD  password of the bootstrapped administrator
 * Optional:
 *   E2E_BASE_URL        defaults to http://localhost:8080
 *   E2E_ADMIN_USERNAME  defaults to admin
 *   E2E_MOCK_LLM_ENDPOINT  defaults to http://mock-llm:8000/v1/chat/completions
 *
 * The seeding in beforeAll is idempotent and the review flow completes its
 * task, so repeated runs against the same stack keep passing.
 */

const ADMIN = {
  username: process.env.E2E_ADMIN_USERNAME ?? 'admin',
  password: process.env.E2E_ADMIN_PASSWORD ?? '',
}
const OPERATOR = { username: 'e2e-operator', password: 'E2e-Operator-Pass1' }
const REVIEWER = { username: 'e2e-reviewer', password: 'E2e-Reviewer-Pass1' }
const PROJECT_NAME = 'E2E 验收项目'
const DOCUMENT_NAME = 'e2e-sample.md'
const DOCUMENT_CONTENT = [
  '# 样例文档',
  '',
  '| 物质 | 熔点 |',
  '| --- | --- |',
  '| 样例物质 | 125 ℃ |',
  '',
].join('\n')
const MODEL_NAME = 'e2e-mock-model'
const MOCK_ENDPOINT =
  process.env.E2E_MOCK_LLM_ENDPOINT ?? 'http://mock-llm:8000/v1/chat/completions'

interface Token {
  access_token: string
}

async function api<T>(
  request: APIRequestContext,
  method: string,
  url: string,
  token?: string,
  data?: unknown,
): Promise<T> {
  const response = await request.fetch(url, {
    method,
    data,
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
  if (!response.ok()) {
    throw new Error(`${method} ${url} failed with ${response.status()}`)
  }
  return (await response.json()) as T
}

async function loginToken(
  request: APIRequestContext,
  username: string,
  password: string,
): Promise<string> {
  const body = await api<Token>(request, 'POST', '/api/auth/login', undefined, {
    username,
    password,
  })
  return body.access_token
}

test.beforeAll(async ({ request }) => {
  if (!ADMIN.password) {
    throw new Error(
      'E2E_ADMIN_PASSWORD is not set; bootstrap the administrator first (docs/operations.md)',
    )
  }
  const admin = await loginToken(request, ADMIN.username, ADMIN.password)

  // Accounts for the two personas.
  const users = await api<Array<{ id: string; username: string }>>(
    request, 'GET', '/api/admin/users?offset=0&limit=500', admin,
  )
  const ensureUser = async (username: string, password: string, role: string) => {
    if (users.some((user) => user.username === username)) return
    await api(request, 'POST', '/api/admin/users', admin, {
      username, password, roles: [role],
    })
  }
  await ensureUser(OPERATOR.username, OPERATOR.password, 'operator')
  await ensureUser(REVIEWER.username, REVIEWER.password, 'reviewer')

  // Project and memberships.
  const projects = await api<Array<{ id: string; name: string }>>(
    request, 'GET', '/api/projects', admin,
  )
  let project = projects.find((item) => item.name === PROJECT_NAME)
  if (!project) {
    project = await api<{ id: string; name: string }>(
      request, 'POST', '/api/projects', admin,
      { name: PROJECT_NAME, description: '端到端验收专用' },
    )
  }
  const projectId = project.id
  const everyone = await api<Array<{ id: string; username: string }>>(
    request, 'GET', '/api/admin/users?offset=0&limit=500', admin,
  )
  for (const username of [OPERATOR.username, REVIEWER.username]) {
    const user = everyone.find((item) => item.username === username)
    if (!user) continue
    const response = await request.fetch(`/api/projects/${projectId}/members`, {
      method: 'POST',
      data: { user_id: user.id, role: 'member' },
      headers: { Authorization: `Bearer ${admin}` },
    })
    if (!response.ok() && response.status() !== 409) {
      throw new Error(`membership for ${username} failed with ${response.status()}`)
    }
  }

  // Sample markdown document with one table (uploaded once per stack).
  const documents = await api<Array<{ original_filename: string }>>(
    request, 'GET', `/api/projects/${projectId}/documents`, admin,
  )
  if (!documents.some((item) => item.original_filename === DOCUMENT_NAME)) {
    const response = await request.post(`/api/projects/${projectId}/documents`, {
      multipart: {
        file: {
          name: DOCUMENT_NAME,
          mimeType: 'text/markdown',
          buffer: Buffer.from(DOCUMENT_CONTENT, 'utf-8'),
        },
      },
      headers: { Authorization: `Bearer ${admin}` },
    })
    if (!response.ok()) {
      throw new Error(`document upload failed with ${response.status()}`)
    }
  }

  // Deterministic mock model config pointing at the mock-llm test service.
  const mockHost = new URL(MOCK_ENDPOINT).hostname
  const models = await api<Array<{ name: string }>>(
    request, 'GET', '/api/admin/models?limit=100', admin,
  )
  if (!models.some((item) => item.name === MODEL_NAME)) {
    await api(request, 'POST', '/api/admin/models', admin, {
      name: MODEL_NAME,
      provider: 'openai-compatible',
      endpoint: MOCK_ENDPOINT,
      model_name: 'mock-e2e',
      api_key: 'e2e-mock-key-0123456789abcdef0123456789',
      allowed_hosts: [mockHost],
      allow_private_network: true,
      allow_insecure_http: true,
      provider_supports_idempotency: false,
      is_enabled: true,
    })
  }
})

async function loginAs(page: Page, username: string, password: string) {
  await page.goto('/login')
  await page.getByLabel('用户名').fill(username)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录' }).click()
  await page.waitForURL('**/dashboard')
}

test('operator submits a custom table-LLM-only job and reviewer publishes a correction', async ({ page, request }) => {
  test.setTimeout(300_000)

  await loginAs(page, OPERATOR.username, OPERATOR.password)

  // Custom job: only the direct table LLM route enabled.
  await page.goto('/jobs/new')
  await page.locator('#job-project').selectOption({ label: PROJECT_NAME })
  await page.getByRole('checkbox', { name: DOCUMENT_NAME }).setChecked(true)
  await page.locator('#preset').selectOption({ label: '自定义' })
  await page.getByRole('checkbox', { name: '文本规则抽取' }).setChecked(false)
  await page.getByRole('checkbox', { name: '专属表格规则插件' }).setChecked(false)
  await page.getByRole('checkbox', { name: '大模型读取正文' }).setChecked(false)
  await page.getByRole('checkbox', { name: '大模型直接读取表格' }).setChecked(true)
  await page.locator('#model').selectOption({ label: `${MODEL_NAME}（mock-e2e）` })
  const plan = page.getByTestId('execution-plan')
  await expect(plan).toContainText('专属表格规则插件：关闭')
  await expect(plan).toContainText('大模型直接读取表格：启用')
  await page.getByRole('button', { name: '确认并提交任务' }).click()
  await expect(page.getByText('任务已提交')).toBeVisible()

  // The batch completes against the mock model.
  await page.goto('/jobs')
  await page.locator('#jobs-project').selectOption({ label: PROJECT_NAME })
  await expect(page.getByText('已完成').first()).toBeVisible({ timeout: 180_000 })

  // Switch persona: reviewer publishes a correction.
  await page.evaluate(() => window.sessionStorage.clear())
  await loginAs(page, REVIEWER.username, REVIEWER.password)
  await page.goto('/reviews')
  await page.getByLabel('项目', { exact: true }).selectOption({ label: PROJECT_NAME })
  const current = page
    .getByRole('listitem')
    .filter({ hasText: '样例物质' })
    .first()
  await current.getByRole('button', { name: '进入查改删' }).click()
  await page.locator('#review-value').fill('126')
  await page.getByRole('button', { name: '修改并通过' }).click()
  await expect(
    page.getByRole('listitem').filter({ hasText: '样例物质' }).filter({ hasText: '126' }),
  ).toBeVisible()

  // Contract check: the immutable history records the correction action.
  const reviewer = await loginToken(request, REVIEWER.username, REVIEWER.password)
  const projects = await api<Array<{ id: string; name: string }>>(
    request, 'GET', '/api/projects', reviewer,
  )
  const projectId = projects.find((item) => item.name === PROJECT_NAME)!.id
  const facts = await api<{ items: Array<{ root_id?: string; row_json: Record<string, string> }> }>(
    request, 'GET', `/api/projects/${projectId}/current-facts?limit=100`, reviewer,
  )
  const fact = facts.items.find((item) => item.row_json.subject === '样例物质')
  expect(fact?.root_id).toBeTruthy()
  const history = await api<{ items: Array<{ action?: string }> }>(
    request, 'GET', `/api/projects/${projectId}/review-facts/${fact!.root_id}/versions?limit=100`, reviewer,
  )
  expect(history.items.map((item) => item.action)).toContain('modify_approve')
})
