import { apiRequest } from './client'
export interface EnabledModel { id: string; name: string; provider: string; model_name: string }
export interface AdminModel extends EnabledModel {
  endpoint: string; api_key: '********'; allowed_hosts: string[]
  allow_private_network: boolean; allow_insecure_http: boolean
  provider_supports_idempotency: boolean; is_enabled: boolean
  cipher_version: number; key_id: string; created_at: string; updated_at: string
}
export const listEnabledModels = (signal?: AbortSignal) => apiRequest<EnabledModel[]>('/api/models', { signal })
export const listAdminModels = (signal?: AbortSignal) => apiRequest<AdminModel[]>('/api/admin/models?limit=100', { signal })
export const createModel = (payload: Record<string, unknown>) => apiRequest<AdminModel>('/api/admin/models', { method: 'POST', body: JSON.stringify(payload) })
export const updateModel = (modelId: string, payload: Record<string, unknown>) => apiRequest<AdminModel>(`/api/admin/models/${modelId}`, { method: 'PATCH', body: JSON.stringify(payload) })
export const deleteModel = (modelId: string) => apiRequest<void>(`/api/admin/models/${modelId}`, { method: 'DELETE' })
export const rotateModelSecret = (modelId: string, apiKey: string) => apiRequest<AdminModel>(`/api/admin/models/${modelId}/secret`, { method: 'POST', body: JSON.stringify({ api_key: apiKey, key_id: 'primary' }) })
export const addPrice = (modelId: string, payload: Record<string, unknown>) => apiRequest(`/api/admin/models/${modelId}/prices`, { method: 'POST', body: JSON.stringify(payload) })
