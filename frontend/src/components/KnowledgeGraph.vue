<template>
  <div class="kg-page">
    <header class="topbar">
      <h1>语义血缘图谱</h1>
      <span class="crumb">指标 / 维度 / 语义模型 / 度量 的关系图（确定性构图）</span>
      <div class="actions">
        <el-select
          v-model="focusMetric" size="small" clearable filterable
          placeholder="聚焦某指标（一跳血缘）" style="width: 240px"
          @change="onFocusChange"
        >
          <el-option v-for="m in metricNames" :key="m" :label="m" :value="m" />
        </el-select>
        <el-button size="small" plain :loading="loading" @click="loadFull">全量</el-button>
      </div>
    </header>

    <section class="kg-body">
      <div class="kg-graph-wrap">
        <div v-if="loading" class="kg-hint">图谱加载中…</div>
        <div v-else-if="loadError" class="kg-hint err">{{ loadError }}</div>
        <div v-else-if="!hasData" class="kg-hint">当前语义层无图谱数据</div>
        <div v-show="hasData" ref="chartEl" class="kg-canvas"></div>
        <div class="kg-legend">
          <span><i class="dot" style="background:#005BFF"></i>指标</span>
          <span><i class="dot" style="background:#10B981"></i>维度</span>
          <span><i class="dot" style="background:#8B5CF6"></i>语义模型</span>
          <span><i class="dot" style="background:#F59E0B"></i>度量</span>
        </div>
      </div>

      <aside class="kg-side">
        <div class="side-card">
          <div class="side-title">血缘查询（RAG 增强）</div>
          <el-input
            v-model="query" type="textarea" :rows="3" size="small"
            placeholder="输入自然语言问题，抽取命中指标并展开血缘，如：存款余额按地区怎么分布"
          />
          <el-button size="small" type="primary" class="side-btn" :loading="lineageLoading" @click="runLineage">
            展开血缘
          </el-button>
          <div v-if="lineageText" class="lineage-out">
            <div class="lineage-metrics">命中指标：{{ lineageMetrics.join(' / ') }}</div>
            <pre class="lineage-text">{{ lineageText }}</pre>
          </div>
          <div v-else-if="lineageQueried" class="lineage-out muted">未从问题中识别到已收录指标</div>
        </div>
        <div class="side-card">
          <div class="side-title">节点详情</div>
          <div v-if="selected" class="node-detail">
            <div class="nd-type">{{ typeLabel(selected.type) }} · {{ selected.id }}</div>
            <div class="nd-label">{{ selected.label }}</div>
            <div v-for="(v, k) in selected.props" :key="k" class="nd-prop">
              <span class="nd-k">{{ k }}</span><span class="nd-v">{{ formatVal(v) }}</span>
            </div>
          </div>
          <div v-else class="muted">点击图中节点查看属性</div>
        </div>
      </aside>
    </section>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { semanticKnowledgeGraph, semanticKnowledgeLineage } from '../utils/api'
import { store } from '../store'

echarts.use([GraphChart, TooltipComponent, LegendComponent, CanvasRenderer])

const TYPE_ORDER = ['metric', 'dimension', 'model', 'measure']
const TYPE_LABEL = { metric: '指标', dimension: '维度', model: '语义模型', measure: '度量' }
const TYPE_COLOR = { metric: '#005BFF', dimension: '#10B981', model: '#8B5CF6', measure: '#F59E0B' }

const chartEl = ref(null)
let chart = null
const loading = ref(false)
const loadError = ref('')
const graph = ref({ nodes: [], edges: [] })
const focusMetric = ref('')
const query = ref('')
const lineageText = ref('')
const lineageMetrics = ref([])
const lineageLoading = ref(false)
const lineageQueried = ref(false)
const selected = ref(null)

const hasData = computed(() => graph.value.nodes.length > 0)
const metricNames = computed(() =>
  graph.value.nodes.filter((n) => n.type === 'metric').map((n) => n.label)
)
function typeLabel(t) { return TYPE_LABEL[t] || t }
function formatVal(v) {
  if (Array.isArray(v)) return v.join(' / ')
  return String(v ?? '')
}

async function loadGraph(params = {}) {
  loading.value = true
  loadError.value = ''
  selected.value = null
  try {
    const res = await semanticKnowledgeGraph(params)
    graph.value = (res && res.graph) || { nodes: [], edges: [] }
    await nextTick()
    renderChart()
  } catch (e) {
    loadError.value = e.message || '图谱加载失败'
  } finally {
    loading.value = false
  }
}

function loadFull() {
  focusMetric.value = ''
  loadGraph({})
}

function onFocusChange(name) {
  if (!name) { loadFull(); return }
  loadGraph({ focus: name, hops: 1 })
}

async function runLineage() {
  lineageLoading.value = true
  lineageText.value = ''
  lineageMetrics.value = []
  lineageQueried.value = false
  try {
    const res = await semanticKnowledgeLineage(query.value)
    lineageMetrics.value = res.metrics || []
    lineageText.value = res.text || ''
    lineageQueried.value = true
    if (lineageMetrics.value.length) focusMetric.value = lineageMetrics.value[0]
  } catch (e) {
    lineageText.value = '血缘查询失败：' + (e.message || '')
  } finally {
    lineageLoading.value = false
  }
}

