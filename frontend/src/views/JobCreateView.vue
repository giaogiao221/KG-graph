<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { createBatch } from '../api/jobs'
import { listEnabledModels, type EnabledModel } from '../api/models'
import { listEnabledPrompts, type PromptTemplate } from '../api/prompts'
import { createProfile, listProfiles, type ProfileSummary, type Routes } from '../api/profiles'
import { listDocuments, listProjects, type DocumentVersion, type Project } from '../api/projects'
import ExecutionPlan from '../components/jobs/ExecutionPlan.vue'

type Preset = 'rule' | 'llm' | 'hybrid'
const projects = ref<Project[]>([])
const projectId = ref('')
const documents = ref<DocumentVersion[]>([])
const profiles = ref<ProfileSummary[]>([])
const models = ref<EnabledModel[]>([])
const prompts = ref<PromptTemplate[]>([])
const selectedDocuments = ref<string[]>([])
const profileSource = ref<'existing' | 'new'>('new')
const selectedProfileVersionId = ref('')
const preset = ref<Preset>('rule')
const modelId = ref('')
const promptId = ref('')
const defaultPrompt: PromptTemplate = { id: '4d5b4f4c-88dd-4c86-a34b-0aba65429cf1', name: '默认事实抽取', description: '', text_prompt: '', table_prompt: '', is_enabled: true, created_at: '', updated_at: '' }
const concurrency = ref(2)
const routes = ref<Routes>({ text_rule: true, text_llm: false, table_rule: true, table_llm: false })
const busy = ref(false); const loading = ref(true); const error = ref(''); const success = ref('')
let generation = 0; let controller: AbortController | null = null
const routePresets: Record<Preset, Routes> = {
  rule: { text_rule: true, text_llm: false, table_rule: true, table_llm: false },
  llm: { text_rule: false, text_llm: true, table_rule: false, table_llm: true },
  hybrid: { text_rule: true, text_llm: true, table_rule: true, table_llm: true },
}
const needsModel = computed(() => routes.value.text_llm || routes.value.table_llm)
const needsPrompt = computed(() => preset.value === 'llm')
const modelError = computed(() => needsModel.value && !modelId.value ? '请选择已启用的模型配置' : '')
const promptError = computed(() => needsPrompt.value && !promptId.value ? '请选择纯大模型抽取提示词模板' : '')
const selectedModel = computed(() => models.value.find((model) => model.id === modelId.value))
const selectedPrompt = computed(() => prompts.value.find((prompt) => prompt.id === promptId.value))
const canSubmit = computed(() => selectedDocuments.value.length > 0 && !busy.value && (profileSource.value === 'existing' ? Boolean(selectedProfileVersionId.value) : !modelError.value && !promptError.value))

watch(preset, (value) => { routes.value = { ...routePresets[value] } })
watch(projectId, async (id) => {
  selectedDocuments.value = []; error.value = ''; const current = ++generation
  controller?.abort(); controller = new AbortController()
  if (!id) { documents.value = []; profiles.value = []; selectedProfileVersionId.value = ''; return }
  try {
    const [nextDocuments, nextProfiles] = await Promise.all([listDocuments(id, controller.signal), listProfiles(id, controller.signal)])
    if (current !== generation) return
    documents.value = nextDocuments; profiles.value = nextProfiles; selectedProfileVersionId.value = nextProfiles[0]?.latest_version.id || ''
  } catch (reason) { if (!(reason instanceof DOMException && reason.name === 'AbortError') && current === generation) error.value = '项目数据暂时无法加载，请重试' }
})
onMounted(async () => {
  try { const [nextProjects, nextModels, nextPrompts] = await Promise.all([listProjects(), listEnabledModels(), listEnabledPrompts()]); const safePrompts = Array.isArray(nextPrompts) ? nextPrompts : [defaultPrompt]; projects.value = nextProjects; models.value = nextModels; prompts.value = safePrompts.length ? safePrompts : [defaultPrompt]; promptId.value = prompts.value[0]?.id || ''; projectId.value = nextProjects[0]?.id || '' }
  catch { error.value = '抽取设置暂时无法加载，请重试' } finally { loading.value = false }
})
onBeforeUnmount(() => { generation += 1; controller?.abort() })

