import { apiRequest } from './client'

export interface Project { id: string; name: string; description: string | null; created_at: string }
export interface ProjectMember { id: string; project_id: string; user_id: string; username: string; role: 'owner' | 'member' }
export interface DocumentVersion {
  id: string; document_id: string; project_id: string; version_number: number
  original_filename: string; sha256: string; size_bytes: number; mime_type: string
  is_extractable: boolean; uploader_id: string; created_at: string
}

export const listProjects = (signal?: AbortSignal) => apiRequest<Project[]>('/api/projects', { signal })
export const createProject = (name: string, description: string) => apiRequest<Project>('/api/projects', {
  method: 'POST', body: JSON.stringify({ name, description: description || null }),
})
export const listDocuments = (projectId: string, signal?: AbortSignal) =>
  apiRequest<DocumentVersion[]>(`/api/projects/${projectId}/documents`, { signal })
export const listDocumentVersions = (projectId: string, documentId: string, signal?: AbortSignal) =>
  apiRequest<DocumentVersion[]>(`/api/projects/${projectId}/documents/${documentId}/versions`, { signal })
export const uploadDocument = (projectId: string, file: File) => {
  const body = new FormData(); body.append('file', file)
  return apiRequest<DocumentVersion>(`/api/projects/${projectId}/documents`, { method: 'POST', body })
}
export const addProjectMember = (projectId: string, userId: string, role: 'owner' | 'member') =>
  apiRequest(`/api/projects/${projectId}/members`, { method: 'POST', body: JSON.stringify({ user_id: userId, role }) })
export const listProjectMembers = (projectId: string) => apiRequest<ProjectMember[]>(`/api/projects/${projectId}/members`)
export const updateProjectMember = (projectId: string, membershipId: string, role: 'owner' | 'member') =>
  apiRequest<ProjectMember>(`/api/projects/${projectId}/members/${membershipId}`, { method: 'PATCH', body: JSON.stringify({ role }) })
export const removeProjectMember = (projectId: string, membershipId: string) =>
  apiRequest<void>(`/api/projects/${projectId}/members/${membershipId}`, { method: 'DELETE' })
