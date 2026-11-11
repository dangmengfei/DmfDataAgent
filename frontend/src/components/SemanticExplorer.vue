<template>
  <header class="topbar">
    <h1>语义查询</h1>
    <span class="crumb">MetricFlow 编译 → StarRocks 执行（SQL/结果回显）</span>
    <div class="actions">
      <el-tag :type="srHealthTag.type" size="small">{{ srHealthTag.text }}</el-tag>
      <span v-if="srLatency" class="sr-latency">延迟 {{ srLatency }}</span>
      <el-button size="small" plain :loading="semExp.loadingMeta" @click="refreshMeta">
        刷新元数据
      </el-button>
      <el-button size="small" text @click="onReset">重置</el-button>
    </div>
  </header>

  <section class="sem-page">
    <!-- 表单 -->
    <div class="form-card">
      <el-form label-position="top" size="default">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="指标（多选）">
              <el-select
                v-model="form.metrics" multiple filterable collapse-tags collapse-tags-tooltip
                placeholder="选择业务指标（支持别名）" style="width: 100%"
                :loading="semExp.loadingMeta" @change="onFormChange"
              >
                <el-option
                  v-for="m in semExp.metrics" :key="m.name" :label="m.name" :value="m.name"
                >
                  <span class="op-name">{{ m.name }}</span>
                  <span class="op-sub">{{ m.type }} · {{ m.unit || '-' }} · 别名 {{ (m.aliases || []).slice(0, 3).join(' / ') }}</span>
                </el-option>
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="分组维度（多选，含时间维度）">
              <el-select
                v-model="form.groupBy" multiple filterable collapse-tags collapse-tags-tooltip
                placeholder="按哪些维度分组" style="width: 100%"
                :loading="semExp.loadingMeta" @change="onFormChange"
              >
                <el-option
                  v-for="d in semExp.dimensions" :key="d.name"
                  :label="d.name + (d.type === 'time' ? ' (时间)' : '')" :value="d.name"
                >
                  <span class="op-name">{{ d.name }}</span>
                  <span class="op-sub">expr {{ d.expr }} · 模型 {{ (d.models || []).join('+') }}</span>
                </el-option>
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="16">
          <el-col :span="6">
            <el-form-item label="开始日期">
              <el-date-picker
                v-model="form.start" type="date" value-format="YYYY-MM-DD"
                placeholder="start" style="width: 100%" @change="onFormChange"
              />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="结束日期">
              <el-date-picker
                v-model="form.end" type="date" value-format="YYYY-MM-DD"
                placeholder="end" style="width: 100%" @change="onFormChange"
              />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="粒度">
              <el-select v-model="form.granularity" style="width: 100%" @change="onFormChange">
                <el-option v-for="g in granules" :key="g" :label="g" :value="g" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="限制行数">
              <el-input-number v-model="form.limit" :min="1" :max="10000" style="width: 100%" @change="onFormChange" />
            </el-form-item>
          </el-col>
        </el-row>

        <!-- 高级选项：同环比 / 维度过滤 / 二次分析（默认折叠，避免基础表单过载） -->
        <el-collapse v-model="advOpen" class="adv-collapse">
          <el-collapse-item name="adv">
            <template #title>
              <span class="adv-title">
                高级选项：同环比 · 维度过滤 · 二次分析
                <el-tag v-if="advActiveCount" size="small" type="warning" effect="light">
                  {{ advActiveCount }} 项已启用
                </el-tag>
              </span>
            </template>

            <el-row :gutter="16">
              <el-col :span="6">
                <el-form-item label="同环比">
                  <el-switch
                    v-model="form.compareOn" active-text="开启" @change="onFormChange"
                  />
                </el-form-item>
              </el-col>
              <el-col :span="6">
                <el-form-item label="对比类型（可多选）">
                  <el-select
                    v-model="form.compareTypes" multiple collapse-tags collapse-tags-tooltip
                    :disabled="!form.compareOn"
                    style="width: 100%" @change="onFormChange"
                  >
                    <el-option v-for="p in popTypes" :key="p.value" :label="p.label" :value="p.value" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="二次分析（作用于已选的全部指标）">
                  <div class="adv-inline">
                    <el-select
                      v-model="form.advAgg" clearable placeholder="聚合覆盖（默认 sum）"
                      style="width: 200px" @change="onFormChange"
                    >
                      <el-option label="avg 底表行均值" value="avg" />
                      <el-option label="max 期间峰值" value="max" />
                      <el-option label="min 期间低点" value="min" />
                    </el-select>
                    <el-checkbox v-model="form.advProportion" @change="onFormChange">
                      占比 __占比
                    </el-checkbox>
                    <el-checkbox v-model="form.advRank" @change="onFormChange">
                      排名 __排名
                    </el-checkbox>
                    <el-select
                      v-model="form.rankScope" :disabled="!form.advRank"
                      style="width: 150px" @change="onFormChange"
                    >
                      <el-option label="全局排名" value="global" />
                      <el-option label="组内排名（按时间）" value="group" />
                    </el-select>
                  </div>
                  <div class="dim adv-hint">
                    说明：聚合覆盖作用于底表行（非按天二次聚合）。余额类指标在月粒度下 SUM
                    得到的是"日积数"（余额×天数），除以当月天数才是日均余额。
                    组内排名需在分组里包含时间维度（如"统计日期"），否则退化为全局排名。
                  </div>
                </el-form-item>
              </el-col>
            </el-row>

            <el-form-item label="维度过滤（多值用英文逗号分隔）">
              <div class="filter-rows">
                <div v-for="(row, idx) in form.filters" :key="idx" class="filter-row">
                  <el-select
                    v-model="row.field" filterable placeholder="维度"
                    style="width: 170px" @change="onFormChange"
                  >
                    <el-option v-for="d in filterableDims" :key="d" :label="d" :value="d" />
                  </el-select>
                  <el-select v-model="row.operator" style="width: 118px" @change="onFormChange">
                    <el-option v-for="op in operators" :key="op.value" :label="op.label" :value="op.value" />
                  </el-select>
                  <el-input
                    v-model="row.values" placeholder="取值，如：上海分行,北京分行"
                    style="width: 300px" @change="onFormChange"
                  />
                  <el-button text type="danger" @click="removeFilter(idx)">删除</el-button>
                </div>
                <div class="filter-row">
                  <el-button size="small" plain @click="addFilter">+ 添加过滤条件</el-button>
                  <span class="dim adv-hint">
                    过滤维度需为所选指标所在数据域提供的维度，否则后端返回 400
                  </span>
                </div>
              </div>
            </el-form-item>
          </el-collapse-item>
        </el-collapse>

        <div class="form-actions">
          <el-button type="primary" plain :loading="semExp.loadingRun === 'compile'" @click="runCompile">
            <el-icon><Document /></el-icon>仅编译 SQL
          </el-button>
          <el-tooltip
            :content="!canQuery ? '当前角色无执行语义查询权限（semantic:query），请联系管理员' : (srHealthy ? '已连通 ' + srHealthHint : 'StarRocks 未连通：' + srHealthHint + '（仍可点击，错误将由后端提示）')"
            placement="top"
          >
            <el-button
              type="primary"
              :loading="semExp.loadingRun === 'query'"
              :disabled="!canQuery"
              :icon="srHealthy ? 'VideoPlay' : undefined"
              @click="runQuery"
            >
              执行查询
            </el-button>
          </el-tooltip>
          <span class="quick-fill">
            <el-link type="primary" underline="never" @click="quickFillLast7">最近 7 天</el-link>
            <el-divider direction="vertical" />
            <el-link type="primary" underline="never" @click="quickFillLast30">最近 30 天</el-link>
            <el-divider direction="vertical" />
            <el-link type="primary" underline="never" @click="quickFillFull">全量数据范围</el-link>
          </span>
        </div>
      </el-form>
    </div>

    <el-alert
      v-if="semExp.error" :title="semExp.error" type="error" show-icon :closable="false"
      class="alert"
    />

    <!-- 结果区 -->
    <div ref="resultRef" v-if="semExp.compile || semExp.run" class="result">
      <el-card shadow="never" class="sql-card">
        <template #header>
          <div class="card-hd">
            <span>
              <el-icon><Document /></el-icon> 生成 SQL
              <el-tag v-if="semExp.run" :type="semExp.run.error ? 'danger' : 'success'" size="small" class="ml-1">
                <template v-if="semExp.run.error">执行失败</template>
                <template v-else>{{ semExp.run.row_count }} 行 · {{ semExp.run.elapsed_ms }}ms</template>
              </el-tag>
            </span>
            <span class="dim">
              模型：{{ sqlMeta.models }} · 列：{{ sqlMeta.columns }} ·
              <span class="scroll-tip">↔ 左右滑动</span>
            </span>
          </div>
        </template>
        <div class="sql-block-wrap">
          <pre ref="sqlBlockRef" class="sql-block">{{ sqlText }}</pre>
          <div class="sql-actions">
            <el-tag v-if="semExp.compile" size="small" type="info">engine: {{ semExp.compile.engine }}</el-tag>
            <el-tag v-if="groupColsText" size="small" type="info">分组键: {{ groupColsText }}</el-tag>
            <el-tag v-if="metricColsText" size="small" type="info">指标列: {{ metricColsText }}</el-tag>
            <el-button size="small" plain @click="copySql">
              <el-icon><CopyDocument /></el-icon>复制 SQL
            </el-button>
            <el-button size="small" plain @click="openFullscreen">
              <el-icon><FullScreen /></el-icon>全屏查看
            </el-button>
          </div>
        </div>
      </el-card>

      <el-card
        v-if="semExp.run" shadow="never" class="result-card"
      >
        <template #header>
          <div class="card-hd">
            <span>
              <el-icon><DataAnalysis /></el-icon> 查询结果
              <el-tag v-if="semExp.run.error" size="small" type="danger">失败</el-tag>
              <el-tag v-else size="small" type="success">
                {{ semExp.run.row_count }} 行 · {{ semExp.run.elapsed_ms }}ms
              </el-tag>
              <el-tag v-if="semExp.run.cached" size="small" type="warning">缓存命中</el-tag>
            </span>
            <span class="dim">
              <span v-if="!semExp.run.error">列数 {{ semExp.run.columns.length }}</span>
              <el-button v-if="!semExp.run.error && semExp.run.rows.length" size="small" text @click="exportCsv">
                <el-icon><Download /></el-icon>导出 CSV
              </el-button>
            </span>
          </div>
        </template>
        <div v-if="semExp.run && semExp.run.caliber" class="caliber-line">
          <div class="sc-line">
            <span class="sc-label">口径说明</span>{{ semExp.run.caliber.watermark }}
          </div>
          <details v-if="caliberRows(semExp.run.caliber).length" class="sc-more">
            <summary>口径明细<span class="sc-ver" v-if="semExp.run.caliber.version"> · {{ semExp.run.caliber.version }}</span></summary>
            <div class="sc-detail">
              <div v-for="(r, i) in caliberRows(semExp.run.caliber)" :key="'sc' + i" class="sc-row">
                <span class="sc-k">{{ r.k }}</span>
                <span class="sc-v">{{ r.v }}</span>
              </div>
            </div>
          </details>
        </div>
        <el-alert
          v-if="semExp.run.error" :title="semExp.run.error" type="error"
          show-icon :closable="false"
        />
        <template v-else-if="semExp.run.rows.length === 0">
          <el-empty
            description="当前时间范围 / 维度组合下没有数据"
            :image-size="100"
          >
            <template #description>
              <p>当前时间范围 / 维度组合下没有数据</p>
              <p class="dim">建议：放宽时间窗、确认所选维度是否有值、调整粒度</p>
              <p v-if="dataAsOf" class="dim">
                数据最新截至 <b>{{ dataAsOf }}</b>，可一键定位到最新可用数据
              </p>
            </template>
            <el-button v-if="dataAsOf" type="primary" plain @click="jumpToLatest">
              定位到最新数据
            </el-button>
          </el-empty>
        </template>
        <el-table
          v-else :data="semExp.run.rows" stripe border size="small" max-height="540"
        >
          <el-table-column
            v-for="c in semExp.run.columns" :key="c" :prop="c" :label="c"
            :formatter="row => fmtCell(row[c])" min-width="120" show-overflow-tooltip
          />
        </el-table>
      </el-card>
    </div>

    <el-empty
      v-else-if="!semExp.loadingRun"
      description="选择指标和维度，点击「编译 SQL」或「执行查询」开始"
    />

    <!-- SQL 全屏查看 Dialog -->
    <el-dialog
      v-model="fullscreenOpen" title="SQL 全屏查看" fullscreen
      :show-close="true" :modal="true"
    >
      <pre class="sql-block fullscreen">{{ sqlText }}</pre>
      <template #footer>
        <el-button @click="copySql">复制 SQL</el-button>
        <el-button type="primary" @click="fullscreenOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 语义层来源 / 发布快照 / 审批流（混合方案）：
         放在页面底部——查询是本页主任务，来源信息属于运维诊断，按需查看即可 -->
    <SemanticSourcePanel />
  </section>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  CopyDocument,
  DataAnalysis,
  Document,
  Download,
  FullScreen,
  Warning
} from '@element-plus/icons-vue'
import {
  store,
  loadSemanticMeta,
  runSemanticCompile,
  runSemanticQuery,
  resetSemanticForm,
  hasPerm
} from '../store'
// 显式导入（该组件也可被自动导入，此处为了可读性与 lint 友好）
import SemanticSourcePanel from './SemanticSourcePanel.vue'
import { caliberRows } from '../utils/caliber'

