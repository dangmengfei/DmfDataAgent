<template>
  <header class="topbar">
    <h1>运行观测</h1>
    <span class="crumb">OpenTelemetry 语义 · 本地落库（trace_records + trace_spans）</span>
    <div class="obs-actions">
      <el-dropdown trigger="click" @command="onExport">
        <el-button plain size="small">
          导出<el-icon class="ml4"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="otlp">OTLP/JSON（全量 span）</el-dropdown-item>
            <el-dropdown-item command="json">简版 JSON（轮次 + span）</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button type="primary" plain size="small" :loading="p.loading" @click="loadObsPage">
        刷新
      </el-button>
    </div>
  </header>

  <section class="obs-page">
    <!-- 当前过滤提示条（按当前会话自动筛时显示） -->
    <div v-if="p.filters.session_id" class="filter-pinned">
      <el-icon><Monitor /></el-icon>
      <span>当前会话：<b>{{ currentSessionLabel || p.filters.session_id.slice(0,10) + '…' }}</b></span>
      <el-tag v-if="store.sessionId === p.filters.session_id" type="primary" size="small" effect="plain">跟随当前对话</el-tag>
      <span class="flex-filler"></span>
      <el-button size="small" plain @click="followCurrent" :disabled="!store.sessionId">跟随当前</el-button>
      <el-button size="small" plain @click="clearSession">查看全部</el-button>
    </div>

    <!-- 过滤条 -->
    <div class="filters">
      <el-input
        v-model="filters.keyword" class="f-kw" size="small" clearable
        placeholder="按问题 / 意图关键字过滤" @keyup.enter="applyFilters"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-select v-model="filters.intent" class="f-sm" size="small" clearable placeholder="意图">
        <el-option v-for="it in intentOptions" :key="it" :label="it" :value="it" />
      </el-select>
      <el-select v-model="filters.has_error" class="f-sm" size="small" clearable placeholder="状态">
        <el-option label="成功" :value="false" />
        <el-option label="失败" :value="true" />
      </el-select>
      <el-input
        v-model="filters.session_id" class="f-session" size="small" clearable
        placeholder="会话 ID" @keyup.enter="applyFilters"
      />
      <el-button size="small" @click="applyFilters">查询</el-button>
      <el-button size="small" text @click="resetObsFilters">重置</el-button>
    </div>

    <div v-if="p.error" class="obs-empty">观测接口不可用：{{ p.error }}</div>

    <template v-else>
      <!-- KPI 汇总 -->
      <div class="kpis">
        <div class="kpi"><div class="k">对话轮次</div><div class="v">{{ n(s.total) }}</div></div>
        <div class="kpi"><div class="k">成功率</div><div class="v" :class="rateClass">{{ s.success_rate }}%</div></div>
        <div class="kpi"><div class="k">平均耗时</div><div class="v">{{ s.avg_elapsed_ms }}<i>ms</i></div></div>
        <div class="kpi"><div class="k">P95 耗时</div><div class="v">{{ s.p95_elapsed_ms }}<i>ms</i></div></div>
        <div class="kpi"><div class="k">Token 总消耗</div><div class="v">{{ n(s.total_tokens) }}</div></div>
        <div class="kpi"><div class="k">Span 总数</div><div class="v">{{ n(spanTotal) }}</div></div>
      </div>
      <div class="kpi-sub">
        输入 {{ n(s.prompt_tokens) }} · 输出 {{ n(s.completion_tokens) }} ·
        mock {{ s.mock_count }} 轮 · 失败 {{ s.error_count }} 轮 ·
        平均 {{ s.avg_span_count }} span/轮 · 最慢 {{ s.max_elapsed_ms }}ms
      </div>

      <!-- 趋势 -->
      <ChartBox v-if="trendViz" :viz="trendViz" />

      <!-- 分布 -->
      <div class="grid2">
        <div class="panel">
          <div class="panel-h">意图分布</div>
          <div class="bars">
            <template v-if="intentBars.length">
              <div v-for="b in intentBars" :key="b.name" class="bar-row">
                <span class="bar-name">{{ b.name }}</span>
                <span class="bar-track"><i :style="{ width: b.pct + '%' }"></i></span>
                <span class="bar-val">{{ b.count }}</span>
              </div>
            </template>
            <div v-else class="obs-empty sm">暂无数据</div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-h">技能分布</div>
          <div class="bars">
            <template v-if="skillBars.length">
              <div v-for="b in skillBars" :key="b.name" class="bar-row">
                <span class="bar-name">{{ b.name }}</span>
                <span class="bar-track"><i :style="{ width: b.pct + '%' }"></i></span>
                <span class="bar-val">{{ b.count }}</span>
              </div>
            </template>
            <div v-else class="obs-empty sm">暂无数据</div>
          </div>
        </div>
      </div>

      <!-- FR11 成本看板 -->
      <div class="panel">
        <div class="panel-h">
          LLM 成本（近 {{ p.costDays }} 天）
          <span class="panel-sub">
            币种 {{ cost.currency || 'CNY' }} ·
            合计 {{ money(cost.totals ? cost.totals.cost : 0) }} ·
            {{ n((cost.totals || {}).total_tokens) }} tokens ·
            {{ n((cost.totals || {}).rounds) }} 轮
          </span>
          <span class="flex-filler"></span>
          <el-select v-model="p.costGroupBy" size="small" class="f-sm" @change="setCostGroupBy">
            <el-option label="按模型" value="model" />
            <el-option label="按用户" value="user" />
            <el-option label="按租户" value="tenant" />
            <el-option label="按意图" value="intent" />
            <el-option label="按天" value="day" />
          </el-select>
          <el-select v-model="p.costDays" size="small" class="f-sm" @change="setCostDays">
            <el-option label="近 7 天" :value="7" />
            <el-option label="近 30 天" :value="30" />
            <el-option label="全部" :value="0" />
          </el-select>
        </div>

        <div v-for="a in budgetAlerts" :key="a.period" class="budget-alert" :class="a.level">
          <el-icon><WarnTriangleFilled /></el-icon>
          <span>{{ a.message }}</span>
        </div>

        <el-table v-if="costItems.length" :data="costItems" size="small" class="obs-table" :max-height="240" stripe>
          <el-table-column prop="key" label="分组" min-width="160">
            <template #default="{ row }">{{ row.key || '（未记录）' }}</template>
          </el-table-column>
          <el-table-column label="轮次" width="90" align="right">
            <template #default="{ row }">{{ n(row.rounds) }}</template>
          </el-table-column>
          <el-table-column label="输入 token" width="120" align="right">
            <template #default="{ row }">{{ n(row.prompt_tokens) }}</template>
          </el-table-column>
          <el-table-column label="输出 token" width="120" align="right">
            <template #default="{ row }">{{ n(row.completion_tokens) }}</template>
          </el-table-column>
          <el-table-column label="成本" width="130" align="right">
            <template #default="{ row }">{{ money(row.cost) }}</template>
          </el-table-column>
          <el-table-column label="占比" min-width="140">
            <template #default="{ row }">
              <span class="bar-track inline"><i :style="{ width: row.pct + '%' }"></i></span>
              <span class="bar-val">{{ row.pct }}%</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="obs-empty sm">
          暂无成本数据（需要真实 LLM 调用才会产生 token 与成本）
        </div>
      </div>

      <!-- FR11 质量看板 -->
      <div class="panel">
        <div class="panel-h">
          回答质量
          <span class="panel-sub">样本 {{ n(q.sample) }} 轮 · 近 {{ p.costDays }} 天</span>
        </div>
        <div class="kpis">
          <div class="kpi"><div class="k">有据回答率</div><div class="v" :class="rateClassOf(q.accuracy ? q.accuracy.grounded_rate : 0)">{{ (q.accuracy || {}).grounded_rate || 0 }}%</div></div>
          <div class="kpi"><div class="k">用户采纳率</div><div class="v" :class="rateClassOf(q.acceptance ? q.acceptance.rate : 0)">{{ (q.acceptance || {}).rate || 0 }}%</div></div>
          <div class="kpi"><div class="k">降级率（mock）</div><div class="v">{{ q.mock_rate || 0 }}%</div></div>
          <div class="kpi"><div class="k">空结果率</div><div class="v">{{ q.empty_rate || 0 }}%</div></div>
          <div class="kpi"><div class="k">澄清率</div><div class="v">{{ q.clarify_rate || 0 }}%</div></div>
          <div class="kpi"><div class="k">幻觉风险率</div><div class="v" :class="q.hallucination_risk_rate > 0 ? 'bad' : 'good'">{{ q.hallucination_risk_rate || 0 }}%</div></div>
        </div>
        <div class="kpi-sub">
          点赞 {{ n((q.acceptance || {}).likes) }} · 点踩 {{ n((q.acceptance || {}).dislikes) }} ·
          反馈总量 {{ n((q.acceptance || {}).feedback_total) }} ·
          失败率 {{ q.error_rate || 0 }}% ·
          <span title="有据回答率：无错误·未降级·确有数据支撑的回答占比，作为准确率的在线代理指标">口径说明</span>
        </div>

        <el-table v-if="(q.by_intent || []).length" :data="q.by_intent" size="small" class="obs-table" :max-height="220" stripe>
          <el-table-column prop="intent" label="意图" min-width="140" />
          <el-table-column prop="total" label="轮次" width="90" align="right" />
          <el-table-column label="有据回答率" width="130" align="right">
            <template #default="{ row }">{{ row.grounded_rate }}%</template>
          </el-table-column>
          <el-table-column label="失败率" width="100" align="right">
            <template #default="{ row }">{{ row.error_rate }}%</template>
          </el-table-column>
          <el-table-column label="降级率" width="100" align="right">
            <template #default="{ row }">{{ row.mock_rate }}%</template>
          </el-table-column>
          <el-table-column label="空结果率" width="110" align="right">
            <template #default="{ row }">{{ row.empty_rate }}%</template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 节点性能（span 聚合） -->
      <div class="panel">
        <div class="panel-h">
          节点性能明细
          <span class="panel-sub">按平均耗时排序 · 共 {{ spanTotal }} 条 span</span>
        </div>
        <el-table
          v-if="nodeRows.length" :data="nodeRows" size="small" class="obs-table"
          :max-height="260" stripe
        >
          <el-table-column prop="name" label="Span 名称" min-width="200" />
          <el-table-column prop="count" label="次数" width="80" align="right" />
          <el-table-column prop="errors" label="错误" width="80" align="right">
            <template #default="{ row }">
              <span :class="{ err: row.errors > 0 }">{{ row.errors }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="avg_ms" label="平均耗时" width="110" align="right">
            <template #default="{ row }">{{ row.avg_ms }} ms</template>
          </el-table-column>
          <el-table-column prop="max_ms" label="最大耗时" width="110" align="right">
            <template #default="{ row }">{{ row.max_ms }} ms</template>
          </el-table-column>
        </el-table>
        <div v-else class="obs-empty sm">暂无 span 数据，去对话页提问后回来查看</div>
      </div>

      <!-- 轮次明细 -->
      <div class="panel">
        <div class="panel-h">
          对话轮次明细
          <span class="panel-sub">共 {{ n(p.total) }} 轮（点击行查看该轮 span 瀑布）</span>
        </div>
        <el-table
          v-if="p.rounds.length" :data="p.rounds" size="small" class="obs-table"
          :max-height="420" stripe @row-click="openDetail"
        >
          <el-table-column prop="saved_at" label="时间" width="160">
            <template #default="{ row }">{{ fmtTime(row.saved_at) }}</template>
          </el-table-column>
          <el-table-column prop="question" label="问题" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.question || '—' }}</template>
          </el-table-column>
          <el-table-column prop="intent" label="意图" width="110">
            <template #default="{ row }">{{ row.intent || '未识别' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <span class="tag" :class="row.has_error ? 'err' : 'ok'">
                {{ row.has_error ? '失败' : '成功' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="90" align="right">
            <template #default="{ row }">{{ row.elapsed_ms == null ? '—' : row.elapsed_ms + 'ms' }}</template>
          </el-table-column>
          <el-table-column label="Token" width="100" align="right">
            <template #default="{ row }">
              {{ row.total_tokens == null ? '—' : n(row.total_tokens) }}
            </template>
          </el-table-column>
          <el-table-column label="Span" width="70" align="right">
            <template #default="{ row }">{{ row.span_count || 0 }}</template>
          </el-table-column>
          <el-table-column label="Trace ID" width="150">
            <template #default="{ row }">
              <span class="mono" :title="row.trace_id || ''">{{ shortId(row.trace_id) }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="obs-empty sm">暂无记录，去对话页提问后回来查看</div>

        <el-pagination
          v-if="p.total > p.pageSize"
          class="pager" small background layout="prev, pager, next, total"
          :current-page="p.page" :page-size="p.pageSize" :total="p.total"
          @current-change="setObsPage"
        />
      </div>
    </template>
  </section>

  <!-- 单轮下钻：OTel span 瀑布 -->
  <el-drawer
    v-model="detailVisible" :title="detailTitle" size="min(720px, 96vw)"
    @closed="onDetailClosed"
  >
    <div v-if="p.detailLoading" class="obs-empty">加载中…</div>
    <div v-else-if="!p.detail" class="obs-empty">未选择轮次</div>
    <div v-else-if="p.detail.error" class="obs-empty">加载失败：{{ p.detail.error }}</div>
    <template v-else>
      <div class="d-meta">
        <span class="mono">trace_id {{ p.detail.trace_id || (p.detail.otel && p.detail.otel.trace_id) || '—' }}</span>
        <span v-if="detailSummary" class="d-sum">{{ detailSummary }}</span>
      </div>

      <div class="panel-h">Span 瀑布（{{ (p.detail.spans || []).length }}）</div>
      <div v-if="!waterfall.length" class="obs-empty sm">该轮无 span 记录</div>
      <div v-else class="wf">
        <div v-for="w in waterfall" :key="w.span_id" class="wf-row" :class="{ 'wf-child': w.depth > 0 }">
          <div class="wf-head">
            <span class="wf-name" :title="w.name">{{ w.name }}</span>
            <span class="wf-tags">
              <span class="tag kind">{{ w.kind }}</span>
              <span class="tag" :class="w.status_code === 'ERROR' ? 'err' : 'ok'">{{ w.status_code }}</span>
              <span class="wf-dur">{{ w.duration_ms == null ? '—' : w.duration_ms + 'ms' }}</span>
            </span>
          </div>
          <div class="wf-track">
            <i :style="{ left: w.left + '%', width: w.width + '%' }" :class="w.status_code === 'ERROR' ? 'err' : ''"></i>
          </div>
          <div v-if="w.attrText" class="wf-attrs">{{ w.attrText }}</div>
        </div>
      </div>

      <div class="panel-h">本节轮次要素</div>
      <pre class="d-json">{{ detailTraceText }}</pre>
    </template>
  </el-drawer>

  <!-- FR13 导出预览（含水印 + 截断告知） -->
  <ExportPreviewDialog v-model="exportVisible" :payload="exportPayload" kind="obs" />
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, Search, WarnTriangleFilled } from '@element-plus/icons-vue'
import ChartBox from './ChartBox.vue'
import {
  store, loadObsPage, setObsFilter, setObsPage, resetObsFilters,
  loadObsRoundDetail, closeObsDetail, setCostGroupBy, setCostDays
} from '../store'
import { obsExport } from '../utils/api'
import ExportPreviewDialog from './ExportPreviewDialog.vue'

/* 顶部"显示当前会话"快捷：当 sidebar 导航到 obs 时若携带 session_id，提供一键清空回全量 */
const currentSessionLabel = computed(() => {
  const sid = store.obsPage.filters.session_id
  if (!sid) return ''
  const sess = (store.sessions || []).find(s => s.id === sid)
  return sess ? sess.title : sid.slice(0, 10) + '…'
})
async function followCurrent() {
  if (!store.sessionId) return
  await setObsFilter({ session_id: store.sessionId, intent: '', has_error: '', keyword: '' })
}
async function clearSession() {
  await setObsFilter({ session_id: '', intent: '', has_error: '', keyword: '' })
}

const p = computed(() => store.obsPage)
const filters = computed(() => store.obsPage.filters)

const EMPTY = {
  total: 0, mock_count: 0, error_count: 0, avg_elapsed_ms: 0, prompt_tokens: 0,
  completion_tokens: 0, total_tokens: 0, intent_distribution: {}, skill_distribution: {},
  success_rate: 0, error_rate: 0, p50_elapsed_ms: 0, p95_elapsed_ms: 0,
  max_elapsed_ms: 0, avg_span_count: 0, token_rounds: 0, daily: []
}
const s = computed(() => p.value.summary || EMPTY)
const spanTotal = computed(() => (p.value.spanStats && p.value.spanStats.total) || 0)
const nodeRows = computed(() => (p.value.spanStats && p.value.spanStats.nodes) || [])
const rateClass = computed(() => {
  const r = s.value.success_rate
  if (!s.value.total) return ''
  return r >= 99 ? 'good' : r >= 90 ? 'warn' : 'bad'
})

function n(v) {
  return (v || 0).toLocaleString('zh-CN')
}
function fmtTime(t) {
  if (!t) return '—'
  const d = new Date(t)
  return isNaN(d.getTime()) ? String(t) : d.toLocaleString('zh-CN')
}
function shortId(id) {
  return id ? String(id).slice(0, 12) + '…' : '—'
}

/* ---- 过滤 ---- */
const intentOptions = computed(() => Object.keys(s.value.intent_distribution || {}))
function applyFilters() {
  loadObsPage()
}

/* ---- FR11 成本 / 质量看板 ---- */
const EMPTY_COST = { currency: 'CNY', totals: { cost: 0, rounds: 0, total_tokens: 0 }, items: [], budget: { alerts: [] } }
const EMPTY_QUALITY = {
  sample: 0, accuracy: { grounded_rate: 0 }, acceptance: { rate: 0, likes: 0, dislikes: 0, feedback_total: 0 },
  error_rate: 0, mock_rate: 0, empty_rate: 0, clarify_rate: 0, hallucination_risk_rate: 0, by_intent: []
}
const cost = computed(() => p.value.cost || EMPTY_COST)
const q = computed(() => p.value.quality || EMPTY_QUALITY)
const budgetAlerts = computed(() => (cost.value.budget || {}).alerts || [])
const costItems = computed(() => {
  const items = cost.value.items || []
  const max = Math.max(...items.map((i) => i.cost || 0), 0) || 1
  return items.map((i) => ({ ...i, pct: Math.round(((i.cost || 0) / max) * 100) }))
})
function money(v) {
  const n = Number(v || 0)
  if (!n) return '0'
  return n < 0.01 ? n.toFixed(6) : n.toFixed(4)
}
function rateClassOf(rate) {
  const r = Number(rate || 0)
  return r >= 95 ? 'good' : r >= 80 ? 'warn' : 'bad'
}

/* ---- 分布条 ---- */
function toBars(dist) {
  const entries = Object.entries(dist || {})
  if (!entries.length) return []
  const max = Math.max(...entries.map(([, v]) => v)) || 1
  return entries.map(([name, count]) => ({ name, count, pct: Math.round((count / max) * 100) }))
}
const intentBars = computed(() => toBars(s.value.intent_distribution))
const skillBars = computed(() => toBars(s.value.skill_distribution))

/* ---- 趋势图（ChartBox 复用） ---- */
const trendViz = computed(() => {
  const daily = s.value.daily || []
  if (daily.length < 2) return null
  return {
    type: 'line',
    title: '按天趋势（轮次 / 平均耗时）',
    x: daily.map((d) => d.date.slice(5)),
    series: [
      { name: '轮次', data: daily.map((d) => d.total) },
      { name: '平均耗时', data: daily.map((d) => d.avg_elapsed_ms) }
    ],
    unit: ''
  }
})

/* ---- 导出（FR13：带 Authorization 取 JSON → 预览（含水印）→ 落盘） ----
   旧实现是 window.open(URL)：新窗口不携带 Authorization 头，鉴权开启时必然 401。
   与 /slang/export、/fewshots/export 统一走「req 取回 → 前端 Blob 落盘」。 */
const exportVisible = ref(false)
const exportPayload = ref(null)
const exporting = ref(false)

async function onExport(format) {
  /* download 参数不再需要：落盘改由前端 Blob 完成 */
  const params = { format, limit: 5000 }
  const f = p.value.filters
  if (f.session_id) params.session_id = f.session_id
  exporting.value = true
  try {
    exportPayload.value = await obsExport(params)
    exportVisible.value = true
  } catch (e) {
    ElMessage.error('导出失败：' + (e && e.message ? e.message : e))
  } finally {
    exporting.value = false
  }
}

/* ---- 单轮下钻（span 瀑布） ---- */
const detailVisible = ref(false)
const detailTitle = computed(() => {
  const d = p.value.detail
  if (!d) return '单轮观测详情'
  const r = p.value.rounds.find((x) => x.round_id === d.round_id)
  return '单轮观测 · ' + (r && r.intent ? r.intent : d.round_id)
})
const detailSummary = computed(() => {
  const d = p.value.detail
  if (!d) return ''
  const otel = d.otel || {}
  const nsp = (otel.node_spans || []).length
  return `${(d.spans || []).length} span · ${nsp} 节点 · ${otel.service_name || ''}`
})
const detailTraceText = computed(() => {
  const d = p.value.detail
  if (!d || !d.trace) return '—'
  const t = { ...d.trace }
  delete t.otel
  return JSON.stringify(t, null, 2)
})

/* 瀑布：以最早的 start_time 为基准换算左右偏移与宽度（占全程百分比） */
const waterfall = computed(() => {
  const d = p.value.detail
  if (!d || !d.spans || !d.spans.length) return []
  const spans = d.spans
    .map((x) => ({ ...x, _s: x.start_time ? new Date(x.start_time).getTime() : null }))
    .filter((x) => x._s != null)
  if (!spans.length) return []
  const t0 = Math.min(...spans.map((x) => x._s))
  const t1 = Math.max(...spans.map((x) => x._s + (x.duration_ms || 0)))
  const total = Math.max(t1 - t0, 1)
  // root span 的 span_id，用于判定层级（子 span 缩进）
  const root = spans.find((x) => !x.parent_span_id)
  const rootId = root ? root.span_id : null
  return spans
    .slice()
    .sort((a, b) => a._s - b._s)
    .map((x) => {
      const off = x._s - t0
      const dur = x.duration_ms || 0
      const attrs = x.attributes || {}
      const attrText = Object.entries(attrs)
        .slice(0, 4)
        .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join('|') : v}`)
        .join('  ')
      return {
        ...x,
        depth: rootId && x.span_id !== rootId ? 1 : 0,
        left: Math.max(0, Math.min(99, (off / total) * 100)),
        width: Math.max(0.6, Math.min(100 - (off / total) * 100, (dur / total) * 100)),
        attrText
      }
    })
})

function openDetail(row) {
  detailVisible.value = true
  loadObsRoundDetail(row.session_id, row.round_id)
}
function onDetailClosed() {
  closeObsDetail()
}
</script>

<style scoped>
.obs-actions{margin-left:auto;display:flex;align-items:center;gap:8px}
.ml4{margin-left:4px}
.obs-page{flex:1;overflow-y:auto;padding:14px 24px 28px}
.filters{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.f-kw{width:260px}
.f-sm{width:120px}
.f-session{width:220px}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--border-soft);border-radius:12px;padding:10px 12px;box-shadow:var(--shadow)}
.kpi .k{font-size:11px;color:var(--muted)}
.kpi .v{font-size:20px;font-weight:600;color:var(--text);margin-top:3px;word-break:break-all}
.kpi .v i{font-size:11px;font-weight:400;color:var(--muted);margin-left:2px;font-style:normal}
.kpi .v.good{color:#00a325}
.kpi .v.warn{color:#d98b00}
.kpi .v.bad{color:#d93026}
.kpi-sub{font-size:11px;color:var(--muted);margin:8px 0 4px}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
.panel{background:var(--card);border:1px solid var(--border-soft);border-radius:12px;padding:10px 12px;margin-top:12px;box-shadow:var(--shadow)}
.panel-h{font-size:12px;font-weight:600;color:var(--text-2);margin-bottom:8px;display:flex;align-items:center;gap:8px}
.panel-sub{font-weight:400;color:var(--muted);font-size:11px}

.bars{display:flex;flex-direction:column;gap:6px}
.bar-row{display:flex;align-items:center;gap:8px;font-size:12px}
.bar-name{width:96px;flex:none;color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar-track{flex:1;height:8px;background:var(--border-soft);border-radius:5px;overflow:hidden}
.bar-track i{display:block;height:100%;background:linear-gradient(90deg,#8bb4ff,#3370ff);border-radius:5px}
.bar-val{width:40px;text-align:right;color:var(--muted)}
.bar-track.inline{display:inline-block;width:96px;vertical-align:middle;margin-right:6px}

.budget-alert{display:flex;align-items:center;gap:6px;font-size:12px;padding:6px 10px;border-radius:8px;margin-bottom:8px}
.budget-alert.warn{background:#FFF7E8;color:#a15c00;border:1px solid #ffd591}
.budget-alert.exceeded{background:#FEF1F1;color:#d93026;border:1px solid #ffb3b3}

.obs-table{width:100%}
.obs-table :deep(.el-table__row){cursor:pointer}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--text-2)}
.obs-empty{font-size:12px;color:var(--muted);padding:14px 4px}
.obs-empty.sm{padding:8px 2px}
.filter-pinned{display:flex;align-items:center;gap:10px;padding:8px 14px;background:#EAF1FF;border:1px solid #c2d6ff;color:var(--primary);border-radius:10px;font-size:12px}
.filter-pinned b{font-family:ui-monospace,Menlo,monospace;color:#0050b3}
.flex-filler{flex:1}
.tag{border-radius:8px;padding:0 6px;font-size:10px;line-height:16px}
.tag.ok{color:#00a325;background:#E8F5EE}
.tag.err{color:#d93026;background:#FEF1F1}
.tag.kind{color:var(--text-2);background:var(--border-soft)}
.pager{margin-top:10px;justify-content:flex-end}

.d-meta{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--muted);margin-bottom:10px}
.d-sum{color:var(--text-2)}
.wf{display:flex;flex-direction:column;gap:9px}
.wf-row{border-left:2px solid transparent;padding-left:0}
.wf-child{padding-left:16px;border-left:2px solid var(--border-soft)}
.wf-head{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:12px}
.wf-name{font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wf-tags{display:flex;align-items:center;gap:6px;flex:none}
.wf-dur{font-size:11px;color:var(--muted);font-family:ui-monospace,Menlo,monospace}
.wf-track{position:relative;height:10px;background:var(--border-soft);border-radius:6px;margin-top:4px;overflow:hidden}
.wf-track i{position:absolute;top:0;height:100%;background:linear-gradient(90deg,#8bb4ff,#3370ff);border-radius:6px}
.wf-track i.err{background:linear-gradient(90deg,#ffb3b3,#f53f3f)}
.wf-attrs{font-size:10px;color:var(--muted);margin-top:3px;font-family:ui-monospace,Menlo,monospace;word-break:break-all}
.d-json{margin-top:6px;padding:10px 12px;background:var(--bg);border:1px solid var(--border-soft);border-radius:8px;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--text-2);line-height:1.6;max-height:280px;overflow:auto;white-space:pre-wrap;word-break:break-all}
</style>
