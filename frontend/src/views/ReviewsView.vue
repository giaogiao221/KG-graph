<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { claimReviewTask, claimReviewTaskBatch, createManualFact, getFactVersions, getRootVersions, listReviewBatches, listReviewTasks, releaseReviewTask, type FactVersion, type ReviewBatchSummary, type ReviewTask } from '../api/reviews'
import { getFact } from '../api/facts'
import { listProjects, type Project } from '../api/projects'
import { useAuthStore } from '../stores/auth'
import ReviewEditor from '../components/reviews/ReviewEditor.vue'

const route = useRoute()
const auth = useAuthStore()
const projects = ref<Project[]>([])
const projectId = ref('')
const batches = ref<ReviewBatchSummary[]>([])
const tasks = ref<ReviewTask[]>([])
const total = ref(0)
const offset = ref(0)
const pageSize = 25
const queueMode = ref<'available' | 'mine' | 'completed'>('available')
const activeTask = ref<ReviewTask | null>(null)
const activeVersion = ref<FactVersion | null>(null)
const history = ref<FactVersion[]>([])
const error = ref('')
const loading = ref(false)
const selectedTaskIds = ref<string[]>([])
const claimNotice = ref('')
const manual = ref({ subject: '', property: '', value: '', evidence: '' })
let controller: AbortController | null = null
let generation = 0

const batchId = computed(() => typeof route.params.batchId === 'string' ? route.params.batchId : '')
const activeBatch = computed(() => batches.value.find((batch) => batch.batch_id === batchId.value))
const page = computed(() => Math.floor(offset.value / pageSize) + 1)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const presetLabel: Record<string, string> = { rule: '规则', llm: '纯大模型', hybrid: '规则 + 大模型' }
const allVisibleSelected = computed(() => tasks.value.length > 0 && tasks.value.every((task) => selectedTaskIds.value.includes(task.id)))

function resetActive() { activeTask.value = null; activeVersion.value = null; history.value = [] }
function beginRequest() { controller?.abort(); controller = new AbortController(); return { signal: controller.signal, current: ++generation } }

async function loadBatches() {
  if (!projectId.value) return
  const request = beginRequest(); loading.value = true; error.value = ''
  try {
    const page = await listReviewBatches(projectId.value, request.signal)
    if (request.current === generation) batches.value = page.items
  } catch (reason) {
    if (!(reason instanceof DOMException && reason.name === 'AbortError') && request.current === generation) error.value = '评审批次暂时无法载入'
  } finally { if (request.current === generation) loading.value = false }
}

async function loadTasks() {
  if (!projectId.value || !batchId.value) return
  const request = beginRequest(); loading.value = true; error.value = ''
  const status = queueMode.value === 'mine' ? 'claimed' : queueMode.value
  try {
    const page = await listReviewTasks(projectId.value, {
      status,
      reviewerId: queueMode.value === 'mine' ? auth.user?.id : undefined,
      batchId: batchId.value,
      offset: offset.value,
      limit: pageSize,
      signal: request.signal,
    })
    if (request.current === generation) { tasks.value = page.items; total.value = page.total }
  } catch (reason) {
    if (!(reason instanceof DOMException && reason.name === 'AbortError') && request.current === generation) error.value = '批次任务暂时无法载入'
  } finally { if (request.current === generation) loading.value = false }
}

async function reloadVersion() {
  const task = activeTask.value
  if (!task) return
  const versions = task.raw_fact_id ? await getFactVersions(projectId.value, task.raw_fact_id) : await getRootVersions(projectId.value, task.root_id)
  history.value = versions.items
  const latest = versions.items.at(-1)
  if (latest) { activeVersion.value = latest; return }
  if (!task.raw_fact_id) { activeVersion.value = null; return }
  const fact = await getFact(projectId.value, task.raw_fact_id)
  activeVersion.value = { root_id: task.root_id, version_number: 0, row_json: fact.row_json, editable_values: { subject: fact.subject, property: fact.property, value: fact.value, evidence: fact.evidence_text } }
}