/* 语义查询执行需 semantic:query（viewer 仅可读元信息）；后端 /semantic/query 亦守卫 */
const canQuery = computed(() => hasPerm('semantic:query'))

const semExp = store.semExp
const form = semExp.form
const granules = ['day', 'week', 'month', 'quarter', 'year']

/* ---- 高级选项：同环比 / 维度过滤 / 二次分析 ---- */
const advOpen = ref([])
const popTypes = [
  { value: 'yoy', label: '同比 yoy（去年同期）' },
  { value: 'mom', label: '环比 mom（上一月）' },
  { value: 'wow', label: '周环比 wow（上一周）' },
  { value: 'qoq', label: '季环比 qoq（上一季）' },
  { value: 'pop', label: '上一周期 pop' }
]
const operators = [
  { value: 'in', label: 'in 属于' },
  { value: 'not in', label: 'not in 不属于' },
  { value: '=', label: '= 等于' },
  { value: '!=', label: '!= 不等于' },
  { value: '>', label: '> 大于' },
  { value: '<', label: '< 小于' },
  { value: 'like', label: 'like 包含' }
]
const filterableDims = computed(() => (semExp.dimensions || []).map(d => d.name))
const advActiveCount = computed(() => {
  let n = 0
  if (form.compareOn) n += 1
  if ((form.filters || []).length) n += 1
  if (form.advAgg) n += 1
  if (form.advProportion) n += 1
  if (form.advRank) n += 1
  return n
})
function addFilter() {
  if (!Array.isArray(form.filters)) form.filters = []
  form.filters.push({ field: '', operator: 'in', values: '' })
}
function removeFilter(idx) {
  form.filters.splice(idx, 1)
  onFormChange()
}

