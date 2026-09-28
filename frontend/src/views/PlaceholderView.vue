<script setup lang="ts">defineProps<{ title: string }>()</script>
<template>
  <section :aria-labelledby="`${title}-title`">
    <p class="eyebrow">协作空间</p>
    <h1 :id="`${title}-title`">{{ title }}</h1>
    <div class="state-card" role="status">
      <h2>功能正在接入</h2>
      <p class="muted">此页面的业务数据将在下一阶段接入。</p>
    </div>
  </section>
</template>
