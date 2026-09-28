import { apiRequest } from './client'
export interface Fact { id: string; subject: string; property: string; value: string; unit: string; condition: string; source_type: string; confidence: number | null; review_status: string; evidence_text: string; evidence_truncated: boolean; evidence_hash: string; extraction_source: string; route: string; row_json?: Record<string, string> }
export interface FactPage { items: Fact[]; total: number; offset: number; limit: number }
export const listFacts = (projectId: string, query: URLSearchParams, signal?: AbortSignal) => apiRequest<FactPage>(`/api/projects/${projectId}/facts?${query}`, { signal })
export const getFact = (projectId: string, factId: string, signal?: AbortSignal) => apiRequest<Fact & { row_json: Record<string, string>; truncated_fields: string[] }>(`/api/projects/${projectId}/facts/${factId}`, { signal })
export const deleteFact = (projectId: string, factId: string) => apiRequest<void>(`/api/projects/${projectId}/facts/${factId}`, { method: 'DELETE' })
export const deleteFacts = (projectId: string, factIds: string[]) => apiRequest<void>(`/api/projects/${projectId}/facts`, { method: 'DELETE', body: JSON.stringify({ fact_ids: factIds }) })
