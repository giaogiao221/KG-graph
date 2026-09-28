import { render, screen } from '@testing-library/vue'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import App from '../App.vue'
import { createAppRouter } from '../router'
import type { CurrentUser } from '../stores/auth'

const cases: Array<[string, string[], string[], string[]]> = [
  ['viewer', ['records:view'], ['项目与文档', '事实结果', '用量统计'], ['抽取任务', '结果评审', '系统管理']],
  ['reviewer', ['records:view', 'records:review'], ['结果评审'], ['抽取任务', '系统管理']],
  ['operator', ['records:view', 'records:create', 'records:edit'], ['抽取任务'], ['结果评审', '系统管理']],
  ['admin', ['records:view', 'records:create', 'records:edit', 'records:review', 'users:create'], ['系统管理'], []],
]

describe.each(cases)('%s navigation', (role, permissions, shown, hidden) => {
  it('follows backend-returned permissions', async () => {
    sessionStorage.setItem('extraction.accessToken', 'saved')
    const user: CurrentUser = { id: role, username: role, roles: [role], permissions }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify(user), { status: 200 }))))
    const pinia = createPinia(); setActivePinia(pinia)
    const router = createAppRouter(pinia, createMemoryHistory())
    await router.push('/dashboard'); await router.isReady()
    render(App, { global: { plugins: [pinia, router] } })
    expect(screen.getByRole('link', { name: '工作台' })).toBeVisible()
    for (const label of shown) {
      const item = label === '系统管理'
        ? screen.getByRole('button', { name: label })
        : screen.getByRole('link', { name: label })
      expect(item).toBeVisible()
    }
    for (const label of hidden) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: label })).not.toBeInTheDocument()
    }
  })
})
