import { apiRequest } from './client'

export interface JobStatusCounts {
  queued: number
  running: number
  partial_success: number
  completed: number
  retryable_failed: number
  permanent_failed: number
  cancelled: number
}

export interface FactStatusCounts {
  candidate: number
  candidate_review: number
  approved: number
  rejected: number
}

export interface TokenSummary {
  total_tokens: number
  cost: string
  currency: string
}

export interface DashboardStats {
  jobs_total: number
  jobs_by_status: JobStatusCounts
  document_jobs_total: number
  facts_total: number
  facts_by_status: FactStatusCounts
  facts_by_route: Record<string, number>
  avg_confidence: number | null
  review_tasks_pending: number
  review_tasks_completed: number
  review_progress_pct: number
  tokens: TokenSummary[]
}

export const getDashboardStats = (projectId?: string, signal?: AbortSignal) => {
  const query = projectId ? `?project_id=${projectId}` : ''
  return apiRequest<DashboardStats>(`/api/dashboard/stats${query}`, { signal })
}
