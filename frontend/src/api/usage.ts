import { apiRequest } from './client'
export interface UsageItem { key: string; calls: number; prompt_tokens: number; completion_tokens: number; cached_tokens: number; total_tokens: number; cost: string; currency: string }
export const getUsage = (query: URLSearchParams, signal?: AbortSignal) => apiRequest<{ items: UsageItem[]; total: number }>(`/api/usage?${query}`, { signal })
