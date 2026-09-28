import { apiRequest } from './client'

export interface Routes { text_rule: boolean; text_llm: boolean; table_rule: boolean; table_llm: boolean }
export interface ProfileVersion { id: string; profile_id: string; version_number: number; snapshot: Record<string, unknown>; snapshot_sha256: string; created_by_id: string; created_at: string }
export interface ProfileSummary { id: string; project_id: string; name: string; latest_version: ProfileVersion }
export interface ProfileCreatePayload { name: string; preset: 'rule' | 'llm' | 'hybrid'; model_config_ids?: string[]; concurrency?: number; prompt_version?: string }
export const listProfiles = (projectId: string, signal?: AbortSignal) => apiRequest<ProfileSummary[]>(`/api/projects/${projectId}/profiles`, { signal })
export const createProfile = (projectId: string, payload: ProfileCreatePayload) => apiRequest<ProfileVersion>(`/api/projects/${projectId}/profiles`, { method: 'POST', body: JSON.stringify(payload) })
