import { fireEvent, render, screen, waitFor } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import JobCreateView from '../views/JobCreateView.vue'

const project = { id: 'p1', name: '项目一', description: null, created_at: '2026-07-19T00:00:00Z' }
const document = {
  id: 'd1', document_id: 'doc1', project_id: 'p1', version_number: 1,
  original_filename: '论文.md', sha256: 'a'.repeat(64), size_bytes: 12, mime_type: 'text/markdown',
  is_extractable: true, uploader_id: 'u1', created_at: '2026-07-19T00:00:00Z',
}
const model = { id: 'm1', name: '单位模型', provider: 'openai-compatible', model_name: 'qwen-plus' }

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function fetcher() {
  return vi.fn((target: string, init?: RequestInit) => {
    if (target === '/api/projects') return json([project])
    if (target === '/api/projects/p1/profiles' && init?.method === 'POST') return json({ id: 'pv1' }, 201)
    if (target === '/api/projects/p1/batches' && init?.method === 'POST') return json({ id: 'b1' }, 201)
    if (target === '/api/projects/p1/documents') return json([document])
    if (target === '/api/projects/p1/profiles') return json([])
    if (target === '/api/models') return json([model])
    return json({})
  })
}

async function renderWizard() {
  const mocked = fetcher()
  vi.stubGlobal('fetch', mocked)
  render(JobCreateView)
  await screen.findByLabelText('论文.md')
  return mocked
}

describe('extraction wizard', () => {
  it('shows the real isolated engine for each extraction mode', async () => {
    await renderWizard()
    const method = screen.getByLabelText('抽取方式')
    const plan = screen.getByTestId('execution-plan')

    expect(plan).toHaveTextContent('本地抽取规则')
    expect(plan).not.toHaveTextContent('大模型混合抽取')

    await userEvent.selectOptions(method, 'hybrid')
    expect(plan).toHaveTextContent('本地抽取规则')
    expect(plan).toHaveTextContent('大模型混合抽取')

    await userEvent.selectOptions(method, 'llm')
    expect(plan).toHaveTextContent('平台纯大模型正文抽取')
    expect(plan).toHaveTextContent('平台纯大模型表格抽取')
    expect(plan).not.toHaveTextContent('本地抽取规则')
  })

  it('requires a model only for pure-LLM and hybrid modes', async () => {
    await renderWizard()
    await userEvent.click(screen.getByLabelText('论文.md'))
    const submit = screen.getByRole('button', { name: '确认并提交任务' })
    expect(submit).toBeEnabled()

    await userEvent.selectOptions(screen.getByLabelText('抽取方式'), 'llm')
    expect(screen.getByRole('alert')).toHaveTextContent('请选择已启用的模型配置')
    expect(submit).toBeDisabled()

    await userEvent.selectOptions(screen.getByLabelText('模型配置'), 'm1')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(submit).toBeEnabled()

    await userEvent.selectOptions(screen.getByLabelText('抽取方式'), 'rule')
    await userEvent.selectOptions(screen.getByLabelText('模型配置'), '')
    expect(submit).toBeEnabled()
  })

  it.each([
    ['rule', []],
    ['llm', ['m1']],
    ['hybrid', ['m1']],
  ] as const)('submits preset %s with the correct model configuration', async (preset, modelConfigIds) => {
    const mocked = await renderWizard()
    await userEvent.selectOptions(screen.getByLabelText('抽取方式'), preset)
    if (modelConfigIds.length) await userEvent.selectOptions(screen.getByLabelText('模型配置'), 'm1')
    await userEvent.click(screen.getByLabelText('论文.md'))

    const button = screen.getByRole('button', { name: '确认并提交任务' })
    await Promise.all([fireEvent.click(button), fireEvent.click(button)])

    await waitFor(() => expect(mocked.mock.calls.filter(([url]) => url === '/api/projects/p1/batches')).toHaveLength(1))
    const profileCall = mocked.mock.calls.find(([url, init]) => url === '/api/projects/p1/profiles' && init?.method === 'POST')
    expect(JSON.parse(String(profileCall?.[1]?.body))).toMatchObject({
      preset,
      model_config_ids: modelConfigIds,
    })
  })
})
