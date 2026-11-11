<template>
  <header class="topbar">
    <h1>合规审计看板</h1>
    <span class="crumb">PRD FR13 · 全链路审计可查（数据访问 / 安全护栏 / 权限校验 / 工具调用）</span>
    <div class="obs-actions">
      <el-button type="primary" plain size="small" :loading="loading" @click="onRefresh">
        刷新
      </el-button>
    </div>
  </header>

  <section class="obs-page">
    <!-- 统计卡片 -->
    <div class="kpis">
      <div class="kpi"><div class="k">审计事件总数</div><div class="v">{{ n(stats.total) }}</div></div>
      <div class="kpi warn"><div class="k">高危(high)</div><div class="v">{{ n((stats.by_level || {}).high || 0) }}</div></div>
      <div class="kpi mid"><div class="k">警告(warning)</div><div class="v">{{ n((stats.by_level || {}).warning || 0) }}</div></div>
      <div class="kpi ok"><div class="k">常规(info)</div><div class="v">{{ n((stats.by_level || {}).info || 0) }}</div></div>
    </div>

    <!-- 类型 / 级别分布 -->
    <div class="grid2">
      <div class="panel">
        <div class="panel-h">事件类型分布</div>
        <div class="bars">
          <template v-if="typeBars.length">
            <div v-for="b in typeBars" :key="b.name" class="bar-row" @click="applyEventType(b.name)">
              <span class="bar-name">{{ eventTypeLabel(b.name) }}</span>
              <span class="bar-track"><i :style="{ width: b.pct + '%' }"></i></span>
              <span class="bar-val">{{ b.count }}</span>
            </div>
          </template>
          <div v-else class="obs-empty sm">暂无数据</div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-h">安全级别分布</div>
        <div class="bars">
          <template v-if="levelBars.length">
            <div v-for="b in levelBars" :key="b.name" class="bar-row" @click="applyLevel(b.name)">
              <span class="bar-name">{{ levelLabel(b.name) }}</span>
              <span class="bar-track"><i :class="'lv-' + b.name" :style="{ width: b.pct + '%' }"></i></span>
              <span class="bar-val">{{ b.count }}</span>
            </div>
          </template>
          <div v-else class="obs-empty sm">暂无数据</div>
        </div>
      </div>
    </div>

    <!-- 过滤条 -->
    <div class="filters">
      <el-select v-model="filters.event_type" class="f-sm" size="small" clearable placeholder="事件类型" @change="onFilterChange">
        <el-option v-for="t in eventTypeOptions" :key="t" :label="eventTypeLabel(t)" :value="t" />
      </el-select>
      <el-select v-model="filters.level" class="f-sm" size="small" clearable placeholder="级别" @change="onFilterChange">
        <el-option label="常规 info" value="info" />
        <el-option label="警告 warning" value="warning" />
        <el-option label="高危 high" value="high" />
      </el-select>
      <el-input v-model="filters.user_id" class="f-session" size="small" clearable placeholder="用户 ID" @keyup.enter="onFilterChange" />
      <el-input v-model="filters.session_id" class="f-session" size="small" clearable placeholder="会话 ID" @keyup.enter="onFilterChange" />
      <el-button size="small" type="primary" @click="onFilterChange">查询</el-button>
      <el-button size="small" text @click="resetFilters">重置</el-button>
    </div>

    <div v-if="loadError" class="obs-empty">审计接口不可用：{{ loadError }}</div>

    <template v-else>
      <el-table :data="items" size="small" class="obs-table" :max-height="520" stripe v-loading="loading">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="detail-box">
              <div v-if="row.summary" class="detail-summary">{{ row.summary }}</div>
              <pre v-if="row.detail_json && Object.keys(row.detail_json).length" class="detail-json">{{ pretty(row.detail_json) }}</pre>
              <div v-else class="obs-empty sm">无扩展明细</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="类型" width="170">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ eventTypeLabel(row.event_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="级别" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="levelTag(row.level)">{{ levelLabel(row.level) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="user_id" label="用户" min-width="120" show-overflow-tooltip />
        <el-table-column prop="tenant_id" label="租户" min-width="110" show-overflow-tooltip />
        <el-table-column prop="session_id" label="会话" min-width="130" show-overflow-tooltip />
        <el-table-column prop="round_id" label="轮次" width="90" show-overflow-tooltip />
        <el-table-column label="摘要" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">{{ row.summary || '—' }}</template>
        </el-table-column>
      </el-table>

      <div class="reg-pager">
        <el-pagination
          :current-page="page" :page-size="pageSize" :total="total"
          :page-sizes="[20, 50, 100]" layout="total, sizes, prev, pager, next"
          @current-change="setPage" @size-change="onSizeChange"
        />
      </div>
    </template>
  </section>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { store } from '../store'
import { fetchAuditLogs, fetchAuditStats } from '../utils/api'

/* 事件类型枚举（与后端写入一致：security/auditor.py、harness、数据访问事件） */
const EVENT_TYPE_LABELS = {
  data_access: '数据访问',
  harness_tool_call: 'Harness 工具调用',
  'permission/check': '权限校验',
  'security/input_guard': '输入安全护栏',
  'security/output_guard': '输出安全护栏',
}
const eventTypeOptions = Object.keys(EVENT_TYPE_LABELS)

const loading = ref(false)
const loadError = ref('')
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)

const stats = reactive({ total: 0, by_type: {}, by_level: {} })

const filters = reactive({ event_type: '', level: '', user_id: '', session_id: '' })

function eventTypeLabel(t) { return EVENT_TYPE_LABELS[t] || t || '-' }
function levelLabel(l) {
  return { info: '常规', warning: '警告', high: '高危', critical: '严重' }[l] || l || '-'
}
function levelTag(l) {
  return l === 'high' || l === 'critical' ? 'danger' : (l === 'warning' ? 'warning' : 'info')
}
function n(v) { return v == null ? '0' : Number(v).toLocaleString('zh-CN') }
function fmtTime(t) {
  if (!t) return '-'
  try { return new Date(t).toLocaleString('zh-CN', { hour12: false }) } catch (e) { return t }
}
function pretty(d) {
  try { return JSON.stringify(d, null, 2) } catch (e) { return String(d) }
}

const typeBars = computed(() => {
  const data = Object.entries(stats.by_type || {})
  const max = Math.max(1, ...data.map(([, c]) => c))
  return data
    .map(([name, count]) => ({ name, count, pct: Math.round((count / max) * 100) }))
    .sort((a, b) => b.count - a.count)
})
const levelBars = computed(() => {
  const data = Object.entries(stats.by_level || {})
  const max = Math.max(1, ...data.map(([, c]) => c))
  return data
    .map(([name, count]) => ({ name, count, pct: Math.round((count / max) * 100) }))
    .sort((a, b) => b.count - a.count)
})

async function loadStats() {
  try {
    const r = await fetchAuditStats()
    stats.by_type = r.by_type || {}
    stats.by_level = r.by_level || {}
    stats.total = (Object.values(stats.by_type)).reduce((a, b) => a + b, 0)
  } catch (e) { /* 统计失败不阻断列表 */ stats.total = 0 }
}

async function loadList() {
  loading.value = true
  loadError.value = ''
  try {
    const params = {
      page: page.value, page_size: pageSize.value,
      event_type: filters.event_type || undefined,
      level: filters.level || undefined,
      user_id: filters.user_id || undefined,
      session_id: filters.session_id || undefined,
    }
    const r = await fetchAuditLogs(params)
    items.value = r.items || []
    total.value = r.total || 0
    if (page.value === 1) await loadStats()
  } catch (e) {
    loadError.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function onFilterChange() { page.value = 1; loadList() }
function resetFilters() {
  filters.event_type = ''; filters.level = ''; filters.user_id = ''; filters.session_id = ''
  page.value = 1; loadList()
}
function applyEventType(t) { filters.event_type = filters.event_type === t ? '' : t; onFilterChange() }
function applyLevel(l) { filters.level = filters.level === l ? '' : l; onFilterChange() }
function setPage(p) { page.value = p; loadList() }
function onSizeChange(s) { pageSize.value = s; page.value = 1; loadList() }
async function onRefresh() { await loadList(); ElMessage.success('已刷新') }

onMounted(() => { loadList() })
</script>

<style scoped>
.topbar { display: flex; align-items: center; gap: 12px; padding: 14px 22px; border-bottom: 1px solid #ebeef5; }
.topbar h1 { margin: 0; font-size: 18px; font-weight: 600; color: #1f2329; }
.crumb { font-size: 12px; color: #8a9099; }
.obs-actions { margin-left: auto; display: flex; gap: 8px; }
.obs-page { flex: 1; min-height: 0; overflow-y: auto; padding: 16px 22px 28px; box-sizing: border-box; }
.obs-page > * { flex: none; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 14px; }
.kpi { background: #f7f8fa; border: 1px solid #ebeef5; border-radius: 8px; padding: 14px 16px; }
.kpi .k { font-size: 12px; color: #8a9099; margin-bottom: 6px; }
.kpi .v { font-size: 24px; font-weight: 600; color: #1f2329; font-variant-numeric: tabular-nums; }
.kpi.warn .v { color: #f53f3f; }
.kpi.mid .v { color: #d4860b; }
.kpi.ok .v { color: #00b42a; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 14px; }
.panel { background: #fff; border: 1px solid #ebeef5; border-radius: 8px; padding: 12px 16px; }
.panel-h { font-size: 13px; font-weight: 600; color: #1f2329; margin-bottom: 10px; }
.bars { display: flex; flex-direction: column; gap: 8px; }
.bar-row { display: flex; align-items: center; gap: 10px; cursor: pointer; }
.bar-name { width: 130px; flex: none; font-size: 12px; color: #4e5969; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { flex: 1; height: 8px; background: #f0f1f3; border-radius: 4px; overflow: hidden; }
.bar-track i { display: block; height: 100%; background: #165dff; border-radius: 4px; }
.bar-track i.lv-high, .bar-track i.lv-critical { background: #f53f3f; }
.bar-track i.lv-warning { background: #d4860b; }
.bar-track i.lv-info { background: #165dff; }
.bar-val { width: 48px; flex: none; text-align: right; font-size: 12px; color: #1f2329; font-variant-numeric: tabular-nums; }
.filters { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.f-sm { width: 160px; }
.f-session { width: 200px; }
.obs-table { width: 100%; }
.reg-pager { display: flex; justify-content: flex-end; margin-top: 14px; }
.detail-box { padding: 6px 12px 10px; }
.detail-summary { font-size: 12px; color: #4e5969; margin-bottom: 6px; }
.detail-json { background: #0d1117; color: #c9d1d9; padding: 10px 12px; border-radius: 6px; font-size: 11px; line-height: 1.6; max-height: 280px; overflow: auto; white-space: pre-wrap; word-break: break-all; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.obs-empty { color: #8a9099; padding: 12px; text-align: center; }
.obs-empty.sm { font-size: 12px; padding: 8px; }
</style>