const resultRef = ref(null)
const sqlBlockRef = ref(null)
const fullscreenOpen = ref(false)

const srHealthy = computed(() => !!(semExp.health && semExp.health.reachable))
const srHealthHint = computed(() => {
  const h = semExp.health || {}
  return h.detail || h.mode || '未配置'
})
// 数据源徽标三态：在线（且底表正常）/ 在线但有告警（空表·陈旧·探测失败）/ 未连通。
// 心跳只能判断"进程在线"，深度探针的 summary 才能发现"在线但某张底表空了/数据陈旧"。
const srHealthTag = computed(() => {
  const h = semExp.health
  if (!h) return { type: 'info', text: '数据源检测中…' }
  if (!h.reachable) return { type: 'warning', text: '数据源未连通' }
  const s = h.summary || {}
  if (h.stale) {
    const parts = []
    if (s.empty_tables) parts.push(`空表 ${s.empty_tables}`)
    if (s.stale_tables) parts.push(`陈旧 ${s.stale_tables}`)
    if (s.failed_tables) parts.push(`探测失败 ${s.failed_tables}`)
    return { type: 'warning', text: `数据源在线·${parts.join('·') || '需关注'}` }
  }
  return { type: 'success', text: '数据源在线' }
})
// 数据源延迟（ms）：在线时随徽标展示，帮助识别"在线但很慢"。
const srLatency = computed(() => {
  const h = semExp.health
  return h && h.reachable && h.latency_ms != null ? `${h.latency_ms}ms` : ''
})
// 数据最新截至日：后端 /semantic/health 探查各语义模型时间维度最大值返回。
// 默认时间窗与空结果提示都优先以它为准，避免"最近 N 天"落在数据截断区之外而取空。
const dataAsOf = computed(() => {
  const h = semExp.health
  return (h && h.data_as_of) || ''
})

