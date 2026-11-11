<template>
  <div class="reg-page">
    <div class="reg-head">
      <div>
        <h2 class="reg-title">语义注册中心</h2>
        <div class="reg-sub">
          指标 / 维度的口径治理：<b>草稿 → 提交审核 → 审批发布</b>。起草与审批分离（发布 / 回滚仅管理员）。
          发布后即时热更新，无需重启。
        </div>
      </div>
      <div class="reg-head-actions">
        <el-button v-if="writable" type="primary" :icon="Plus" @click="openCreate()">新增{{ tabLabel }}</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="onRefresh()">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="!writable"
      title="当前角色对语义注册中心为只读：新增 / 编辑 / 提交 / 废弃需 semantic:write，审批发布 / 回滚需管理员。"
      type="info" :closable="false" show-icon class="reg-alert"
    />
    <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" show-icon class="reg-alert" />

    <el-tabs v-model="activeTab" class="reg-tabs" @tab-change="onTabChange">
      <el-tab-pane label="指标" name="metric" />
      <el-tab-pane label="维度" name="dimension" />
    </el-tabs>

    <div class="reg-toolbar">
      <el-input
        v-model="filters.keyword" class="reg-search" clearable placeholder="匹配 名称 / key / 别名"
        @keyup.enter="onFilterChange()" @clear="onFilterChange()" @input="onKeywordInput()"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-select v-if="activeTab === 'metric'" v-model="filters.metricType" class="reg-select" clearable placeholder="类型"
                 @change="onFilterChange()">
        <el-option label="simple 单指标" value="simple" />
        <el-option label="ratio 比率" value="ratio" />
        <el-option label="derived 派生" value="derived" />
      </el-select>
      <el-select v-model="filters.status" class="reg-select" clearable placeholder="状态" @change="onFilterChange()">
        <el-option label="草稿" value="draft" />
        <el-option label="待审核" value="review" />
        <el-option label="已发布" value="published" />
        <el-option label="已废弃" value="deprecated" />
      </el-select>
      <el-button text :icon="RefreshLeft" @click="resetFilters()">重置</el-button>
      <span class="reg-hint">共 {{ total }} 条</span>
    </div>

    <!-- FR13 数据权限提示（T198f）：让"某指标/维度为什么用不了"在管理面上可见 -->
    <el-alert
      v-if="permCtx && permCtx.mode !== 'off'"
      :type="permCtx.mode === 'enforce' ? 'warning' : 'info'"
      :closable="false"
      show-icon
      class="reg-perm-alert"
      :title="permAlertTitle"
    />

    <!-- 指标表 -->
    <el-table
      v-if="activeTab === 'metric'" :data="items" v-loading="loading" border class="reg-table"
      :row-class-name="metricRowClass"
    >
      <el-table-column label="名称" min-width="150" show-overflow-tooltip>
        <template #default="{ row }"><b>{{ row.name }}</b></template>
      </el-table-column>
      <el-table-column label="key" min-width="150" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ row.metric_key }}</span></template>
      </el-table-column>
      <el-table-column label="类型" width="100">
        <template #default="{ row }"><el-tag effect="plain" size="small">{{ typeLabel(row.metric_type) }}</el-tag></template>
      </el-table-column>
      <el-table-column label="口径" min-width="200" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono sm">{{ paramSummary(row) }}</span></template>
      </el-table-column>
      <el-table-column label="别名" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ (row.aliases || []).join(' / ') || '-' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><el-tag :type="statusTag(row.status)" effect="plain" size="small">{{ statusLabel(row.status) }}</el-tag></template>
      </el-table-column>
      <el-table-column v-if="showPermMarks" label="权限" width="120">
        <template #default="{ row }">
          <el-tag v-if="metricKeyRestricted(row.metric_key)" size="small" type="danger" effect="plain">
            未授权
          </el-tag>
          <span v-else class="reg-dash">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="300" fixed="right">
        <template #default="{ row }">
          <el-button v-if="writable" link type="primary" @click="onValidate(row)">校验</el-button>
          <el-button v-if="writable && row.status === 'draft'" link type="primary" @click="onSubmit(row)">提交</el-button>
          <el-button v-if="isAdmin && row.status === 'review'" link type="success" @click="onPublish(row)">发布</el-button>
          <el-button v-if="writable && row.status === 'published'" link type="warning" @click="onDeprecate(row)">废弃</el-button>
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link @click="openVersions(row)">版本</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 维度表 -->
    <el-table
      v-else :data="items" v-loading="loading" border class="reg-table"
      :row-class-name="dimensionRowClass"
    >
      <el-table-column label="名称" min-width="150" show-overflow-tooltip>
        <template #default="{ row }"><b>{{ row.name }}</b></template>
      </el-table-column>
      <el-table-column label="key" min-width="150" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ row.dimension_key }}</span></template>
      </el-table-column>
      <el-table-column label="类型" width="100">
        <template #default="{ row }"><el-tag effect="plain" size="small">{{ row.dim_type === 'time' ? '时间' : '类别' }}</el-tag></template>
      </el-table-column>
      <el-table-column label="表达式" min-width="160" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono sm">{{ row.expr }}</span></template>
      </el-table-column>
      <el-table-column label="所属模型" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.model_key || '-' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><el-tag :type="statusTag(row.status)" effect="plain" size="small">{{ statusLabel(row.status) }}</el-tag></template>
      </el-table-column>
      <el-table-column v-if="showPermMarks" label="权限" width="180">
        <template #default="{ row }">
          <el-tag v-if="dimensionDenied(row.name)" size="small" type="danger" effect="plain">该列被禁</el-tag>
          <el-tooltip v-else-if="rowScopeValues(row.name)" placement="top">
            <template #content>
              行权限：仅可见 {{ rowScopeValues(row.name).join('、') }}
            </template>
            <el-tag size="small" type="warning" effect="plain">
              行权限 {{ rowScopeValues(row.name).length }} 值
            </el-tag>
          </el-tooltip>
          <span v-else class="reg-dash">—</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="300" fixed="right">
        <template #default="{ row }">
          <el-button v-if="writable" link type="primary" @click="onValidate(row)">校验</el-button>
          <el-button v-if="writable && row.status === 'draft'" link type="primary" @click="onSubmit(row)">提交</el-button>
          <el-button v-if="isAdmin && row.status === 'review'" link type="success" @click="onPublish(row)">发布</el-button>
          <el-button v-if="writable && row.status === 'published'" link type="warning" @click="onDeprecate(row)">废弃</el-button>
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link @click="openVersions(row)">版本</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="reg-pager">
      <el-pagination
        layout="total, prev, pager, next, sizes"
        :total="total" :current-page="page" :page-size="pageSize"
        :page-sizes="[10, 20, 50, 100]"
        @current-change="setPage" @size-change="onSizeChange"
      />
    </div>

    <!-- 编辑 / 新增 -->
    <el-drawer v-model="editDrawer" :title="form.id ? `编辑${tabLabel}` : `新增${tabLabel}`" size="600px">
      <el-alert v-if="formError" :title="formError" type="error" :closable="false" show-icon class="reg-alert" />
      <el-form label-width="120px" class="reg-form">
        <template v-if="activeTab === 'metric'">
          <el-form-item label="名称">
            <el-input v-model="form.name" placeholder="如 存款余额" />
          </el-form-item>
          <el-form-item label="指标类型">
            <el-select v-model="form.metric_type" class="reg-wide" :disabled="!!form.id">
              <el-option label="simple 单指标（基于一个度量）" value="simple" />
              <el-option label="ratio 比率（分子/分母）" value="ratio" />
              <el-option label="derived 派生（多指标运算）" value="derived" />
            </el-select>
          </el-form-item>
          <el-form-item label="口径定义">
            <el-select v-if="form.metric_type === 'simple'" v-model="form.measure" filterable class="reg-wide" placeholder="选择度量">
              <el-option v-for="m in measureOptions" :key="m.measure_key" :label="`${m.name}（${m.measure_key}）`" :value="m.measure_key" />
            </el-select>
            <template v-else-if="form.metric_type === 'ratio'">
              <div class="reg-pair">
                <el-select v-model="form.numerator" filterable placeholder="分子指标" class="reg-wide">
                  <el-option v-for="m in metricOptions" :key="m.metric_key" :label="`${m.name}（${m.metric_key}）`" :value="m.metric_key" />
                </el-select>
                <el-select v-model="form.denominator" filterable placeholder="分母指标" class="reg-wide">
                  <el-option v-for="m in metricOptions" :key="m.metric_key" :label="`${m.name}（${m.metric_key}）`" :value="m.metric_key" />
                </el-select>
              </div>
            </template>
            <template v-else>
              <el-select v-model="form.childMetrics" multiple filterable class="reg-wide" placeholder="选择参与运算的指标">
                <el-option v-for="m in metricOptions" :key="m.metric_key" :label="`${m.name}（${m.metric_key}）`" :value="m.metric_key" />
              </el-select>
            </template>
          </el-form-item>
          <el-form-item label="单位"><el-input v-model="form.unit" placeholder="如 元 / 笔 / %" /></el-form-item>
          <el-form-item label="别名"><el-input v-model="form.aliases" placeholder="逗号分隔，如 存款,余额" /></el-form-item>
          <el-form-item label="粒度">
            <el-input v-model="form.granularities" placeholder="day,week,month,quarter,year" />
          </el-form-item>
          <el-form-item label="同环比">
            <el-input v-model="form.pop_types" placeholder="yoy,mom,qoq,wow" />
          </el-form-item>
        </template>

        <template v-else>
          <el-form-item label="名称"><el-input v-model="form.name" placeholder="如 机构 / 客户类型" /></el-form-item>
          <el-form-item label="维度类型">
            <el-radio-group v-model="form.dim_type">
              <el-radio value="time">时间</el-radio>
              <el-radio value="categorical">类别</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="表达式"><el-input v-model="form.expr" placeholder="如 region / DATE(ts)" /></el-form-item>
          <el-form-item v-if="form.dim_type === 'time'" label="时间粒度">
            <el-select v-model="form.time_granularity" class="reg-wide">
              <el-option label="天" value="day" />
              <el-option label="周" value="week" />
              <el-option label="月" value="month" />
              <el-option label="季" value="quarter" />
              <el-option label="年" value="year" />
            </el-select>
          </el-form-item>
        </template>

        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="3" placeholder="口径说明" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDrawer = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSubmitForm()">保存</el-button>
      </template>
    </el-drawer>

    <!-- 校验结果 -->
    <el-dialog v-model="valDialog" :title="`口径校验 · ${valName}`" width="560px">
      <el-alert
        :title="valResult.passed ? '校验通过' : '校验未通过'"
        :type="valResult.passed ? 'success' : 'error'" :closable="false" show-icon
      />
      <div v-if="valResult.problems && valResult.problems.length" class="val-list">
        <div v-for="(p, i) in valResult.problems" :key="'p' + i" class="val-item err">✗ {{ p }}</div>
      </div>
      <div v-if="valResult.warnings && valResult.warnings.length" class="val-list">
        <div v-for="(w, i) in valResult.warnings" :key="'w' + i" class="val-item warn">! {{ w }}</div>
      </div>
      <el-table v-if="valResult.gates && valResult.gates.length" :data="valResult.gates" size="small" border class="reg-table">
        <el-table-column prop="gate" label="校验闸" width="120" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'pass' ? 'success' : (row.status === 'warn' ? 'warning' : 'danger')" size="small">
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="detail" label="说明" min-width="240" show-overflow-tooltip />
      </el-table>
    </el-dialog>

    <!-- 版本历史 -->
    <el-drawer v-model="vs.visible" :title="`版本历史 · ${vs.name}`" size="620px">
      <el-alert v-if="vs.error" :title="vs.error" type="error" :closable="false" show-icon class="reg-alert" />
      <el-table :data="vs.items" v-loading="vs.loading" border size="small" class="reg-table">
        <el-table-column label="版本" width="70">
          <template #default="{ row }"><span class="mono">v{{ row.version }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">{{ statusLabel(row.status) }}</template>
        </el-table-column>
        <el-table-column label="操作人" width="100" show-overflow-tooltip>
          <template #default="{ row }">{{ row.operator || '-' }}</template>
        </el-table-column>
        <el-table-column label="时间" min-width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button v-if="isAdmin" link type="warning" @click="onRollback(row)">回滚</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, RefreshLeft, Search } from '@element-plus/icons-vue'
