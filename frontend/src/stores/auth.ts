import { defineStore } from 'pinia'
import { apiRequest, configureApiClient } from '../api/client'

const TOKEN_KEY = 'extraction.accessToken'

export interface CurrentUser {
  id: string
  username: string
  roles: string[]
  permissions: string[]
}

interface TokenResponse { access_token: string; token_type: string }

export const useAuthStore = defineStore('auth', {
  state: () => ({
    accessToken: null as string | null,
    user: null as CurrentUser | null,
    sessionGeneration: 0,
    initialized: false,
    restorePromise: null as Promise<void> | null,
  }),
  getters: {
    isAuthenticated: (state) => Boolean(state.accessToken && state.user),
    hasPermission: (state) => (permission: string) =>
      Boolean(state.user?.permissions.includes(permission)),
    hasRole: (state) => (role: string) => Boolean(state.user?.roles.includes(role)),
  },
  actions: {
    configure(onUnauthorized: () => void) {
      configureApiClient(
        () => ({ token: this.accessToken, generation: this.sessionGeneration }),
        (requestGeneration) => {
          if (requestGeneration !== this.sessionGeneration) return
          this.clearSession()
          onUnauthorized()
        },
      )
    },
    acceptSession(token: string, user: CurrentUser) {
      this.sessionGeneration += 1
      this.accessToken = token
      this.user = user
      sessionStorage.setItem(TOKEN_KEY, token)
    },
    clearSession() {
      this.sessionGeneration += 1
      this.accessToken = null
      this.user = null
      sessionStorage.removeItem(TOKEN_KEY)
    },
    async login(username: string, password: string) {
      const token = await apiRequest<TokenResponse>('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      })
      this.sessionGeneration += 1
      const generation = this.sessionGeneration
      this.accessToken = token.access_token
      this.user = null
      sessionStorage.setItem(TOKEN_KEY, token.access_token)
      try {
        const user = await apiRequest<CurrentUser>('/api/auth/me')
        if (generation === this.sessionGeneration) this.user = user
      } catch (error) {
        if (generation === this.sessionGeneration) this.clearSession()
        throw error
      }
    },
    restore(): Promise<void> {
      if (this.initialized) return Promise.resolve()
      if (this.restorePromise) return this.restorePromise
      this.restorePromise = (async () => {
        const token = sessionStorage.getItem(TOKEN_KEY)
        if (!token) return
        this.sessionGeneration += 1
        const generation = this.sessionGeneration
        this.accessToken = token
        this.user = null
        try {
          const user = await apiRequest<CurrentUser>('/api/auth/me')
          if (generation === this.sessionGeneration) this.user = user
        } catch {
          if (generation === this.sessionGeneration) this.clearSession()
        }
      })().finally(() => {
        this.initialized = true
        this.restorePromise = null
      })
      return this.restorePromise
    },
    logout() {
      this.clearSession()
    },
  },
})