// 初次进入若时间窗为空，默认填最近 30 天（避免初学者点执行查询拿不到结果）。
// 注意：必须放在 dataAsOf 等被 quickFillLast30 依赖的 const 声明之后——
// watch 的 immediate 回调在 setup 期同步执行，若此处引用了尚未初始化的 const 会命中 TDZ。
watch(() => [form.start, form.end], ([s, e]) => {
  if (!s && !e && !semExp.metrics.length) {
    quickFillLast30()
  }
}, { immediate: true })

const sqlText = computed(() => {
  if (semExp.run && semExp.run.sql) return semExp.run.sql
  if (semExp.compile && semExp.compile.sql) return semExp.compile.sql
  return ''
})
const sqlMeta = computed(() => {
  const c = semExp.compile || (semExp.run && { columns: semExp.run.columns })
  if (!c) return { models: '-', columns: '-' }
  return {
    models: (c.models || []).join(' / ') || '-',
    columns: (c.columns || []).join(' / ') || '-'
  }
})
const groupColsText = computed(() => {
  const c = semExp.compile
  return c ? (c.group_columns || []).join(' / ') : ''
})
const metricColsText = computed(() => {
  const c = semExp.compile
  return c ? (c.metric_columns || []).join(' / ') : ''
})

function fmtCell(v) {
  if (v == null) return ''
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return v.toLocaleString()
    return Number(v).toLocaleString(undefined, { maximumFractionDigits: 4 })
  }
  if (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v)) return v
  return String(v)
}