import {
  registryMetrics, registryMetricDetail, createMetricDraft, updateMetric,
  validateMetric, submitMetric, publishMetric, deprecateMetric, metricVersions, rollbackMetric,
  registryDimensions, registryMeasures, createDimensionDraft, updateDimension,
  validateDimension, submitDimension, publishDimension, deprecateDimension, dimensionVersions, rollbackDimension
} from '../utils/api'
import {
  isAdmin, hasPerm, store,
  loadPermissionContext, metricKeyRestricted, dimensionDenied, rowScopeValues
} from '../store'

const writable = computed(() => hasPerm('semantic:write'))
const admin = computed(() => isAdmin())

/* ---- FR13 数据权限标记（T198f） ----
 * 把「当前用户对哪些指标/维度受限」直接标在管理面上，避免"某指标查不出数"被当成 bug。 */
const permCtx = computed(() => store.permCtx)
const showPermMarks = computed(
  () => !!(permCtx.value && permCtx.value.mode !== 'off' && permCtx.value.restricted)
)
const permAlertTitle = computed(() => {
  const c = permCtx.value
  if (!c) return ''
  const parts = []
  if (!c.unrestricted_metrics) parts.push(`指标白名单 ${(c.allowed_metrics || []).length} 个（未列出的指标不可用）`)
  if ((c.denied_dimensions || []).length) parts.push(`列禁 ${c.denied_dimensions.length} 个（结果集硬剔除）`)
  if ((c.row_scope_dimensions || []).length) parts.push(`行权限 ${c.row_scope_dimensions.length} 个维度（仅可见授权值）`)
  const modeText = c.mode === 'enforce' ? '已启用拦截' : '当前仅审计（不拦截）'
  return `数据权限：${modeText}${parts.length ? ' · ' + parts.join(' · ') : ' · 无授权记录（不限制）'}`
})
const metricRowClass = ({ row }) => (metricKeyRestricted(row.metric_key) ? 'reg-row-limited' : '')
const dimensionRowClass = ({ row }) =>
  dimensionDenied(row.name) || rowScopeValues(row.name) ? 'reg-row-limited' : ''
