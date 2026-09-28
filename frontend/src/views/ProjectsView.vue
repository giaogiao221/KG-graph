<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createProject, listDocuments, listDocumentVersions, listProjects, uploadDocument, type DocumentVersion, type Project } from '../api/projects'
import { useAuthStore } from '../stores/auth'
const auth = useAuthStore(); const canManageDocuments = auth.hasPermission('records:create')
const projects = ref<Project[]>([]); const projectId = ref(''); const documents = ref<DocumentVersion[]>([]); const versions = ref<Record<string, DocumentVersion[]>>({})
const name = ref(''); const description = ref(''); const busy = ref(false); const error = ref(''); let controller: AbortController | null = null; let generation = 0
async function loadProjects() { projects.value = await listProjects(); if (!projectId.value) projectId.value = projects.value[0]?.id || '' }
watch(projectId, async (id) => { controller?.abort(); controller = new AbortController(); const current = ++generation; if (!id) return
  try { const values = await listDocuments(id, controller.signal); if (current === generation) documents.value = values } catch (reason) { if (!(reason instanceof DOMException && reason.name === 'AbortError')) error.value = '文档列表暂时无法载入' } })
onMounted(() => loadProjects().catch(() => { error.value = '项目列表暂时无法载入' })); onBeforeUnmount(() => { generation++; controller?.abort() })
async function addProject() { if (!name.value.trim()) return; busy.value = true; try { const value = await createProject(name.value.trim(), description.value.trim()); name.value = ''; description.value = ''; await loadProjects(); projectId.value = value.id } catch { error.value = '项目创建失败' } finally { busy.value = false } }
async function upload(event: Event) { const input = event.target as HTMLInputElement; const file = input.files?.[0]; if (!file || !projectId.value) return; busy.value = true; error.value = ''
  try { await uploadDocument(projectId.value, file); documents.value = await listDocuments(projectId.value) } catch (reason: unknown) { error.value = reason instanceof Error && 'status' in reason && reason.status === 413 ? '文件超过系统允许的大小' : '文件类型或内容无法上传，请检查后重试' } finally { input.value = ''; busy.value = false } }
async function showVersions(document: DocumentVersion) { versions.value[document.document_id] = await listDocumentVersions(projectId.value, document.document_id) }
const size = (bytes: number) => `${(bytes / 1024).toFixed(1)} KB`
</script>
<template><section aria-labelledby="projects-title"><p class="eyebrow">资料空间</p><h1 id="projects-title">项目与文档</h1>
  <div class="split-grid"><form v-if="canManageDocuments" class="panel" @submit.prevent="addProject"><h2>新建项目</h2><label for="project-name">项目名称</label><input id="project-name" v-model="name" required /><label for="project-description">说明</label><textarea id="project-description" v-model="description" /><button type="submit" :disabled="busy">创建项目</button></form>
  <div class="panel"><label for="project-select">当前项目</label><select id="project-select" v-model="projectId"><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.name }}</option></select><label v-if="canManageDocuments" class="upload-button">上传书籍或论文<input type="file" accept=".pdf,.doc,.docx,.txt,.md,.markdown,.csv,.tsv,.xls,.xlsx" :disabled="busy || !projectId" @change="upload" /></label><p class="muted">{{ canManageDocuments ? '系统会保存常见文档与表格；暂不可抽取的文件会由服务端明确标记。' : '当前账号可查看已加入项目中的文档与抽取结果。' }}</p></div></div>
  <p v-if="error" role="alert" class="message error">{{ error }}</p>
  <div class="table-wrap"><table><caption>当前项目的文档</caption><thead><tr><th>文件名</th><th>版本</th><th>类型</th><th>大小</th><th>SHA-256</th><th>状态</th><th>操作</th></tr></thead><tbody><template v-for="document in documents" :key="document.id"><tr><td>{{ document.original_filename }}</td><td>{{ document.version_number }}</td><td>{{ document.mime_type }}</td><td>{{ size(document.size_bytes) }}</td><td class="hash">{{ document.sha256 }}</td><td>{{ document.is_extractable ? '可抽取' : '已保存，暂不支持抽取' }}</td><td><button type="button" @click="showVersions(document)">查看版本</button></td></tr><tr v-if="versions[document.document_id]"><td colspan="7"><ul><li v-for="version in versions[document.document_id]" :key="version.id">版本 {{ version.version_number }} · {{ version.original_filename }} · {{ size(version.size_bytes) }}</li></ul></td></tr></template></tbody></table></div>
</section></template>
