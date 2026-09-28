import { apiRequest } from './client'

export interface PromptTemplate {
  id: string
  name: string
  description: string
  text_prompt: string
  table_prompt: string
  is_enabled: boolean
  created_at: string
  updated_at: string
}

export type PromptTemplatePayload = Pick<PromptTemplate, 'name' | 'description' | 'text_prompt' | 'table_prompt' | 'is_enabled'>
export const listEnabledPrompts = () => apiRequest<PromptTemplate[]>('/api/prompts')
export const listAdminPrompts = () => apiRequest<PromptTemplate[]>('/api/admin/prompts?offset=0&limit=500')
export const createPrompt = (payload: PromptTemplatePayload) => apiRequest<PromptTemplate>('/api/admin/prompts', { method: 'POST', body: JSON.stringify(payload) })
export const updatePrompt = (id: string, payload: Partial<PromptTemplatePayload>) => apiRequest<PromptTemplate>(`/api/admin/prompts/${id}`, { method: 'PATCH', body: JSON.stringify(payload) })
export const deletePrompt = (id: string) => apiRequest<void>(`/api/admin/prompts/${id}`, { method: 'DELETE' })
