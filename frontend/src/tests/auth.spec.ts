import { fireEvent, render, screen, waitFor } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { apiRequest, configureApiClient } from '../api/client'
import { createAppRouter, safeInternalRoute } from '../router'
import { useAuthStore, type CurrentUser } from '../stores/auth'
import styles from '../styles.css?raw'

// ECharts needs canvas which jsdom does not provide; stub it for auth tests
// that mount the dashboard through the router.
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

const admin: CurrentUser = {
  id: 'a1', username: 'admin', roles: ['admin'],
  permissions: ['records:view', 'records:create', 'records:edit', 'records:review', 'users:create'],
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  }))
}

async function renderAt(path: string, fetcher: typeof fetch) {
  vi.stubGlobal('fetch', fetcher)
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createAppRouter(pinia, createMemoryHistory())
  await router.push(path)
  await router.isReady()
  const rendered = render(App, { global: { plugins: [pinia, router] } })
  return { ...rendered, router, auth: useAuthStore(pinia) }
}

describe('authentication', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('shows a non-technical status while session restoration is pending', () => {
    const pinia = createPinia(); setActivePinia(pinia)
    render(App, {
      global: { plugins: [pinia], stubs: { RouterView: true } },
    })
    expect(screen.getByRole('status')).toHaveTextContent('正在验证登录状态')
  })

  it('logs in and redirects to the intended protected page', async () => {
    const fetcher = vi.fn()
      .mockImplementationOnce(() => response({ access_token: 'token', token_type: 'bearer' }))
      .mockImplementationOnce(() => response(admin))
    const { router } = await renderAt('/login?redirect=/projects', fetcher)
    await userEvent.type(screen.getByLabelText('用户名'), 'admin')
    await userEvent.type(screen.getByLabelText('密码'), 'StrongPass1!')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))
    await waitFor(() => expect(router.currentRoute.value.path).toBe('/projects'))
    expect(sessionStorage.getItem('extraction.accessToken')).toBe('token')
  })

  it('shows a concise error for invalid credentials and clears the password', async () => {
    const fetcher = vi.fn(() => response({ detail: 'invalid username or password' }, 401))
    await renderAt('/login', fetcher)
    await userEvent.type(screen.getByLabelText('用户名'), 'admin')
    const password = screen.getByLabelText('密码') as HTMLInputElement
    await userEvent.type(password, 'wrong')
    await userEvent.click(screen.getByRole('button', { name: '登录' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('用户名或密码不正确')
    expect(password.value).toBe('')
    expect(JSON.stringify(useAuthStore().$state)).not.toContain('wrong')
  })

  it('restores the session before rendering a protected route', async () => {
    sessionStorage.setItem('extraction.accessToken', 'saved')
    let resolveMe!: (value: Response) => void
    const fetcher = vi.fn(() => new Promise<Response>((resolve) => { resolveMe = resolve }))
    const pending = renderAt('/reviews', fetcher)
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('结果评审')).not.toBeInTheDocument()
    resolveMe(new Response(JSON.stringify(admin), { status: 200 }))
    const { router } = await pending
    expect(router.currentRoute.value.path).toBe('/reviews')
  })

  it('clears an invalid restored session before showing a protected route', async () => {
    sessionStorage.setItem('extraction.accessToken', 'expired')
    const { router } = await renderAt('/facts', vi.fn(() => response({}, 401)))
    expect(router.currentRoute.value.path).toBe('/login')
    expect(sessionStorage.getItem('extraction.accessToken')).toBeNull()
    expect(screen.getByRole('heading', { name: '登录抽取评审系统' })).toBeVisible()
  })

  it('handles concurrent unauthorized responses once', async () => {
    const pinia = createPinia(); setActivePinia(pinia)
    const auth = useAuthStore(pinia)
    const onUnauthorized = vi.fn()
    auth.configure(onUnauthorized)
    auth.acceptSession('token', admin)
    vi.stubGlobal('fetch', vi.fn(() => response({}, 401)))
    await Promise.allSettled([apiRequest('/api/a'), apiRequest('/api/b')])
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    expect(auth.accessToken).toBeNull()
  })

  it('handles the same token again when it belongs to a new session generation', async () => {
    const pinia = createPinia(); setActivePinia(pinia)
    const auth = useAuthStore(pinia)
    const onUnauthorized = vi.fn()
    auth.configure(onUnauthorized)
    vi.stubGlobal('fetch', vi.fn(() => response({}, 401)))
    auth.acceptSession('same-token', admin)
    await expect(apiRequest('/api/first')).rejects.toMatchObject({ status: 401 })
    auth.acceptSession('same-token', admin)
    await expect(apiRequest('/api/second')).rejects.toMatchObject({ status: 401 })
    expect(onUnauthorized).toHaveBeenCalledTimes(2)
  })

  it('does not let a delayed unauthorized response clear a newer session', async () => {
    const pinia = createPinia(); setActivePinia(pinia)
    const auth = useAuthStore(pinia)
    const onUnauthorized = vi.fn()
    auth.configure(onUnauthorized)
    auth.acceptSession('same-token', admin)
    let resolveOld!: (value: Response) => void
    const fetcher = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveOld = resolve }))
      .mockImplementationOnce(() => response({}, 401))
    vi.stubGlobal('fetch', fetcher)

    const oldRequest = apiRequest('/api/old')
    auth.acceptSession('same-token', { ...admin, username: 'new-admin' })
    resolveOld(new Response('{}', { status: 401 }))
    await expect(oldRequest).rejects.toMatchObject({ status: 401 })
    expect(auth.user?.username).toBe('new-admin')
    expect(onUnauthorized).not.toHaveBeenCalled()

    await expect(apiRequest('/api/new')).rejects.toMatchObject({ status: 401 })
    expect(auth.accessToken).toBeNull()
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('rejects absolute and cross-origin targets without exposing the token', async () => {
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    configureApiClient(() => ({ token: 'secret-token', generation: 1 }), vi.fn())
    await expect(apiRequest('https://other.example/api/data')).rejects.toThrow('仅允许访问系统内部接口')
    await expect(apiRequest('//other.example/api/data')).rejects.toThrow('仅允许访问系统内部接口')
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('forces redirect errors so an API response cannot forward credentials', async () => {
    const fetcher = vi.fn(() => response({ ok: true }))
    vi.stubGlobal('fetch', fetcher)
    configureApiClient(() => ({ token: 'secret-token', generation: 1 }), vi.fn())

    await apiRequest('/api/data', { redirect: 'follow' })

    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher).toHaveBeenCalledWith('/api/data', expect.objectContaining({
      redirect: 'error',
    }))
  })

  it.each([
    ['https://evil.example', '/dashboard'],
    ['//evil.example', '/dashboard'],
    ['/login', '/dashboard'],
    ['/projects?tab=mine', '/projects?tab=mine'],
  ])('sanitizes an intended route of %s', (target, expected) => {
    expect(safeInternalRoute(target)).toBe(expected)
  })

  it('logs out and returns to login', async () => {
    sessionStorage.setItem('extraction.accessToken', 'saved')
    const { router } = await renderAt('/dashboard', vi.fn(() => response(admin)))
    await userEvent.click(screen.getByRole('button', { name: '退出登录' }))
    expect(router.currentRoute.value.path).toBe('/login')
    expect(sessionStorage.getItem('extraction.accessToken')).toBeNull()
  })

  it('uses semantic landmarks and labelled navigation', async () => {
    sessionStorage.setItem('extraction.accessToken', 'saved')
    await renderAt('/dashboard', vi.fn(() => response(admin)))
    expect(screen.getByRole('banner')).toBeVisible()
    expect(screen.getByRole('navigation', { name: '主导航' })).toBeVisible()
    expect(screen.getByRole('main')).toBeVisible()
    expect(screen.getByRole('link', { name: '跳到主要内容' })).toHaveAttribute('href', '#main-content')
    await fireEvent.keyDown(document.body, { key: 'Tab' })
  })

  it('keeps a closed mobile sidebar out of keyboard navigation until opened', async () => {
    sessionStorage.setItem('extraction.accessToken', 'saved')
    const { container } = await renderAt('/dashboard', vi.fn(() => response(admin)))
    const sidebar = container.querySelector('aside.sidebar')
    const toggle = screen.getByRole('button', { name: '打开主导航', hidden: true })
    expect(sidebar).not.toHaveClass('open')
    expect(styles).toContain('visibility: hidden')
    expect(styles).toContain('pointer-events: none')

    await fireEvent.click(toggle)

    expect(sidebar).toHaveClass('open')
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveAttribute('aria-label', '关闭主导航')
    expect(styles).toContain('visibility: visible')
    expect(styles).toContain('pointer-events: auto')
  })
})