async function submit() {
  if (!canSubmit.value) return
  busy.value = true; error.value = ''; success.value = ''
  try {
    let profileVersionId = selectedProfileVersionId.value
    if (profileSource.value === 'new') {
      const profile = await createProfile(projectId.value, { name: `任务方案-${new Date().toISOString()}`, preset: preset.value, model_config_ids: needsModel.value ? [modelId.value] : [], concurrency: concurrency.value, ...(needsPrompt.value ? { prompt_version: promptId.value } : {}) })
      profileVersionId = profile.id
    }
    const batch = await createBatch(projectId.value, profileVersionId, selectedDocuments.value)
    success.value = `任务已提交，批次编号：${batch.id}`
  } catch { error.value = '任务未能提交，请检查文档和抽取设置后重试' } finally { busy.value = false }
}
</script>

<template>
  <section aria-labelledby="job-create-title"><p class="eyebrow">抽取向导</p><h1 id="job-create-title">新建抽取任务</h1>
    <p v-if="loading" role="status">正在加载可用项目和模型</p>
    <form v-else @submit.prevent="submit">
      <label for="job-project">项目</label><select id="job-project" v-model="projectId"><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.name }}</option></select>
      <fieldset><legend>文档版本</legend><label v-for="document in documents" :key="document.id" class="check-row"><input v-model="selectedDocuments" type="checkbox" :value="document.id" :aria-label="document.original_filename" />{{ document.original_filename }}（版本 {{ document.version_number }}）</label><p v-if="!documents.length" class="muted">当前项目暂无可抽取文档。</p></fieldset>
      <label for="profile-source">方案来源</label><select id="profile-source" v-model="profileSource"><option value="existing">使用已有方案</option><option value="new">新建方案</option></select>
      <template v-if="profileSource === 'existing'"><label for="existing-profile">已有抽取方案</label><select id="existing-profile" v-model="selectedProfileVersionId"><option value="">请选择</option><option v-for="profile in profiles" :key="profile.latest_version.id" :value="profile.latest_version.id">{{ profile.name }}（版本 {{ profile.latest_version.version_number }}）</option></select><p v-if="!profiles.length" class="muted">当前项目没有已有方案，请新建方案。</p></template>
      <template v-else>
        <label for="preset">抽取方式</label><select id="preset" v-model="preset"><option value="rule">规则</option><option value="llm">纯大模型</option><option value="hybrid">规则 + 大模型</option></select>
        <label for="model">模型配置</label><select id="model" v-model="modelId"><option value="">{{ needsModel ? '请选择模型配置' : '规则模式不使用模型配置' }}</option><option v-for="model in models" :key="model.id" :value="model.id">{{ model.name }}（{{ model.model_name }}）</option></select>
        <template v-if="needsPrompt"><label for="prompt">提示词模板</label><select id="prompt" v-model="promptId"><option value="">请选择提示词模板</option><option v-for="prompt in prompts" :key="prompt.id" :value="prompt.id">{{ prompt.name }}</option></select><p class="field-help">模板内容由系统管理中的“提示词模板”统一配置；本任务会保存所选版本的内容快照。</p></template>
        <label for="concurrency">并行文档数（1-32）</label><input id="concurrency" v-model.number="concurrency" type="number" min="1" max="32" />
        <p v-if="modelError || promptError" class="message error" role="alert">{{ modelError || promptError }}</p><ExecutionPlan :routes="routes" :preset="preset" :document-count="selectedDocuments.length" :concurrency="concurrency" :model-name="selectedModel?.name" :prompt-name="selectedPrompt?.name" />
      </template>
      <p v-if="error" class="message error" role="alert">{{ error }}</p><p v-if="success" class="message success" role="status">{{ success }}</p><button class="primary" type="submit" :disabled="!canSubmit">{{ busy ? '正在提交…' : '确认并提交任务' }}</button>
    </form>
  </section>
</template>
