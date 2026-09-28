import { apiRequest } from './client'
export const createUser = (username: string, password: string, roles: string[], projectIds: string[]) => apiRequest('/api/admin/users', { method: 'POST', body: JSON.stringify({ username, password, roles, project_ids: projectIds }) })
export interface AdminUser { id: string; username: string; is_disabled: boolean; roles: string[] }
export const listUsers = () => apiRequest<AdminUser[]>('/api/admin/users?offset=0&limit=500')
export const updateUser = (userId: string, payload: Partial<Pick<AdminUser, 'username' | 'is_disabled' | 'roles'>> & { password?: string }) =>
  apiRequest<AdminUser>(`/api/admin/users/${userId}`, { method: 'PATCH', body: JSON.stringify(payload) })
export const disableUser = (userId: string) => apiRequest<void>(`/api/admin/users/${userId}`, { method: 'DELETE' })
