<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { ApiError } from '../../api/client'
import { submitReview, type FactVersion, type ReviewTask } from '../../api/reviews'

const props = defineProps<{ projectId: string; task: ReviewTask; version: FactVersion }>()
const emit = defineEmits<{ reload: []; saved: [FactVersion] }>()
const form = reactive({ subject: '', property: '', value: '', evidence: '' })
const busy = ref(false); const conflict = ref(false); const error = ref('')
function load() {
  const values = props.version.editable_values || props.version.row_json
  form.subject = values.subject || ''
  form.property = values.property || ''
  form.value = values.value || ''
  form.evidence = values.evidence || values.evidence_text || ''
  conflict.value = false; error.value = ''
}
watch(() => props.version, load, { immediate: true, deep: true })
async function save(action: string) {
  if (busy.value) return
  busy.value = true; conflict.value = false; error.value = ''
  const patch: Record<string, string> = action === 'modify_approve'
    ? { subject: form.subject, property: form.property, value: form.value, evidence: form.evidence }
    : {}
  try {
    const saved = await submitReview(props.projectId, props.task, props.version, action, patch, crypto.randomUUID())
    emit('saved', saved)
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 409) conflict.value = true
    else error.value = '评审操作未能完成，请稍后重试'
  } finally { busy.value = false }
}
</script>
<template>
  <form class="review-editor" @submit.prevent>
    <label for="review-subject">主体</label><input id="review-subject" v-model="form.subject" />
    <label for="review-property">关系</label><input id="review-property" v-model="form.property" />
    <label for="review-value">值</label><input id="review-value" v-model="form.value" />
    <label for="review-evidence">证据</label><textarea id="review-evidence" v-model="form.evidence" rows="5" />
    <p v-if="conflict" class="message error" role="alert">该结果已被其他评审员修改，请重新载入</p>
    <p v-if="error" class="message error" role="alert">{{ error }}</p>
    <div class="actions">
      <button type="button" :disabled="busy" @click="save('approve')">通过</button>
      <button type="button" :disabled="busy" @click="save('modify_approve')">修改并通过</button>
      <button type="button" :disabled="busy" @click="save('reject')">驳回</button>
      <button type="button" :disabled="busy" @click="save('dispute')">标记争议</button>
      <button type="button" :disabled="busy" @click="save('delete')">删除</button>
      <button v-if="conflict" type="button" @click="emit('reload')">重新载入最新版本</button>
    </div>
  </form>
</template>