function todayStr() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
function daysAgo(n) {
  const d = new Date()
  d.setDate(d.getDate() - n)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
function daysBefore(dateStr, n) {
  const d = new Date(`${dateStr}T00:00:00`)
  d.setDate(d.getDate() - n)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}
function quickFillLast7() {
  const end = dataAsOf.value || todayStr()
  form.end = end
  form.start = daysBefore(end, 6)
  form.granularity = 'day'
  onFormChange()
}
function quickFillLast30() {
  // 以数据最新截至日为终点（取不到时回落今天），避免默认窗落在数据截断区之外取空
  const end = dataAsOf.value || todayStr()
  form.end = end
  form.start = daysBefore(end, 29)
  form.granularity = 'day'
  onFormChange()
}
function jumpToLatest() {
  if (!dataAsOf.value) return
  form.end = dataAsOf.value
  form.start = daysBefore(dataAsOf.value, 29)
  form.granularity = 'day'
  onFormChange()
  runQuery()
}
function quickFillFull() {
  form.start = '2025-08-01'
  form.end = todayStr()
  form.granularity = 'day'
  onFormChange()
}

function refreshMeta() {
  loadSemanticMeta(true)
}
function onReset() {
  resetSemanticForm()
}
async function runCompile() {
  await runSemanticCompile()
  await nextTick()
  scrollToResult()
}
async function runQuery() {
  await runSemanticQuery()
  await nextTick()
  scrollToResult()
}
function scrollToResult() {
  const el = resultRef.value
  if (!el) return
  // 只滚动页面自身的容器；不要用 scrollIntoView —— 它在找不到可滚祖先时会去滚 document，
  // 导致整页（含侧边栏）上移，而且 body{overflow:hidden} 下没有滚动条，用户无法滚回来。
  const scroller = el.closest('.sem-page')
  if (!scroller) return
  scroller.scrollTo({ top: Math.max(0, el.offsetTop - 12), behavior: 'smooth' })
}
function onFormChange() {
  // 改了表单就清旧结果，避免旧 SQL/旧数据误导
  semExp.compile = null
  semExp.run = null
  semExp.error = ''
}
async function copySql() {
  if (!sqlText.value) return
  try {
    await navigator.clipboard.writeText(sqlText.value)
    ElMessage({ message: 'SQL 已复制到剪贴板', type: 'success' })
  } catch (e) {
    ElMessage({ message: '复制失败：' + (e.message || e), type: 'error' })
  }
}
function openFullscreen() {
  fullscreenOpen.value = true
}
function exportCsv() {
  if (!semExp.run || !semExp.run.rows.length) return
  const cols = semExp.run.columns
  const esc = v => {
    const s = v == null ? '' : String(v)
    if (s.includes(',') || s.includes('"') || s.includes('\n')) {
      return '"' + s.replace(/"/g, '""') + '"'
    }
    return s
  }
  const lines = [cols.join(',')]
  for (const row of semExp.run.rows) {
    lines.push(cols.map(c => esc(row[c])).join(','))
  }
  const csv = '\uFEFF' + lines.join('\n') // BOM 让 Excel 正确识别 UTF-8
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `semantic-${Date.now()}.csv`
  a.click()
  URL.revokeObjectURL(url)
  ElMessage({ message: '已导出 CSV', type: 'success' })
}
</script>

<style scoped>
.topbar {
  display: flex; align-items: center; gap: 12px;
  padding: 0 24px; height: 56px; border-bottom: 1px solid var(--border);
  background: #fff;
}
.topbar h1 { font-size: 18px; font-weight: 600; margin: 0; }
.topbar .crumb { color: var(--muted); font-size: 12px; }
.topbar .actions { margin-left: auto; display: flex; align-items: center; gap: 8px; }
.sr-latency { font-size: 12px; color: #909399; }

.sem-page {
  /* 关键：主区域必须自己是滚动容器，否则 body{overflow:hidden} 会把超出的内容裁掉，
     表格数据看不到、也没有滚动条（与 Observability.vue 的 .obs-page 保持一致） */
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  position: relative;
  padding: 20px 24px; display: flex; flex-direction: column; gap: 16px;
  background: var(--bg);
}
/* flex column + overflow 下子项会被压缩，显式禁止 */
.sem-page > * { flex: none; }

.form-card {
  background: #fff; border: 1px solid var(--border); border-radius: 8px;
  padding: 18px 20px;
}

/* 高级选项折叠面板：与表单卡片同底，展开后不抢基础表单的视觉 */
.adv-collapse {
  border-top: 1px dashed var(--border);
  border-bottom: none;
  margin: 4px 0 12px;
}
.adv-collapse :deep(.el-collapse-item__header) {
  border-bottom: none;
  height: 40px;
  font-size: 13px;
  color: var(--muted);
}
.adv-collapse :deep(.el-collapse-item__wrap) { border-bottom: none; }
.adv-collapse :deep(.el-collapse-item__content) { padding-bottom: 4px; }
.adv-title { display: inline-flex; align-items: center; gap: 8px; font-weight: 500; }
.adv-inline { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.filter-rows { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.filter-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.adv-hint { font-size: 12px; }
.form-actions {
  display: flex; align-items: center; gap: 12px; padding-top: 4px;
  flex-wrap: wrap;
}
.form-actions .quick-fill {
  margin-left: auto; display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px;
}
.form-actions .quick-fill .el-link { font-size: 12px; }

.alert { border-radius: 8px; }

.result { display: flex; flex-direction: column; gap: 16px; }
.result-card, .sql-card {
  border-radius: 8px;
}
.card-hd {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  font-weight: 600;
  flex-wrap: wrap;
}
.card-hd .dim {
  font-weight: normal; color: var(--muted); font-size: 12px;
  display: inline-flex; align-items: center; gap: 8px;
}
.caliber-line {
  margin: 0 0 12px;
  font-size: 12px; line-height: 1.6; color: #5b6b7f;
  background: #f6f8fa; border-left: 3px solid #005bff; border-radius: 6px;
  padding: 6px 10px;
}
.caliber-line .sc-label {
  display: inline-block; font-weight: 600; color: #005bff; margin-right: 6px;
}
.caliber-line .sc-more { margin-top: 5px; }
.caliber-line .sc-more > summary { cursor: pointer; color: #005bff; font-weight: 600; outline: none; }
.caliber-line .sc-ver { font-weight: 400; color: var(--muted); }
.caliber-line .sc-detail { margin-top: 4px; display: grid; gap: 3px; }
.caliber-line .sc-row { display: flex; gap: 6px; align-items: flex-start; }
.caliber-line .sc-k { flex: 0 0 58px; color: var(--muted); }
.caliber-line .sc-v { flex: 1; min-width: 0; word-break: break-word; color: var(--text-2); }
.card-hd .scroll-tip {
  display: inline-flex; align-items: center;
  padding: 2px 8px; border-radius: 10px;
  background: #fff7e6; color: #d97706;
  border: 1px solid #fde2a8;
  font-size: 11px;
}
.ml-1 { margin-left: 6px; }

.sql-block-wrap {
  position: relative;
}
.sql-block {
  margin: 0; padding: 12px 14px; background: #0f172a; color: #e2e8f0;
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.6;
  white-space: pre;
  overflow-x: auto;
  overflow-y: auto;
  max-height: 360px;
  /* 让横滚条更显眼一点 */
  scrollbar-width: thin;
  scrollbar-color: #475569 #1e293b;
}
.sql-block::-webkit-scrollbar { height: 10px; width: 10px; }
.sql-block::-webkit-scrollbar-track { background: #1e293b; }
.sql-block::-webkit-scrollbar-thumb {
  background: #475569; border-radius: 5px;
}
.sql-block::-webkit-scrollbar-thumb:hover { background: #64748b; }
.sql-block.fullscreen {
  max-height: calc(100vh - 220px);
  font-size: 13px;
}

.sql-actions {
  display: flex; align-items: center; gap: 8px; padding-top: 10px;
  flex-wrap: wrap;
}

.op-name { font-weight: 500; }
.op-sub {
  margin-left: 8px; font-size: 11px; color: var(--muted);
}

/* 表格列宽自适应 — 让宽内容可读 */
:deep(.el-table .cell) {
  white-space: nowrap;
}
</style>