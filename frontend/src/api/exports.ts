import { apiBlob, apiRequest } from './client'
export interface ExportRecord { id: string; format: string; include_unreviewed: boolean; record_count: number; size_bytes: number; sha256: string | null; status: string; created_at: string }
export const listExports = (projectId: string, signal?: AbortSignal) => apiRequest<{ items: ExportRecord[]; total: number; offset: number; limit: number }>(`/api/projects/${projectId}/exports?offset=0&limit=100`, { signal })
export const createExport = (projectId: string, format: 'tsv' | 'csv' | 'json', includeUnreviewed = false, signal?: AbortSignal) => apiRequest<ExportRecord>(`/api/projects/${projectId}/exports`, { method: 'POST', body: JSON.stringify({ format, include_unreviewed: includeUnreviewed, filters: {}, idempotency_key: crypto.randomUUID() }), signal })
export const exportDownloadUrl = (projectId: string, exportId: string) => `/api/projects/${projectId}/exports/${exportId}/download`
export const downloadExport = (projectId: string, exportId: string, signal?: AbortSignal) => apiBlob(exportDownloadUrl(projectId, exportId), { signal })
