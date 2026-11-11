<template>
  <el-drawer
    v-model="visible" title="发布租户覆盖（overlay）" size="72%"
    :close-on-click-modal="false" @open="onOpen" @closed="onClosed"
  >
    <el-alert
      type="info" :closable="false" show-icon class="ov-alert"
      title="overlay 是「按 name 整条替换」，不是字段级 merge"
      description="勾选基座条目 → 改字段 → 提交的是该条目的完整定义。因此未勾选的条目继续用全局口径；改动的字段之外的字段会取自基座当前值。"
    />

    <!-- ① 租户 -->
    <div class="ov-section">
      <span class="ov-step">1</span> 选择目标租户
      <span class="ov-hint">（留空 = 全局；非管理员只能发给自己所在租户）</span>
    </div>
    <div class="ov-row">
      <el-select
        v-model="tenantId" filterable clearable :disabled="!admin" class="ov-tenant"
        :placeholder="admin ? '租户（空 = 全局）' : '本人所属租户'"
      >
        <el-option v-for="t in tenants" :key="t.id" :label="tenantLabel(t)" :value="t.id" />
      </el-select>
      <el-button :loading="loadingBase" @click="loadBase">载入基座条目</el-button>
      <span v-if="baseLoaded" class="ov-hint">
        基座：{{ (base.semantic_models || []).length }} 模型 / {{ (base.metrics || []).length }} 指标
      </span>
    </div>
    <el-alert
      v-if="existingDrafts" type="warning" :closable="false" show-icon class="ov-alert"
      :title="`该租户已有 ${existingDrafts} 份未审 overlay 快照`"
      description="overlay 按版本升序叠加，后发布的覆盖先发布的。确认不需要旧的请先废弃，避免同一条目被两份 overlay 争抢。"
    />

    <!-- ② 选条目 -->
    <div class="ov-section">
      <span class="ov-step">2</span> 选择要覆盖的条目
      <el-radio-group v-model="kind" size="small" class="ov-kind">
        <el-radio-button label="metrics">指标</el-radio-button>
        <el-radio-button label="semantic_models">语义模型</el-radio-button>
      </el-radio-group>
      <el-input v-model="keyword" size="small" clearable placeholder="搜索名称" class="ov-search" />
    </div>
    <el-table
      v-loading="loadingBase" :data="filteredEntries" size="small" border class="ov-table"
      :empty-text="baseLoaded ? '没有匹配的条目' : '请先载入基座条目'"
      @selection-change="onSelectionChange"
    >
      <el-table-column type="selection" width="44" />
      <el-table-column prop="name" :label="kind === 'metrics' ? '指标' : '模型'" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.type" size="small" effect="plain" class="ov-inline">{{ row.type }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="200" show-overflow-tooltip />
      <el-table-column v-if="kind === 'metrics'" prop="unit" label="单位" width="90" />
      <el-table-column v-else prop="table" label="物理表" min-width="160" show-overflow-tooltip />
      <el-table-column label="已改动" width="90">
        <template #default="{ row }">
          <el-tag v-if="diffOf(row.name).length" size="small" type="warning" effect="plain">
            {{ diffOf(row.name).length }} 处
          </el-tag>
          <span v-else>—</span>
        </template>
      </el-table-column>
    </el-table>

    <!-- ③ 改字段 -->
    <template v-if="drafts.length">
      <div class="ov-section"><span class="ov-step">3</span> 修改字段</div>
      <el-collapse v-model="opened">
        <el-collapse-item v-for="d in drafts" :key="d.key" :name="d.key">
          <template #title>
            <span class="ov-title">{{ d.name }}</span>
            <el-tag v-if="diffOf(d.name).length" size="small" type="warning" effect="plain">
              {{ diffOf(d.name).length }} 处改动
            </el-tag>
            <el-tag v-else size="small" type="info" effect="plain">未改动</el-tag>
            <el-button
              link type="danger" size="small" class="ov-remove"
              @click.stop="removeDraft(d.key)"
            >移除</el-button>
          </template>

          <template v-if="d.kind === 'metrics'">
            <el-form label-width="110px" size="small" class="ov-form">
              <el-form-item label="显示单位">
                <el-input v-model="d.entry.unit" placeholder="如 元 / 笔 / %" />
              </el-form-item>
              <el-form-item label="量纲缩放">
                <el-input-number v-model="d.entry.scale" :precision="4" :step="0.1" />
                <span class="ov-hint">口径值 = 原始值 × scale</span>
              </el-form-item>
              <el-form-item label="小数位">
                <el-input-number v-model="d.entry.round" :min="0" :max="8" />
              </el-form-item>
              <el-form-item label="别名">
                <el-select v-model="d.entry.aliases" multiple filterable allow-create default-first-option class="ov-wide">
                  <el-option v-for="a in d.entry.aliases" :key="a" :label="a" :value="a" />
                </el-select>
              </el-form-item>
              <el-form-item label="时间粒度">
                <el-checkbox-group v-model="d.entry.granularities">
                  <el-checkbox v-for="g in granularityOptions" :key="g" :label="g" :value="g" />
                </el-checkbox-group>
              </el-form-item>
              <el-form-item label="对比类型">
                <el-checkbox-group v-model="d.entry.pop_types">
                  <el-checkbox v-for="p in popOptions" :key="p" :label="p" :value="p" />
                </el-checkbox-group>
              </el-form-item>
              <el-form-item label="口径说明">
                <el-input v-model="d.entry.description" type="textarea" :rows="2" />
              </el-form-item>
              <el-form-item label="type_params">
                <el-input v-model="d.typeParamsText" type="textarea" :rows="3" class="mono" />
                <div class="ov-hint">JSON；引用链（measure / 分子分母指标）在这里，改错会在发布校验被拦下</div>
              </el-form-item>
              <el-form-item label="name / type">
                <span class="mono ov-hint">{{ d.entry.name }} · {{ d.entry.type }}（身份与引用链，不可改）</span>
              </el-form-item>
            </el-form>
          </template>

          <template v-else>
            <el-form label-width="110px" size="small" class="ov-form">
              <el-form-item label="物理表">
                <el-input v-model="d.entry.table" class="mono" />
              </el-form-item>
              <el-form-item label="说明">
                <el-input v-model="d.entry.description" type="textarea" :rows="2" />
              </el-form-item>
              <el-form-item label="agg_time">
                <el-input v-model="d.entry.agg_time_dimension" class="mono" />
              </el-form-item>
              <el-alert
                type="info" :closable="false" show-icon class="ov-alert-inline"
                title="维度 / 度量按整份覆盖"
                description="overlay 对模型按 name 整条替换：下方改的维度/度量数组会整体取代基座该模型的对应数组（不是逐条 merge）。删字段即代表该模型不再暴露该维度/度量。"
              />
              <el-form-item label="维度（JSON）">
                <el-input v-model="d.dimensionsText" type="textarea" :rows="6" class="mono" />
                <div class="ov-hint">JSON 数组；每项 { name, type: time|categorical, expr, time_granularity?, description? }</div>
              </el-form-item>
              <el-form-item label="度量（JSON）">
                <el-input v-model="d.measuresText" type="textarea" :rows="6" class="mono" />
                <div class="ov-hint">JSON 数组；每项 { name, agg, expr, agg_time_dimension?, unit?, description? }</div>
              </el-form-item>
            </el-form>
          </template>

          <div v-if="diffOf(d.name).length" class="ov-diff">
            <div class="ov-diff-hd">与基座的差异</div>
            <div v-for="c in diffOf(d.name)" :key="c.field" class="ov-diff-row">
              <span class="ov-field">{{ c.field }}</span>
              <span class="ov-old">{{ c.oldText }}</span>
              <span class="ov-arrow">→</span>
              <span class="ov-new">{{ c.newText }}</span>
            </div>
          </div>
        </el-collapse-item>
      </el-collapse>
    </template>

    <!-- ④ 提交 -->
    <div class="ov-section"><span class="ov-step">4</span> 预览与提交</div>
    <div class="ov-row">
      <el-tag v-if="drafts.length" type="warning" effect="plain">
        将覆盖 {{ metricDrafts.length }} 个指标 / {{ modelDrafts.length }} 个模型
      </el-tag>
      <el-tag v-else type="info" effect="plain">尚未选择条目</el-tag>
      <span class="ov-spacer" />
      <el-checkbox v-model="submitReview" :disabled="!drafts.length">
        提交后直接送审（draft → review）
      </el-checkbox>
      <el-button type="primary" :loading="submitting" :disabled="!drafts.length" @click="onSubmit">
        发布为 overlay 草稿
      </el-button>
    </div>
    <el-input
      v-if="drafts.length" :model-value="payloadPreview" type="textarea" :rows="8"
      readonly class="mono ov-preview"
    />
  </el-drawer>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import * as api from '../utils/api'