function renderChart() {
  if (!chartEl.value) return
  if (!chart) chart = echarts.init(chartEl.value)
  const g = graph.value
  const degree = {}
  g.edges.forEach((e) => {
    degree[e.source] = (degree[e.source] || 0) + 1
    degree[e.target] = (degree[e.target] || 0) + 1
  })
  const nodes = g.nodes.map((n) => {
    const d = degree[n.id] || 0
    const size = (n.type === 'metric' ? 44 : n.type === 'model' ? 38 : 30) + Math.min(d, 6) * 2
    return {
      id: n.id,
      name: n.id,
      label: n.label,
      category: TYPE_ORDER.indexOf(n.type),
      symbolSize: size,
      value: n.props,
      itemStyle: { color: TYPE_COLOR[n.type] },
    }
  })
  const links = g.edges.map((e) => ({
    source: e.source,
    target: e.target,
    label: { show: false, formatter: e.label },
    lineStyle: { color: '#C9CDD4', curveness: 0.12, width: 1.2 },
  }))
  chart.setOption(
    {
      backgroundColor: 'transparent',
      tooltip: {
        confine: true,
        backgroundColor: 'rgba(255,255,255,0.96)',
        borderColor: '#E4E3DD',
        textStyle: { color: '#333', fontSize: 12 },
        formatter: (p) => {
          if (p.dataType === 'edge') return p.data.label || ''
          const type = TYPE_ORDER[p.data.category]
          return `<b>${p.data.label}</b><br/>类型：${TYPE_LABEL[type] || type}`
        },
      },
      legend: { show: false },
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          draggable: true,
          label: { show: true, position: 'right', fontSize: 11, color: '#1A1B1C', formatter: (p) => p.data.label },
          edgeLabel: { show: false },
          emphasis: { focus: 'adjacency', label: { show: true }, lineStyle: { width: 2 } },
          force: { repulsion: 220, edgeLength: [60, 140], gravity: 0.08 },
          categories: TYPE_ORDER.map((t) => ({ name: TYPE_LABEL[t], itemStyle: { color: TYPE_COLOR[t] } })),
          data: nodes,
          links,
        },
      ],
    },
    true
  )
  chart.off('click')
  chart.on('click', (p) => {
    if (p.dataType === 'node') {
      const raw = g.nodes.find((n) => n.id === p.data.id)
      if (raw) selected.value = raw
    }
  })
}

function onResize() { if (chart) chart.resize() }

onMounted(() => {
  loadGraph({})
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (chart) { chart.dispose(); chart = null }
})
</script>

<style scoped>
.kg-page { display: flex; flex-direction: column; height: 100%; }
.topbar {
  display: flex; align-items: center; gap: 12px; padding: 12px 18px;
  border-bottom: 1px solid var(--border, #ECECEC); background: #fff;
}
.topbar h1 { font-size: 16px; margin: 0; color: #1A1B1C; }
.crumb { font-size: 12px; color: var(--muted, #8a8f99); }
.actions { margin-left: auto; display: flex; gap: 8px; }
.kg-body { flex: 1; display: flex; gap: 12px; padding: 12px; min-height: 0; }
.kg-graph-wrap {
  position: relative; flex: 1; min-width: 0;
  border: 1px solid var(--border, #ECECEC); border-radius: 12px; background: #FBFBF9;
  overflow: hidden;
}
.kg-canvas { width: 100%; height: 100%; min-height: 480px; }
.kg-hint {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: var(--muted, #8a8f99); font-size: 13px;
}
.kg-hint.err { color: #EA6668; }
.kg-legend {
  position: absolute; left: 12px; bottom: 10px; display: flex; gap: 14px;
  font-size: 12px; color: #6B7280; background: rgba(255,255,255,0.8);
  padding: 4px 10px; border-radius: 8px;
}
.kg-legend .dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
.kg-side { width: 320px; flex: none; display: flex; flex-direction: column; gap: 12px; overflow: auto; }
.side-card {
  border: 1px solid var(--border, #ECECEC); border-radius: 12px; background: #fff; padding: 12px;
}
.side-title { font-size: 13px; font-weight: 600; color: #1A1B1C; margin-bottom: 8px; }
.side-btn { margin-top: 8px; width: 100%; }
.lineage-out { margin-top: 10px; }
.lineage-metrics { font-size: 12px; color: #005BFF; margin-bottom: 4px; }
.lineage-text {
  white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.7;
  color: #374151; background: #F7F8FA; border-radius: 8px; padding: 8px 10px; margin: 0;
}
.muted { color: var(--muted, #8a8f99); font-size: 12px; }
.node-detail .nd-type { font-size: 11px; color: var(--muted, #8a8f99); }
.node-detail .nd-label { font-size: 14px; font-weight: 600; color: #1A1B1C; margin: 2px 0 8px; }
.nd-prop { display: flex; gap: 8px; font-size: 12px; margin-bottom: 4px; }
.nd-k { color: #8a8f99; flex: none; width: 96px; }
.nd-v { color: #374151; word-break: break-all; }
</style>