const activeTab = ref('metric')
const tabLabel = computed(() => (activeTab.value === 'metric' ? '指标' : '维度'))

const loading = ref(false)
const loadError = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const items = ref([])
const filters = reactive({ keyword: '', metricType: '', status: '' })

/* 下拉选项（度量 / 指标，供口径定义选择） */
const measureOptions = ref([])
const metricOptions = ref([])

/* 编辑抽屉 */
const editDrawer = ref(false)
const saving = ref(false)
const formError = ref('')
const form = reactive({
  id: 0, name: '', metric_type: 'simple', measure: '', numerator: '', denominator: '',
  childMetrics: [], unit: '', aliases: '', granularities: 'day,week,month,quarter,year',
  pop_types: 'yoy,mom', description: '',
  dim_type: 'categorical', expr: '', time_granularity: 'day'
})

/* 校验对话框 */
const valDialog = ref(false)
const valName = ref('')
const valResult = reactive({ passed: false, problems: [], warnings: [], gates: [] })

/* 版本抽屉 */
const vs = reactive({ visible: false, name: '', key: '', loading: false, error: '', items: [] })
/* 当前编辑行的 key（供 update 使用） */
const rowKey = ref('')

function typeLabel(t) {
  return { simple: 'simple', ratio: 'ratio', derived: 'derived' }[t] || t || '-'
}
function statusLabel(s) {
  return { draft: '草稿', review: '待审核', published: '已发布', deprecated: '已废弃' }[s] || s || '-'
}
function statusTag(s) {
  return s === 'published' ? 'success' : (s === 'draft' ? 'warning' : (s === 'review' ? 'primary' : 'info'))
}
function paramSummary(row) {
  const tp = row.type_params || {}
  if (row.metric_type === 'simple') return 'measure=' + (tp.measure || '-')
  if (row.metric_type === 'ratio') return `num=${tp.numerator || '-'} / den=${tp.denominator || '-'}`
  if (row.metric_type === 'derived') return 'metrics=' + ((tp.metrics || []).join(',') || '-')
  return '-'
}
function fmtTime(t) {
  if (!t) return '-'
  try { return new Date(t).toLocaleString('zh-CN', { hour12: false }) } catch (e) { return t }
}