import { isAdmin, store } from '../store'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  tenants: { type: Array, default: () => [] },
  defaultTenant: { type: String, default: '' }
})
const emit = defineEmits(['update:modelValue', 'published'])

const GRANULARITIES = ['day', 'week', 'month', 'quarter', 'year']
const POP_TYPES = ['yoy', 'mom', 'qoq', 'wow', 'pop']

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const admin = computed(() => isAdmin())
const granularityOptions = GRANULARITIES
const popOptions = POP_TYPES

const tenantId = ref('')
const kind = ref('metrics')
const keyword = ref('')
const base = ref({})
const baseLoaded = ref(false)
const loadingBase = ref(false)
const drafts = ref([]) // [{ key, kind, name, entry, typeParamsText }]
const opened = ref([])
const submitting = ref(false)
const submitReview = ref(false)
const existingDrafts = ref(0)

const tenantLabel = (t) => (t.name || t.code || t.id) + (t.code ? `（${t.code}）` : '')

function reset() {
  base.value = {}
  baseLoaded.value = false
  drafts.value = []
  opened.value = []
  keyword.value = ''
  kind.value = 'metrics'
  existingDrafts.value = 0
  submitReview.value = false
}

function onOpen() {
  const own = (store.auth && store.auth.user && store.auth.user.tenant_id) || ''
  // 非 admin 恒发本租户（后端也会强制归属，这里只是让界面不说谎）
  tenantId.value = admin.value ? props.defaultTenant || '' : own
  reset()
  loadBase()
}