async function activate(task: ReviewTask) { activeTask.value = task; await reloadVersion() }
async function claim(task: ReviewTask) { try { await activate(await claimReviewTask(projectId.value, task.id)); selectedTaskIds.value = selectedTaskIds.value.filter((id) => id !== task.id); await loadTasks(); void loadBatches() } catch { error.value = '该任务可能已被其他评审员领取，请刷新当前批次' } }
function toggleVisibleTasks() { selectedTaskIds.value = allVisibleSelected.value ? [] : tasks.value.map((task) => task.id) }
async function claimSelected() {
  if (!selectedTaskIds.value.length) return
  try {
    const result = await claimReviewTaskBatch(projectId.value, selectedTaskIds.value)
    selectedTaskIds.value = []
    if (result.items[0]) await activate(result.items[0])
    claimNotice.value = result.skipped_task_ids.length
      ? `已领取 ${result.items.length} 项；${result.skipped_task_ids.length} 项已被其他评审员领取。`
      : `已领取 ${result.items.length} 项任务。`
    await loadTasks(); void loadBatches()
  } catch { error.value = '批量领取未能完成，请刷新当前批次后重试' }
}
async function release() { if (!activeTask.value) return; await releaseReviewTask(projectId.value, activeTask.value); resetActive(); await loadTasks() }
async function manualCreate() { try { await createManualFact(projectId.value, manual.value); manual.value = { subject: '', property: '', value: '', evidence: '' } } catch { error.value = '手工结果未能保存，请检查必填字段' } }
function saved() { resetActive(); void loadTasks(); void loadBatches() }
function changeMode(mode: 'available' | 'mine' | 'completed') { queueMode.value = mode; offset.value = 0; selectedTaskIds.value = []; resetActive(); void loadTasks() }
function previousPage() { if (offset.value > 0) { offset.value = Math.max(0, offset.value - pageSize); selectedTaskIds.value = []; void loadTasks() } }
function nextPage() { if (offset.value + pageSize < total.value) { offset.value += pageSize; selectedTaskIds.value = []; void loadTasks() } }

watch(projectId, () => { batches.value = []; tasks.value = []; offset.value = 0; selectedTaskIds.value = []; resetActive(); void loadBatches().then(() => { if (batchId.value) void loadTasks() }) })
watch(batchId, () => { tasks.value = []; offset.value = 0; selectedTaskIds.value = []; resetActive(); if (batchId.value) void loadTasks() })
onMounted(async () => { projects.value = await listProjects(); projectId.value = String(route.query.project || projects.value[0]?.id || ''); if (projectId.value) await loadBatches(); if (batchId.value) await loadTasks() })
onBeforeUnmount(() => { generation += 1; controller?.abort() })
</script>

