export class ApiError extends Error {
  constructor(public readonly status: number, message = '请求未能完成') {
    super(message)
    this.name = 'ApiError'
  }
}

export interface SessionSnapshot {
  token: string | null
  generation: number
}

let sessionProvider: () => SessionSnapshot = () => ({ token: null, generation: 0 })
let unauthorizedHandler: (requestGeneration: number) => void = () => undefined
let unauthorizedGenerations = new Set<number>()

export function configureApiClient(
  getSession: () => SessionSnapshot,
  onUnauthorized: (requestGeneration: number) => void,
) {
  sessionProvider = getSession
  unauthorizedHandler = onUnauthorized
  unauthorizedGenerations = new Set<number>()
}

function assertInternalApiTarget(target: string) {
  if (!target.startsWith('/api/') || target.startsWith('//') || target.includes('\\')) {
    throw new Error('仅允许访问系统内部接口')
  }
  const parsed = new URL(target, window.location.origin)
  if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith('/api/')) {
    throw new Error('仅允许访问系统内部接口')
  }
}

export async function apiRequest<T>(target: string, init: RequestInit = {}): Promise<T> {
  assertInternalApiTarget(target)
  const { token, generation } = sessionProvider()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const response = await fetch(target, { ...init, headers, redirect: 'error' })
  if (response.status === 401) {
    if (token && !unauthorizedGenerations.has(generation)) {
      unauthorizedGenerations.add(generation)
      unauthorizedHandler(generation)
    }
    throw new ApiError(401, '登录状态已失效')
  }
  if (!response.ok) {
    throw new ApiError(response.status, '请求未能完成')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export async function apiBlob(target: string, init: RequestInit = {}): Promise<Blob> {
  assertInternalApiTarget(target)
  const { token, generation } = sessionProvider()
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(target, { ...init, headers, redirect: 'error' })
  if (response.status === 401) {
    if (token && !unauthorizedGenerations.has(generation)) {
      unauthorizedGenerations.add(generation); unauthorizedHandler(generation)
    }
    throw new ApiError(401, '登录状态已失效')
  }
  if (!response.ok) throw new ApiError(response.status)
  return response.blob()
}