function onClosed() {
  reset()
}

async function loadBase() {
  loadingBase.value = true
  try {
    base.value = await api.semanticBaseManifest()
    baseLoaded.value = true
    // 换租户要重新起算：草稿来自"当前基座"，基座没变则保留用户已填内容
    drafts.value = drafts.value.filter((d) => baseEntries.value.some((e) => e.name === d.name))
    await loadExistingDrafts()
  } catch (e) {
    ElMessage.error(api.formatDetail(e?.message || e))
  } finally {
    loadingBase.value = false
  }
}

async function loadExistingDrafts() {
  existingDrafts.value = 0
  if (!tenantId.value) return
  try {
    const res = await api.listSnapshots(tenantId.value, 'draft', 20)
    existingDrafts.value = (res.items || []).filter((s) => s.mode === 'overlay').length
  } catch {
    existingDrafts.value = 0 // 只读提示，失败不打断编辑
  }
}

watch(tenantId, () => {
  if (baseLoaded.value) loadExistingDrafts()
})

const baseEntries = computed(() => {
  const list = base.value[kind.value] || []
  return list.filter((e) => e && e.name)
})

const filteredEntries = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return baseEntries.value
  return baseEntries.value.filter(
    (e) => e.name.toLowerCase().includes(kw) || (e.description || '').toLowerCase().includes(kw)
  )
})

/* 选中即"以基座条目为起点"建草稿：整条替换语义要求提交完整条目，不能只发改动字段 */
function onSelectionChange(rows) {
  const names = new Set(rows.map((r) => r.name))
  drafts.value = drafts.value.filter((d) => d.kind !== kind.value || names.has(d.name))
  for (const r of rows) {
    const key = `${kind.value}:${r.name}`
    if (drafts.value.some((d) => d.key === key)) continue
    const entry = JSON.parse(JSON.stringify(r))
    // 维度/度量嵌套在模型内、无独立租户列（归属继承宿主模型）；
    // overlay 对模型整条替换，因此把这两份数组也作为可编辑项暴露出来。
    entry.dimensions = entry.dimensions || []
    entry.measures = entry.measures || []
    drafts.value.push({
      key,
      kind: kind.value,
      name: r.name,
      entry,
      origin: r,
      typeParamsText: JSON.stringify(entry.type_params || {}, null, 2),
      dimensionsText: JSON.stringify(entry.dimensions, null, 2),
      measuresText: JSON.stringify(entry.measures, null, 2)
    })
    opened.value.push(key)
  }
}

function removeDraft(key) {
  drafts.value = drafts.value.filter((d) => d.key !== key)
}

const metricDrafts = computed(() => drafts.value.filter((d) => d.kind === 'metrics'))
const modelDrafts = computed(() => drafts.value.filter((d) => d.kind === 'semantic_models'))

