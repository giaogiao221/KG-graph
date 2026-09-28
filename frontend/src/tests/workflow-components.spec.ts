import { fireEvent, render, screen, waitFor } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import FactDetailDrawer from '../components/facts/FactDetailDrawer.vue'
import AdminView from '../views/AdminView.vue'
import UsageView from '../views/UsageView.vue'
import { streamBatchProgress } from '../api/jobs'
import { downloadExport } from '../api/exports'
import { configureApiClient } from '../api/client'
import { createMemoryHistory, createRouter } from 'vue-router'

it('renders evidence as text and exposes all 59 fields without HTML injection', async () => {
  const row = Object.fromEntries(Array.from({ length: 58 }, (_, index) => [`字段${index + 1}`, `值${index + 1}`]))
  row['evidence'] = '<img src=x onerror=alert(1)>  原始空白\n下一行'
  const { container } = render(FactDetailDrawer, { props: { open: true, fact: { id: 'f1', row_json: row } } })
  expect(screen.getByText(/<img src=x/)).toBeVisible()
  expect(container.querySelector('img')).toBeNull()
  expect(screen.getAllByRole('term')).toHaveLength(59)
  await fireEvent.keyDown(document, { key: 'Escape' })
})

it('aborts authenticated progress fetch without putting the token in the URL', async () => {
  const fetchMock = vi.fn((_url: string, init: RequestInit) => Promise.resolve(new Response('')))
  vi.stubGlobal('fetch', fetchMock)
  const controller = new AbortController()
  await streamBatchProgress('p1', 'b1', { token: 'secret', signal: controller.signal, onProgress: vi.fn() })
  expect(fetchMock.mock.calls[0][0]).toBe('/api/projects/p1/batches/b1/events')
  expect(new Headers(fetchMock.mock.calls[0][1].headers).get('Authorization')).toBe('Bearer secret')
  expect(fetchMock.mock.calls[0][0]).not.toContain('secret')
  expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal)
})

it('downloads an owned export with header authentication and no token in the URL', async () => {
  const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
    Promise.resolve(new Response('safe-export', { status: 200 })))
  vi.stubGlobal('fetch', fetchMock)
  configureApiClient(() => ({ token: 'download-secret', generation: 4 }), vi.fn())

  const blob = await downloadExport('p1', 'e1')

  expect(await blob.text()).toBe('safe-export')
  expect(fetchMock.mock.calls[0][0]).toBe('/api/projects/p1/exports/e1/download')
  expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get('Authorization')).toBe('Bearer download-secret')
  expect(fetchMock.mock.calls[0][0]).not.toContain('download-secret')
})

it('keeps currencies in separate usage rows', async () => {
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url === '/api/projects') return Promise.resolve(new Response(JSON.stringify([{ id: 'p1', name: '项目一' }]), { status: 200 }))
    return Promise.resolve(new Response(JSON.stringify({ items: [
      { key: 'm1', calls: 1, prompt_tokens: 10, completion_tokens: 2, cached_tokens: 0, total_tokens: 12, cost: '0.2', currency: 'CNY' },
      { key: 'm1', calls: 1, prompt_tokens: 4, completion_tokens: 1, cached_tokens: 1, total_tokens: 6, cost: '0.1', currency: 'USD' },
    ], total: 2, offset: 0, limit: 50 }), { status: 200 }))
  }))
  render(UsageView)
  expect(await screen.findByText('0.2 CNY')).toBeVisible()
  expect(screen.getByText('0.1 USD')).toBeVisible()
})

it('clears password and API key fields after admin submissions', async () => {
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (!init?.method || init.method === 'GET') {
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))
    }
    return Promise.resolve(new Response(JSON.stringify({ id: 'x' }), { status: 201 }))
  }))
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/admin', component: AdminView }] })
  await router.push('/admin?section=users'); await router.isReady()
  render(AdminView, { props: { authorized: true }, global: { plugins: [router] } })
  await userEvent.click(screen.getByRole('button', { name: '新增用户' }))
  const password = screen.getByLabelText('初始密码') as HTMLInputElement
  await userEvent.type(screen.getByLabelText('用户名'), 'new-user')
  await userEvent.type(password, 'StrongPass1!')
  await userEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(screen.queryByLabelText('初始密码')).toBeNull())
  await userEvent.click(screen.getByRole('button', { name: '新增用户' }))
  expect((screen.getByLabelText('初始密码') as HTMLInputElement).value).toBe('')
  await userEvent.click(screen.getByRole('button', { name: '取消' }))
  await router.push('/admin?section=models')
  await userEvent.click(screen.getByRole('button', { name: '新增配置' }))
  const apiKey = screen.getByLabelText(/API Key/) as HTMLInputElement
  await userEvent.type(screen.getByLabelText(/配置名称/), '模型一')
  await userEvent.type(screen.getByLabelText(/Base URL/), 'https://models.example/v1/chat/completions')
  await userEvent.type(screen.getByLabelText(/允许主机/), 'models.example')
  await userEvent.type(screen.getByLabelText(/模型名称/), 'extractor')
  await userEvent.type(apiKey, 'never-store-me')
  await userEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(screen.queryByLabelText(/API Key/)).toBeNull())
  expect(JSON.stringify(sessionStorage)).not.toContain('never-store-me')
})
