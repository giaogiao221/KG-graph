<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, PictorialBarChart, PieChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { DashboardStats } from '../../api/dashboard'

echarts.use([
  BarChart,
  LineChart,
  PictorialBarChart,
  PieChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
])

const props = defineProps<{ stats: DashboardStats }>()

const reviewChartRef = ref<HTMLDivElement>()
const jobChartRef = ref<HTMLDivElement>()
const routeChartRef = ref<HTMLDivElement>()
const tokenChartRef = ref<HTMLDivElement>()

let charts: echarts.ECharts[] = []

const reviewOption = computed(() => {
  const s = props.stats.facts_by_status ?? {}
  const items = [
    { name: '待评审', value: s.candidate, color: '#f59e0b' },
    { name: '评审中', value: s.candidate_review, color: '#3b82f6' },
    { name: '已通过', value: s.approved, color: '#10b981' },
    { name: '已驳回', value: s.rejected, color: '#ef4444' },
  ].filter((item) => item.value > 0)
  return {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, icon: 'circle' },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        avoidLabelOverlap: true,
        itemStyle: { borderRadius: 8, borderColor: '#fff', borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } },
        data: items.map((item) => ({ name: item.name, value: item.value, itemStyle: { color: item.color } })),
      },
    ],
  }
})

const jobOption = computed(() => {
  const s = props.stats.jobs_by_status ?? {}
  const items = [
    { name: '排队', value: s.queued, color: '#94a3b8', symbol: 'circle' },
    { name: '运行中', value: s.running, color: '#3b82f6', symbol: 'circle' },
    { name: '部分成功', value: s.partial_success, color: '#f59e0b', symbol: 'circle' },
    { name: '已完成', value: s.completed, color: '#10b981', symbol: 'circle' },
    { name: '可重试失败', value: s.retryable_failed, color: '#f97316', symbol: 'circle' },
    { name: '永久失败', value: s.permanent_failed, color: '#ef4444', symbol: 'circle' },
    { name: '已取消', value: s.cancelled, color: '#9ca3af', symbol: 'circle' },
  ]
  const maxValue = Math.max(...items.map((item) => item.value), 1)
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 8, top: 30, bottom: 0, containLabel: true },
    xAxis: { type: 'category', data: items.map((item) => item.name), axisLine: { show: false }, axisTick: { show: false } },
    yAxis: { type: 'value', splitLine: { lineStyle: { type: 'dashed' } } },
    series: [
      {
        type: 'pictorialBar',
        symbol: 'rect',
        symbolRepeat: true,
        symbolSize: ['100%', 4],
        symbolMargin: 2,
        data: items.map((item) => ({
          value: item.value,
          itemStyle: { color: item.color },
          symbolClip: true,
        })),
        z: 10,
      },
      {
        type: 'pictorialBar',
        symbol: 'rect',
        symbolRepeat: true,
        symbolSize: ['100%', 4],
        symbolMargin: 2,
        symbolClip: true,
        data: items.map(() => maxValue),
        itemStyle: { color: '#f1f5f9' },
        z: 5,
      },
    ],
  }
})

const routeOption = computed(() => {
  const s = props.stats.facts_by_route ?? {}
  const labels: Record<string, string> = {
    rule_text: '规则·正文',
    rule_table: '规则·表格',
    llm_text: '大模型·正文',
    llm_table: '大模型·表格',
  }
  const items = Object.entries(s).map(([key, value]) => ({
    name: labels[key] ?? key,
    value,
  }))
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 8, top: 10, bottom: 0, containLabel: true },
    xAxis: { type: 'value', splitLine: { lineStyle: { type: 'dashed' } } },
    yAxis: { type: 'category', data: items.map((item) => item.name), axisLine: { show: false }, axisTick: { show: false } },
    series: [
      {
        type: 'bar',
        data: items.map((item) => item.value),
        barWidth: 18,
        itemStyle: { borderRadius: [0, 8, 8, 0], color: '#6366f1' },
        label: { show: true, position: 'right', fontSize: 12 },
      },
    ],
  }
})

const tokenOption = computed(() => {
  const tokens = props.stats.tokens ?? []
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 8, right: 8, top: 30, bottom: 0, containLabel: true },
    xAxis: { type: 'category', data: tokens.map((t) => t.currency), axisLine: { show: false }, axisTick: { show: false } },
    yAxis: [
      { type: 'value', name: 'Tokens', splitLine: { lineStyle: { type: 'dashed' } } },
      { type: 'value', name: '费用', splitLine: { show: false } },
    ],
    series: [
      {
        name: '总 Tokens',
        type: 'bar',
        data: tokens.map((t) => t.total_tokens),
        itemStyle: { color: '#3b82f6', borderRadius: [8, 8, 0, 0] },
        barWidth: 30,
      },
      {
        name: '费用',
        type: 'line',
        yAxisIndex: 1,
        data: tokens.map((t) => Number(t.cost)),
        itemStyle: { color: '#f59e0b' },
        lineStyle: { width: 3 },
        smooth: true,
      },
    ],
  }
})

function renderCharts() {
  const targets = [
    { el: reviewChartRef.value, option: reviewOption.value },
    { el: jobChartRef.value, option: jobOption.value },
    { el: routeChartRef.value, option: routeOption.value },
    { el: tokenChartRef.value, option: tokenOption.value },
  ]
  for (const target of targets) {
    if (!target.el) continue
    let chart = charts.find((c) => c.getDom() === target.el)
    if (!chart) {
      chart = echarts.init(target.el)
      charts.push(chart)
    }
    chart.setOption(target.option, true)
  }
}

function resizeCharts() {
  for (const chart of charts) chart.resize()
}

onMounted(() => {
  renderCharts()
  window.addEventListener('resize', resizeCharts)
})

watch(() => props.stats, renderCharts, { deep: true })

onBeforeUnmount(() => {
  window.removeEventListener('resize', resizeCharts)
  for (const chart of charts) chart.dispose()
  charts = []
})
</script>

<template>
  <div class="stats-charts">
    <div class="chart-card">
      <h3>评审状态分布</h3>
      <div ref="reviewChartRef" class="chart-body" aria-label="评审状态环形图" />
    </div>
    <div class="chart-card">
      <h3>任务状态统计</h3>
      <div ref="jobChartRef" class="chart-body" aria-label="任务状态象形柱图" />
    </div>
    <div class="chart-card">
      <h3>事实来源路线</h3>
      <div ref="routeChartRef" class="chart-body" aria-label="事实来源路线条形图" />
    </div>
    <div class="chart-card">
      <h3>Token 用量与费用</h3>
      <div ref="tokenChartRef" class="chart-body" aria-label="Token 用量与费用图" />
    </div>
  </div>
</template>