function text(v) {
  if (v === undefined || v === null || v === '') return '（空）'
  if (Array.isArray(v)) return v.join('、') || '（空）'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function diffOf(name) {
  const d = drafts.value.find((x) => x.name === name)
  if (!d) return []
  const out = []
  for (const field of editableFields(d.kind)) {
    let oldV, newV
    if (field === 'type_params') {
      oldV = d.origin?.[field]; newV = safeParse(d.typeParamsText)
    } else if (field === 'dimensions') {
      oldV = d.origin?.dimensions; newV = safeParse(d.dimensionsText)
    } else if (field === 'measures') {
      oldV = d.origin?.measures; newV = safeParse(d.measuresText)
    } else {
      oldV = d.origin?.[field]; newV = d.entry[field]
    }
    if (JSON.stringify(oldV ?? null) !== JSON.stringify(newV ?? null)) {
      out.push({ field, oldText: text(oldV), newText: text(newV) })
    }
  }
  return out
}

function editableFields(kind) {
  return kind === 'metrics'
    ? ['unit', 'scale', 'round', 'aliases', 'granularities', 'pop_types', 'description', 'type_params']
    : ['table', 'description', 'agg_time_dimension', 'dimensions', 'measures']
}

function safeParse(s) {
  try {
    return JSON.parse(s)
  } catch {
    return null
  }
}

const payloadPreview = computed(() => {
  const payload = {}
  if (metricDrafts.value.length) {
    payload.metrics = metricDrafts.value.map((d) => withTypeParams(d))
  }
  if (modelDrafts.value.length) {
    payload.semantic_models = modelDrafts.value.map((d) => d.entry)
  }
  return JSON.stringify(payload, null, 2)
})

function withTypeParams(d) {
  const entry = JSON.parse(JSON.stringify(d.entry))
  const parsed = safeParse(d.typeParamsText)
  entry.type_params = parsed === null ? {} : parsed
  return entry
}

async function onSubmit() {
  for (const d of metricDrafts.value) {
    if (safeParse(d.typeParamsText) === null) {
      ElMessage.error(`指标「${d.name}」的 type_params 不是合法 JSON`)
      return
    }
  }
  for (const d of modelDrafts.value) {
    const dims = safeParse(d.dimensionsText)
    const meas = safeParse(d.measuresText)
    if (dims === null || !Array.isArray(dims)) {
      ElMessage.error(`模型「${d.name}」的维度必须是合法 JSON 数组`)
      return
    }
    if (meas === null || !Array.isArray(meas)) {
      ElMessage.error(`模型「${d.name}」的度量必须是合法 JSON 数组`)
      return
    }
    d.entry.dimensions = dims
    d.entry.measures = meas
  }
  submitting.value = true
  try {
    const payload = JSON.parse(payloadPreview.value)
    await api.publishSnapshot({
      tenant_id: tenantId.value,
      mode: 'overlay',
      state: 'draft',
      payload
    })
    ElMessage.success('overlay 草稿已创建，可在版本历史里提交审核 / 审批发布')
    if (submitReview.value) ElMessage.info('送审请在快照版本历史里点「提交审核」')
    emit('published')
    visible.value = false
  } catch (e) {
    ElMessage.error(api.formatDetail(e?.message || e))
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.ov-alert { margin-bottom: 12px }
.ov-alert-inline { margin: 4px 0 12px }
.ov-section {
  margin: 14px 0 8px; font-weight: 600; font-size: 13px;
  display: flex; align-items: center; gap: 8px;
}
.ov-step {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 50%;
  background: #409eff; color: #fff; font-size: 12px;
}
.ov-hint { color: #909399; font-size: 12px; font-weight: 400 }
.ov-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px }
.ov-tenant { width: 280px }
.ov-spacer { flex: 1 }
.ov-kind { margin-left: 8px }
.ov-search { width: 200px }
.ov-table { margin-bottom: 4px }
.ov-inline { margin-left: 6px }
.ov-title { font-weight: 600; margin-right: 8px }
.ov-remove { margin-left: 8px }
.ov-form { padding: 8px 4px 0 }
.ov-wide { width: 100% }
.ov-diff { margin: 4px 0 10px; padding: 8px 10px; background: #fafafa; border-radius: 4px }
.ov-diff-hd { font-size: 12px; color: #606266; margin-bottom: 6px }
.ov-diff-row { display: flex; gap: 8px; font-size: 12px; margin-bottom: 4px; align-items: baseline }
.ov-field { color: #606266; min-width: 90px }
.ov-old { color: #909399; text-decoration: line-through; max-width: 40%; word-break: break-all }
.ov-arrow { color: #c0c4cc }
.ov-new { color: #e6a23c; max-width: 40%; word-break: break-all }
.ov-preview { margin-top: 8px; font-size: 12px }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace }
</style>