async function loadOptions() {
  // 度量列表（simple 指标的 measure 候选）与指标列表（ratio/derived 候选）各自拉取
  try {
    const [ms, mts] = await Promise.all([
      registryMeasures({ page_size: 500 }),
      registryMetrics({ page_size: 200 })
    ])
    measureOptions.value = ms.items || []
    metricOptions.value = mts.items || []
  } catch (e) { /* 选项加载失败不阻断列表 */ }
}
function mts2(mts) { return (mts && mts.items) || [] }

async function loadList() {
  loading.value = true
  loadError.value = ''
  try {
    const params = {
      page: page.value, page_size: pageSize.value,
      keyword: filters.keyword || undefined,
      status: filters.status || undefined
    }
    if (activeTab.value === 'metric') {
      params.metric_type = filters.metricType || undefined
      const r = await registryMetrics(params)
      items.value = r.items || []
      total.value = r.total || 0
    } else {
      const r = await registryDimensions(params)
      items.value = r.items || []
      total.value = r.total || 0
    }
  } catch (e) {
    loadError.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function onTabChange() { page.value = 1; loadList() }
function onFilterChange() { page.value = 1; loadList() }
function resetFilters() {
  filters.keyword = ''; filters.metricType = ''; filters.status = ''
  page.value = 1; loadList()
}
let kwTimer = 0
function onKeywordInput() {
  clearTimeout(kwTimer)
  kwTimer = setTimeout(() => onFilterChange(), 300)
}
function setPage(p) { page.value = p; loadList() }
function onSizeChange(s) { pageSize.value = s; page.value = 1; loadList() }
async function onRefresh() { await loadList(); ElMessage.success('已刷新') }

/* ---- 新增 / 编辑 ---- */
function resetForm() {
  Object.assign(form, {
    id: 0, name: '', metric_type: 'simple', measure: '', numerator: '', denominator: '',
    childMetrics: [], unit: '', aliases: '', granularities: 'day,week,month,quarter,year',
    pop_types: 'yoy,mom', description: '',
    dim_type: 'categorical', expr: '', time_granularity: 'day'
  })
  formError.value = ''
}
function openCreate() {
  resetForm()
  editDrawer.value = true
}
function openEdit(row) {
  rowKey.value = activeTab.value === 'metric' ? row.metric_key : row.dimension_key
  if (activeTab.value === 'metric') {
    const tp = row.type_params || {}
    Object.assign(form, {
      id: row.id, name: row.name, metric_type: row.metric_type, measure: tp.measure || '',
      numerator: tp.numerator || '', denominator: tp.denominator || '',
      childMetrics: tp.metrics || [], unit: row.unit || '',
      aliases: (row.aliases || []).join(', '),
      granularities: (row.granularities || []).join(', '),
      pop_types: (row.pop_types || []).join(', '),
      description: row.description || ''
    })
  } else {
    Object.assign(form, {
      id: row.id, name: row.name, dim_type: row.dim_type, expr: row.expr,
      time_granularity: row.time_granularity || 'day', description: row.description || ''
    })
  }
  formError.value = ''
  editDrawer.value = true
}

function _split(s) {
  return String(s || '').split(/[,，、\s]+/).map((x) => x.trim()).filter(Boolean)
}

async function onSubmitForm() {
  if (!form.name.trim()) { ElMessage.warning('名称不能为空'); return }
  saving.value = true
  formError.value = ''
  try {
    if (activeTab.value === 'metric') {
      const typeParams = {}
      if (form.metric_type === 'simple') {
        if (!form.measure) { ElMessage.warning('simple 指标需选择度量'); saving.value = false; return }
        typeParams.measure = form.measure
      } else if (form.metric_type === 'ratio') {
        if (!form.numerator || !form.denominator) { ElMessage.warning('ratio 指标需选择分子/分母'); saving.value = false; return }
        typeParams.numerator = form.numerator
        typeParams.denominator = form.denominator
      } else {
        if (!form.childMetrics.length) { ElMessage.warning('derived 指标需选择至少一个子指标'); saving.value = false; return }
        typeParams.metrics = [...form.childMetrics]
      }
      const payload = {
        name: form.name.trim(),
        metric_type: form.metric_type,
        type_params: typeParams,
        unit: form.unit.trim(),
        aliases: _split(form.aliases),
        granularities: _split(form.granularities),
        pop_types: _split(form.pop_types),
        description: form.description.trim()
      }
      if (form.id) await updateMetric(rowKey.value, payload)
      else await createMetricDraft(payload)
    } else {
      const payload = {
        name: form.name.trim(),
        dim_type: form.dim_type,
        expr: form.expr.trim(),
        time_granularity: form.dim_type === 'time' ? form.time_granularity : '',
        description: form.description.trim()
      }
      if (form.id) await updateDimension(rowKey.value, payload)
      else await createDimensionDraft(payload)
    }
    ElMessage.success('已保存')
    editDrawer.value = false
    await loadList()
  } catch (e) {
    formError.value = e.message || '保存失败'
  } finally {
    saving.value = false
  }
}

/* ---- 校验 / 提交 / 发布 / 废弃 ---- */
async function onValidate(row) {
  const key = activeTab.value === 'metric' ? row.metric_key : row.dimension_key
  valName.value = row.name
  try {
    const r = activeTab.value === 'metric' ? await validateMetric(key) : await validateDimension(key)
    Object.assign(valResult, {
      passed: r.passed, problems: r.problems || [], warnings: r.warnings || [], gates: r.gates || []
    })
    valDialog.value = true
  } catch (e) { ElMessage.error(e.message || '校验失败') }
}
async function onSubmit(row) {
  const key = activeTab.value === 'metric' ? row.metric_key : row.dimension_key
  try {
    if (activeTab.value === 'metric') await submitMetric(key)
    else await submitDimension(key)
    ElMessage.success('已提交审核')
    await loadList()
  } catch (e) { ElMessage.error(e.message || '提交失败') }
}
async function onPublish(row) {
  const key = activeTab.value === 'metric' ? row.metric_key : row.dimension_key
  const ok = await ElMessageBox.confirm(
    `审批发布「${row.name}」？发布后即时热更新生效。`, '审批发布',
    { type: 'warning', confirmButtonText: '发布', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    if (activeTab.value === 'metric') await publishMetric(key)
    else await publishDimension(key)
    ElMessage.success('已发布')
    await loadList()
  } catch (e) { ElMessage.error(e.message || '发布失败') }
}
async function onDeprecate(row) {
  const key = activeTab.value === 'metric' ? row.metric_key : row.dimension_key
  const ok = await ElMessageBox.confirm(
    `废弃「${row.name}」？运行时将退回上一个已发布版本。`, '废弃确认',
    { type: 'warning', confirmButtonText: '废弃', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    if (activeTab.value === 'metric') await deprecateMetric(key)
    else await deprecateDimension(key)
    ElMessage.success('已废弃')
    await loadList()
  } catch (e) { ElMessage.error(e.message || '废弃失败') }
}

/* ---- 版本 ---- */
function openVersions(row) {
  vs.name = row.name
  vs.key = activeTab.value === 'metric' ? row.metric_key : row.dimension_key
  vs.visible = true
  vs.loading = true
  vs.error = ''
  vs.items = []
  const loader = activeTab.value === 'metric'
    ? metricVersions(vs.key)
    : dimensionVersions(vs.key)
  loader.then((r) => { vs.items = r.items || []; vs.total = r.total || 0 })
    .catch((e) => { vs.error = e.message || '加载版本失败' })
    .finally(() => { vs.loading = false })
}
async function onRollback(row) {
  const ok = await ElMessageBox.confirm(
    `回滚到版本 v${row.version}？会生成一条新版本（不删除历史）。`, '回滚确认',
    { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    // 回滚接口：POST {key}/rollback，生成一条新版本（不删除历史）
    const fn = activeTab.value === 'metric' ? rollbackMetric : rollbackDimension
    await fn(vs.key, row.version)
    ElMessage.success(`已回滚到 v${row.version}`)
    vs.visible = false
    await loadList()
  } catch (e) { ElMessage.error(e.message || '回滚失败') }
}

onMounted(() => {
  loadList()
  loadOptions()
  // 权限标记是"锦上添花"：拉失败也不影响列表（store 内已静默兜底）
  loadPermissionContext()
})
</script>

<style scoped>
.reg-page { flex: 1; min-height: 0; overflow-y: auto; position: relative; padding: 18px 22px 28px; box-sizing: border-box; }
.reg-page > * { flex: none; }
.reg-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.reg-title { margin: 0; font-size: 18px; font-weight: 600; color: #1f2329; }
.reg-sub { font-size: 12px; color: #8a9099; margin-top: 4px; max-width: 820px; line-height: 1.7; }
.reg-head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.reg-alert { margin: 6px 0; }
.reg-tabs { margin: 4px 0 0; }
.reg-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 6px 0 12px; }
.reg-search { width: 260px; }
.reg-select { width: 150px; }
.reg-hint { font-size: 12px; color: #8a9099; }
/* FR13 数据权限提示条 + 受限行（T198f） */
.reg-perm-alert { margin: 0 0 12px; border-radius: 8px; }
.reg-dash { color: #c9cdd4; }
/* :row-class-name 作用在 <tr> 上（非本组件模板节点），需 :deep 穿透 scoped */
.reg-table :deep(.reg-row-limited) td { background: #fffafa; }
.reg-table { width: 100%; }
.reg-pager { display: flex; justify-content: flex-end; margin-top: 14px; }
.reg-form { padding: 0 8px; }
.reg-wide { width: 100%; }
.reg-pair { display: flex; flex-direction: column; gap: 8px; }
.mono { font-size: 11px; color: #8a9099; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.sm { font-size: 12px; }
.val-list { margin: 10px 0; }
.val-item { font-size: 12px; line-height: 1.8; }
.val-item.err { color: #f53f3f; }
.val-item.warn { color: #d4860b; }
</style>
