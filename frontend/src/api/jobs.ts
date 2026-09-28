import { apiRequest } from './client'

export interface JobStep { id: string; kind: string; status: string; idempotency_key: string; version: number; attempt_count: number; failure_code: string | null; failure_summary: string | null }
export interface DocumentJob { id: string; document_version_id: string; original_filename: string; document_version_number: number; status: string; version: number; steps: JobStep[] }
export interface Batch { id: string; project_id: string; profile_version_id: string; status: string; version: number; created_at: string; document_jobs: DocumentJob[] }
export interface BatchPage { items: Batch[]; total: number; offset: number; limit: number }
export const createBatch = (projectId: string, profileVersionId: string, documentVersionIds: string[]) => apiRequest<Batch>(`/api/projects/${projectId}/batches`, { method: 'POST', body: JSON.stringify({ profile_version_id: profileVersionId, document_version_ids: documentVersionIds }) })
export const listBatches = (projectId: string, signal?: AbortSignal) => apiRequest<BatchPage>(`/api/projects/${projectId}/batches?offset=0&limit=100`, { signal })
export const getBatch = (projectId: string, batchId: string, signal?: AbortSignal) => apiRequest<Batch>(`/api/projects/${projectId}/batches/${batchId}`, { signal })
export const retryStep = (projectId: string, batchId: string, step: JobStep) => apiRequest<JobStep>(`/api/projects/${projectId}/batches/${batchId}/steps/${step.id}/retry`, { method: 'POST', body: JSON.stringify({ expected_version: step.version }) })
export const cancelBatch = (projectId: string, batchId: string) => apiRequest<Batch>(`/api/projects/${projectId}/batches/${batchId}/cancel`, { method: 'POST' })
export const deleteBatch = (projectId: string, batchId: string) => apiRequest<void>(`/api/projects/${projectId}/batches/${batchId}`, { method: 'DELETE' })

export async function streamBatchProgress(projectId: string, batchId: string, options: { token: string; signal: AbortSignal; onProgress: (batch: Batch) => void }) {
  const response = await fetch(`/api/projects/${projectId}/batches/${batchId}/events`, {
    headers: { Accept: 'text/event-stream', Authorization: `Bearer ${options.token}` },
    signal: options.signal, redirect: 'error',
  })
  if (!response.ok) throw new Error('进度连接暂时不可用')
  if (!response.body) return
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read(); if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const event = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2)
        const data = event.split('\n').find((line) => line.startsWith('data: '))
        if (data) options.onProgress(JSON.parse(data.slice(6)) as Batch)
        boundary = buffer.indexOf('\n\n')
      }
    }
  } finally { reader.releaseLock() }
}
