<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { createPrompt, deletePrompt, listAdminPrompts, updatePrompt, type PromptTemplate } from '../api/prompts'

const items = ref<PromptTemplate[]>([])
const selected = ref<PromptTemplate | null>(null)
const name = ref('')
const description = ref('')
const textPrompt = ref('')
const tablePrompt = ref('')
const enabled = ref(true)
const busy = ref(false)
const message = ref('')
const error = ref('')

function reset(item?: PromptTemplate) {
  selected.value = item ?? null
  name.value = item?.name ?? ''
  description.value = item?.description ?? ''
  textPrompt.value = item?.text_prompt ?? ''
  tablePrompt.value = item?.table_prompt ?? ''
  enabled.value = item?.is_enabled ?? true
  message.value = ''; error.value = ''
}
async function load() { items.value = await listAdminPrompts(); if (!selected.value && items.value[0]) reset(items.value[0]) }
async function save() {
  busy.value = true; message.value = ''; error.value = ''
  const payload = { name: name.value.trim(), description: description.value.trim(), text_prompt: textPrompt.value.trim(), table_prompt: tablePrompt.value.trim(), is_enabled: enabled.value }
  try { const value = selected.value ? await updatePrompt(selected.value.id, payload) : await createPrompt(payload); await load(); reset(value); message.value = '提示词模板已保存。新建的纯大模型任务会固定使用该模板快照。' }
  catch { error.value = '模板未能保存，请检查名称和两类提示词内容。' } finally { busy.value = false }
}
async function remove() {
  if (!selected.value || !confirm(`删除模板“${selected.value.name}”？已有任务不受影响。`)) return
  try { await deletePrompt(selected.value.id); reset(); await load(); message.value = '提示词模板已删除。' } catch { error.value = '模板已有历史引用，不能删除；请改为停用。' }
}
onMounted(() => { void load().catch(() => { error.value = '提示词模板暂时无法载入。' }) })
</script>

<template>
  <section class="admin-page prompt-page" aria-labelledby="prompt-title">
    <p class="eyebrow">系统管理</p><h1 id="prompt-title">提示词模板</h1>
    <p v-if="message" class="message success">{{ message }}</p><p v-if="error" class="message error">{{ error }}</p>
    <div class="prompt-workspace">
      <section class="prompt-list" aria-label="已有提示词模板"><header><h2>已有模板</h2><button type="button" @click="reset()">新增模板</button></header>
        <div class="table-wrap"><table><thead><tr><th>名称</th><th>状态</th><th>更新时间</th></tr></thead><tbody><tr v-for="item in items" :key="item.id" :class="{ selected: selected?.id === item.id }" @click="reset(item)"><td><strong>{{ item.name }}</strong><small>{{ item.description || '未填写说明' }}</small></td><td><span class="status" :class="item.is_enabled ? 'status--completed' : 'status--cancelled'">{{ item.is_enabled ? '启用' : '停用' }}</span></td><td>{{ new Date(item.updated_at).toLocaleDateString() }}</td></tr><tr v-if="!items.length"><td colspan="3" class="empty-cell">暂无模板</td></tr></tbody></table></div>
      </section>
      <form class="prompt-editor" @submit.prevent="save"><header><div><h2>{{ selected ? '编辑模板' : '新增模板' }}</h2><p>每个模板都包含正文与表格两段独立指令。</p></div><label class="switch"><input v-model="enabled" type="checkbox" />启用</label></header>
        <label>模板名称<input v-model="name" required maxlength="200" placeholder="例如：教材事实抽取" /></label>
        <label>适用说明<input v-model="description" maxlength="500" placeholder="用于说明抽取范围和用途" /></label>
        <label>正文抽取提示词<textarea v-model="textPrompt" required rows="11" minlength="40" /></label>
        <label>表格抽取提示词<textarea v-model="tablePrompt" required rows="11" minlength="40" /></label>
        <footer><button v-if="selected" class="danger-button" type="button" @click="remove">删除</button><span></span><button type="submit" class="primary" :disabled="busy">{{ busy ? '正在保存…' : '保存模板' }}</button></footer>
      </form>
    </div>
  </section>
</template>