<template>
  <section class="review-page" aria-labelledby="reviews-title">
    <p class="eyebrow">多人协作</p>
    <div class="page-heading-row">
      <div><h1 id="reviews-title">{{ batchId ? '批次评审' : '结果评审' }}</h1><p v-if="activeBatch" class="muted">批次 {{ activeBatch.batch_id.slice(0, 8) }} · {{ presetLabel[activeBatch.preset] || activeBatch.preset }} · {{ new Date(activeBatch.created_at).toLocaleString('zh-CN') }}</p></div>
      <label>项目<select v-model="projectId"><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.name }}</option></select></label>
    </div>
    <p v-if="error" role="alert" class="message error">{{ error }}</p>
    <p v-if="claimNotice" role="status" class="message success">{{ claimNotice }}</p>

    <template v-if="!batchId">
      <div class="section-toolbar"><h2>评审批次</h2><span class="muted">共 {{ batches.length }} 个批次</span></div>
      <div class="table-wrap review-batch-table"><table><thead><tr><th>批次</th><th>创建时间</th><th>抽取方式</th><th>总任务</th><th>待领取</th><th>处理中</th><th>已完成</th><th>进度</th><th>操作</th></tr></thead><tbody>
        <tr v-for="batch in batches" :key="batch.batch_id"><td class="hash-short">{{ batch.batch_id.slice(0, 8) }}</td><td>{{ new Date(batch.created_at).toLocaleString('zh-CN') }}</td><td>{{ presetLabel[batch.preset] || batch.preset }}</td><td>{{ batch.total }}</td><td>{{ batch.pending }}</td><td>{{ batch.claimed }}</td><td>{{ batch.completed }}</td><td><div class="progress-track"><span :style="{ width: `${batch.total ? Math.round(batch.completed / batch.total * 100) : 0}%` }" /></div></td><td><RouterLink class="button-link compact" :to="{ name: 'review-batch', params: { batchId: batch.batch_id }, query: { project: projectId } }">进入批次</RouterLink></td></tr>
        <tr v-if="!loading && !batches.length"><td colspan="9" class="empty-cell">当前项目暂无评审批次</td></tr>
      </tbody></table></div>
      <details class="panel manual-review-create"><summary>手工新增结果</summary><form @submit.prevent="manualCreate"><div class="form-grid"><label>主体<input v-model="manual.subject" required /></label><label>关系<input v-model="manual.property" required /></label><label>值<input v-model="manual.value" required /></label><label>证据<textarea v-model="manual.evidence" required /></label></div><button type="submit">保存手工结果</button></form></details>
    </template>

    <template v-else>
      <div class="section-toolbar"><RouterLink class="text-link" :to="{ name: 'reviews', query: { project: projectId } }">返回批次列表</RouterLink><div class="segmented" aria-label="任务状态"><button type="button" :class="{ active: queueMode === 'available' }" @click="changeMode('available')">可领取</button><button type="button" :class="{ active: queueMode === 'mine' }" @click="changeMode('mine')">我的任务</button><button type="button" :class="{ active: queueMode === 'completed' }" @click="changeMode('completed')">已完成</button></div></div>
      <div class="review-workspace">
        <section class="review-task-pane" aria-labelledby="task-table-title"><div class="section-toolbar"><h2 id="task-table-title">任务列表</h2><div v-if="queueMode === 'available'" class="toolbar"><span class="muted">已选 {{ selectedTaskIds.length }} 项</span><button type="button" :disabled="!selectedTaskIds.length" @click="claimSelected">批量领取</button></div><span v-else class="muted">{{ total }} 条</span></div><div class="table-wrap"><table><thead><tr><th v-if="queueMode === 'available'"><input type="checkbox" aria-label="全选当前页任务" :checked="allVisibleSelected" @change="toggleVisibleTasks" /></th><th>主体</th><th>关系</th><th>值</th><th>状态</th><th>操作</th></tr></thead><tbody>
          <tr v-for="task in tasks" :key="task.id" :class="{ selected: activeTask?.id === task.id }"><td v-if="queueMode === 'available'"><input v-model="selectedTaskIds" type="checkbox" :value="task.id" :aria-label="`选择任务 ${task.id.slice(0, 8)}`" /></td><td>{{ task.subject }}</td><td>{{ task.property }}</td><td class="truncate-cell">{{ task.value }}</td><td>{{ task.status === 'pending' ? '待领取' : task.status === 'claimed' ? '处理中' : '已完成' }}</td><td><button v-if="queueMode === 'available'" type="button" @click="claim(task)">领取</button><button v-else-if="queueMode === 'mine'" type="button" @click="activate(task)">继续</button><span v-else class="muted">已归档</span></td></tr>
          <tr v-if="!loading && !tasks.length"><td :colspan="queueMode === 'available' ? 6 : 5" class="empty-cell">当前状态没有任务</td></tr>
        </tbody></table></div><div class="pagination"><button type="button" :disabled="offset === 0" @click="previousPage">上一页</button><span>第 {{ page }} / {{ pageCount }} 页</span><button type="button" :disabled="offset + pageSize >= total" @click="nextPage">下一页</button></div></section>
        <section class="review-editor-pane" aria-labelledby="editor-title"><div class="section-toolbar"><h2 id="editor-title">当前评审</h2><div v-if="activeTask" class="toolbar"><button type="button" @click="release">放回队列</button></div></div><template v-if="activeTask && activeVersion"><ReviewEditor :project-id="projectId" :task="activeTask" :version="activeVersion" @reload="reloadVersion" @saved="saved" /><details v-if="history.length" class="version-history"><summary>版本历史（{{ history.length }}）</summary><ol><li v-for="version in history" :key="version.id || version.version_number">版本 {{ version.version_number }} · {{ version.action || '原始结果' }}</li></ol></details></template><p v-else class="empty-editor">从左侧领取或继续一项任务</p></section>
      </div>
    </template>
  </section>
</template>
