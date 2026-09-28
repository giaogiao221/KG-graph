<script setup lang="ts">
import type { Routes } from '../../api/profiles'
defineProps<{ routes: Routes; preset: 'rule' | 'llm' | 'hybrid'; documentCount: number; concurrency: number; modelName?: string; promptName?: string }>()
</script>
<template>
  <section class="plan-card" data-testid="execution-plan" aria-labelledby="plan-title">
    <h2 id="plan-title">执行计划确认</h2>
    <ul v-if="preset === 'rule'" class="route-list"><li>本地抽取规则</li><li>平台事实合并</li><li>审核导入</li></ul>
    <ul v-else-if="preset === 'hybrid'" class="route-list"><li>本地抽取规则</li><li>大模型混合抽取</li><li>平台事实合并</li><li>审核导入</li></ul>
    <ul v-else class="route-list"><li>平台纯大模型正文抽取（系统配置提示词）</li><li>平台纯大模型表格抽取（系统配置提示词）</li><li>平台事实合并</li><li>审核导入</li></ul>
    <p>共 {{ documentCount }} 个文档版本；并行数 {{ concurrency }}。</p>
    <p v-if="modelName">模型配置：{{ modelName }}</p>
    <p v-if="promptName">提示词模板：{{ promptName }}</p>
  </section>
</template>
