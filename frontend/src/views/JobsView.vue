<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { cancelBatch, deleteBatch, listBatches, retryStep, streamBatchProgress, type Batch, type JobStep } from '../api/jobs'
import { listProjects, type Project } from '../api/projects'
import { useAuthStore } from '../stores/auth'
const auth = useAuthStore(); const projects = ref<Project[]>([]); const projectId = ref(''); const batches = ref<Batch[]>([]); const error = ref(''); const streaming = ref(false); const hiddenBatchIds = ref(new Set<string>())
let listRequest: AbortController | null = null; let poll: number | null = null; let generation = 0; const streams = new Map<string, AbortController>()
const terminal = new Set(['completed', 'permanent_failed', 'cancelled'])
const status: Record<string, string> = { queued: '排队中', running: '正在处理', partial_success: '部分完成', completed: '已完成', retryable_failed: '可重试失败', permanent_failed: '永久失败', cancelled: '已取消' }
function stop() { generation += 1; listRequest?.abort(); listRequest = null; for (const item of streams.values()) item.abort(); streams.clear(); if (poll !== null) window.clearInterval(poll); poll = null; streaming.value = false }
async function load(project: string, current: number) {
  listRequest?.abort(); const active = new AbortController(); listRequest = active
  const page = await listBatches(project, active.signal)
  if (current !== generation || project !== projectId.value) return
  batches.value = page.items.filter((batch) => !hiddenBatchIds.value.has(batch.id)); connectActive(project, current)
}
function connectActive(project: string, current: number) {
  if (!auth.accessToken) return
  const activeIds = new Set(batches.value.filter((batch) => !terminal.has(batch.status)).map((batch) => batch.id))
  for (const [id, controller] of streams) if (!activeIds.has(id)) { controller.abort(); streams.delete(id) }
  for (const batchId of activeIds) {
    if (streams.has(batchId)) continue
    const controller = new AbortController(); streams.set(batchId, controller); streaming.value = true
    void streamBatchProgress(project, batchId, { token: auth.accessToken, signal: controller.signal, onProgress: (batch) => { if (current !== generation || project !== projectId.value) return; const index = batches.value.findIndex((item) => item.id === batch.id); if (index >= 0) batches.value[index] = batch } })
      .catch(() => undefined).finally(() => { if (streams.get(batchId) === controller) streams.delete(batchId); if (current === generation) streaming.value = streams.size > 0 })
  }
}
watch(projectId, async (project) => { stop(); batches.value = []; error.value = ''; if (!project) return; const current = ++generation; try { await load(project, current); poll = window.setInterval(() => void load(project, current).catch(() => undefined), 5000) } catch { if (current === generation) error.value = '任务列表暂时无法载入' } })
onMounted(async () => { try { projects.value = await listProjects(); projectId.value = projects.value[0]?.id || '' } catch { error.value = '项目列表暂时无法载入' } }); onBeforeUnmount(stop)
async function retry(batch: Batch, step: JobStep) { const project = projectId.value; const current = generation; try { await retryStep(project, batch.id, step); if (current === generation && project === projectId.value) await load(project, current) } catch { if (current === generation) error.value = '任务状态已变化，请刷新后重试' } }
async function cancel(batch: Batch) { if (!confirm(`终止批次 ${batch.id.slice(0, 8)}？未完成步骤将被取消。`)) return; try { await cancelBatch(projectId.value, batch.id); await load(projectId.value, generation) } catch { error.value = '批次未能终止，请刷新后重试' } }
async function remove(batch: Batch) { if (!confirm(`删除批次 ${batch.id.slice(0, 8)}？任务会终止并从列表隐藏，调用审计记录会保留。`)) return; try { await deleteBatch(projectId.value, batch.id); hiddenBatchIds.value.add(batch.id); batches.value = batches.value.filter((item) => item.id !== batch.id) } catch { error.value = '批次未能删除，请刷新后重试' } }
function stepLabel(job: { steps: JobStep[] }, step: JobStep) {
  if (step.status !== 'queued') return status[step.status] || step.status
  const failed = job.steps.find((item) => item.status === 'permanent_failed')
  return failed ? `等待上游失败（${failed.kind}）` : status.queued
}
function bookTitle(filename: string) { return filename.replace(/\.[^.]+$/, '') || filename }
function batchTitle(batch: Batch) { const books = [...new Set(batch.document_jobs.map((job) => bookTitle(job.original_filename)))]; return books.length > 1 ? `${books[0]} 等 ${books.length} 本` : books[0] || `批次 ${batch.id.slice(0, 8)}` }
</script>
<template><section aria-labelledby="jobs-title"><p class="eyebrow">执行中心</p><h1 id="jobs-title">抽取任务</h1><div class="toolbar"><label for="jobs-project">项目</label><select id="jobs-project" v-model="projectId"><option v-for="project in projects" :value="project.id" :key="project.id">{{ project.name }}</option></select><RouterLink class="button-link" to="/jobs/new">新建抽取任务</RouterLink><span role="status">{{ streaming ? '全部运行批次已连接实时进度' : '使用安全轮询更新全部批次' }}</span></div><p v-if="error" role="alert" class="message error">{{ error }}</p><p v-if="!batches.length" class="empty">当前项目还没有抽取任务。</p><article v-for="batch in batches" :key="batch.id" class="batch-card"><header><div><h2>{{ batchTitle(batch) }}</h2><p class="batch-meta">批次 ID：{{ batch.id.slice(0, 8) }} · {{ new Date(batch.created_at).toLocaleString() }}</p></div><div class="batch-actions"><button v-if="!terminal.has(batch.status)" type="button" @click="cancel(batch)">终止</button><button type="button" @click="remove(batch)">删除</button><span class="status" :class="`status--${batch.status}`">{{ status[batch.status] || batch.status }}</span></div></header><div v-for="job in batch.document_jobs" :key="job.id" class="document-job"><h3>{{ bookTitle(job.original_filename) }} · 第 {{ job.document_version_number }} 版 · {{ status[job.status] || job.status }}</h3><ul><li v-for="step in job.steps" :key="step.id"><span>{{ step.kind }}：{{ stepLabel(job, step) }}</span><span v-if="step.failure_summary" class="error-text">{{ step.failure_summary }}</span><button v-if="step.status === 'retryable_failed'" type="button" @click="retry(batch, step)">安全重试</button><span v-else-if="step.status === 'permanent_failed'">需人工检查，不能自动重试</span></li></ul></div></article></section></template>
