<template>
  <div class="mem-page">
    <div class="mem-head">
      <div>
        <h2 class="mem-title">记忆管理</h2>
        <div class="mem-sub">
          PRD FR9 分层记忆（平台视角）· L1 会话记忆 / L2 用户记忆 / L3 长期记忆。
          「我的偏好」页是个人快捷入口，本页用于跨用户、跨会话的查看与维护。
        </div>
      </div>
      <div class="mem-head-actions">
        <el-switch
          v-model="memoryOn"
          :loading="store.prefs.saving"
          active-text="记忆注入"
          inline-prompt
          style="--el-switch-on-color: #12b76a"
          @change="onToggleMemory"
        />
        <el-button :icon="Refresh" @click="onRefresh">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="!memoryOn"
      title="记忆注入已关闭（当前登录用户）：系统不会把历史会话摘要与偏好注入到对话中，直到重新开启。"
      type="warning" :closable="false" show-icon class="mem-alert"
    />
    <el-alert
      v-if="!canWriteMemory()"
      title="当前角色对记忆为只读：L3 长期记忆的录入 / 审核 / 删除需要 memory:write 权限点（管理员）。"
      type="info" :closable="false" show-icon class="mem-alert"
    />
    <el-alert v-if="mem.error" :title="mem.error" type="error" :closable="false" show-icon class="mem-alert" />

    <!-- 统计卡：一眼看清各层体量（数据来自 GET /memory/stats） -->
    <div class="mem-stats">
      <div v-for="c in statCards" :key="c.key" class="mem-stat">
        <div class="ms-label">{{ c.label }}</div>
        <div class="ms-value">{{ c.value }}</div>
        <div class="ms-hint">{{ c.hint }}</div>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="mem-tabs" @tab-change="onTabChange">
      <!-- ---------- L1 会话记忆 ---------- -->
      <el-tab-pane label="会话记忆（L1）" name="l1">
        <div class="mem-toolbar">
          <el-input
            v-model="filterL1" class="mem-search" clearable
            placeholder="按摘要正文搜索（如「存款」「按月」）"
            @keyup.enter="loadL1Summaries()" @clear="loadL1Summaries()"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button :icon="Refresh" :loading="l1.loading" @click="loadL1Summaries()">刷新</el-button>
          <span class="mem-hint">摘要由滚动压缩生成（超出保留窗口的轮次增量压缩进摘要），每会话一条。</span>
        </div>

        <el-table :data="l1.items" v-loading="l1.loading" border class="mem-table">
          <el-table-column label="会话标题" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">
              <span>{{ row.session_title || '（会话已删除）' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="归属用户" width="130" show-overflow-tooltip>
            <template #default="{ row }">{{ row.user_id || '-' }}</template>
          </el-table-column>
          <el-table-column label="摘要" min-width="340">
            <template #default="{ row }">
              <div class="mem-summary">{{ row.summary }}</div>
            </template>
          </el-table-column>
          <el-table-column label="覆盖至消息" width="120">
            <template #default="{ row }">
              <span class="mem-mono">{{ shortId(row.upto_message_id) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openSummary(row)">查看</el-button>
              <el-button link type="danger" @click="onClearL1(row)">清空</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <div class="mem-empty">
              暂无会话摘要。摘要只会在会话变长（未压缩轮次 ≥ 阈值）时生成，短会话不会产生 L1 记忆。
            </div>
          </template>
        </el-table>
      </el-tab-pane>

      <!-- ---------- L2 用户记忆 ---------- -->
      <el-tab-pane label="用户记忆（L2）" name="l2">
        <div class="mem-toolbar">
          <el-input
            v-model="filterL2.user_id" class="mem-search" clearable placeholder="按用户 ID 过滤"
            @keyup.enter="setL2Filter({})" @clear="setL2Filter({})"
          />
          <el-select v-model="filterL2.source" class="mem-select" clearable placeholder="来源"
                      @change="setL2Filter({})">
            <el-option label="用户设置" value="user" />
            <el-option label="系统学习" value="inferred" />
          </el-select>
          <el-select v-model="filterL2.status" class="mem-select" clearable placeholder="状态"
                      @change="setL2Filter({})">
            <el-option label="生效中" value="active" />
            <el-option label="已关闭" value="archived" />
          </el-select>
          <el-button :icon="Refresh" :loading="l2.loading" @click="loadL2Preferences()">刷新</el-button>
          <span class="mem-hint">同一用户同一偏好项只保留一条；「系统学习」来自点赞时的口径沉淀。</span>
        </div>

        <el-table :data="l2.items" v-loading="l2.loading" border class="mem-table">
          <el-table-column label="用户" min-width="170">
            <template #default="{ row }">
              <div class="mem-user">{{ row.display_name || row.username || '（未知用户）' }}</div>
              <div class="mem-user-raw">{{ row.username || row.user_id }}</div>
            </template>
          </el-table-column>
          <el-table-column label="偏好项" min-width="160">
            <template #default="{ row }">
              <div>{{ prefKeyLabel(row.key) }}</div>
              <div class="mem-mono">{{ row.key }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="value" label="值" min-width="200" show-overflow-tooltip />
          <el-table-column label="来源" width="110">
            <template #default="{ row }">
              <el-tag :type="row.source === 'user' ? 'primary' : 'info'" effect="plain" size="small">
                {{ row.source === 'user' ? '用户设置' : '系统学习' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="置信度" width="90">
            <template #default="{ row }">{{ fmtConfidence(row.confidence) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.status === 'active' ? 'success' : 'info'" effect="plain" size="small">
                {{ row.status === 'active' ? '生效中' : '已关闭' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">{{ fmtTime(row.updated_at || row.created_at) }}</template>
          </el-table-column>
          <el-table-column v-if="canWriteMemory()" label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button link type="danger" @click="onRemoveL2(row)">删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <div class="mem-empty">暂无用户偏好数据。用户可在「我的偏好」页设置，或点赞后由系统学习。</div>
          </template>
        </el-table>
      </el-tab-pane>

      <!-- ---------- L3 长期记忆 ---------- -->
      <el-tab-pane label="长期记忆（L3）" name="l3">
        <div class="mem-toolbar">
          <el-select v-model="filterL3.type" class="mem-select" clearable placeholder="类型"
                      @change="setL3Filter({})">
            <el-option v-for="(label, val) in L3_TYPE_LABEL" :key="val" :label="label" :value="val" />
          </el-select>
          <el-select v-model="filterL3.status" class="mem-select" clearable placeholder="状态"
                      @change="setL3Filter({})">
            <el-option v-for="(label, val) in L3_STATUS_LABEL" :key="val" :label="label" :value="val" />
          </el-select>
          <el-input
            v-model="filterL3.keyword" class="mem-search" clearable
            placeholder="匹配问题 / 答案 / 标签"
            @keyup.enter="setL3Filter({})" @clear="setL3Filter({})"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-checkbox v-model="filterL3.include_few_shot" @change="setL3Filter({})">
            包含点赞样本镜像
          </el-checkbox>
          <el-button :icon="Refresh" :loading="l3.loading" @click="loadL3Memories()">刷新</el-button>
          <el-button v-if="canWriteMemory()" type="primary" :icon="Plus" @click="openCreate">
            录入知识
          </el-button>
        </div>

        <el-alert
          title="L3 长期记忆为团队/域级沉淀，审核通过（approved）后才参与召回注入；点赞产生的问答对会镜像到本表 type=few_shot，样本本身的维护请用「Few-Shot 管理」页。"
          type="info" :closable="false" show-icon class="mem-alert"
        />

        <el-table :data="l3.items" v-loading="l3.loading" border class="mem-table">
          <el-table-column label="类型" width="120">
            <template #default="{ row }">
              <el-tag :type="row.type === 'few_shot' ? 'info' : 'primary'" effect="plain" size="small">
                {{ l3TypeLabel(row.type) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="question" label="问题 / 场景" min-width="220" show-overflow-tooltip />
          <el-table-column prop="answer" label="答案 / 规范" min-width="240" show-overflow-tooltip />
          <el-table-column label="意图" width="110" show-overflow-tooltip>
            <template #default="{ row }">{{ row.intent || '-' }}</template>
          </el-table-column>
          <el-table-column label="标签" width="140" show-overflow-tooltip>
            <template #default="{ row }">{{ row.tags || '-' }}</template>
          </el-table-column>
          <el-table-column label="权重" width="80">
            <template #default="{ row }">{{ row.weight }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="statusTagType(row.status)" effect="plain" size="small">
                {{ l3StatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="创建人" width="130" show-overflow-tooltip>
            <template #default="{ row }">{{ row.created_by || '-' }}</template>
          </el-table-column>
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <template v-if="canWriteMemory()">
                <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
                <el-button v-if="row.status === 'pending'" link type="success"
                           @click="onAudit(row, 'approved')">通过</el-button>
                <el-button v-if="row.status === 'pending'" link type="warning"
                           @click="onAudit(row, 'rejected')">驳回</el-button>
                <el-button link type="danger" @click="onRemoveL3(row)">删除</el-button>
              </template>
              <span v-else class="mem-readonly">只读</span>
            </template>
          </el-table-column>
          <template #empty>
            <div class="mem-empty">
              暂无长期记忆。可点击「录入知识」沉淀口径类规则 / 术语解释 / 常见纠错。
            </div>
          </template>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- L1 摘要全文 -->
    <el-drawer v-model="summaryDrawer" title="会话摘要全文" size="520px">
      <div v-if="currentSummary" class="mem-drawer">
        <div class="md-row"><span class="md-k">会话</span>{{ currentSummary.session_title || currentSummary.session_id }}</div>
        <div class="md-row"><span class="md-k">归属用户</span>{{ currentSummary.user_id || '-' }}</div>
        <div class="md-row"><span class="md-k">更新时间</span>{{ fmtTime(currentSummary.updated_at) }}</div>
        <div class="md-row"><span class="md-k">覆盖至</span><span class="mem-mono">{{ currentSummary.upto_message_id || '-' }}</span></div>
        <pre class="md-body">{{ currentSummary.summary }}</pre>
      </div>
    </el-drawer>

    <!-- L3 录入 / 编辑 -->
    <el-drawer v-model="l3Drawer" :title="l3Form.id ? '编辑长期记忆' : '录入长期记忆'" size="560px">
      <el-form label-width="88px" class="mem-form">
        <el-form-item label="类型">
          <el-select v-model="l3Form.type" style="width: 100%">
            <el-option label="规则（rule）" value="rule" />
            <el-option label="术语（glossary）" value="glossary" />
            <el-option label="纠错（correction）" value="correction" />
            <el-option label="点赞样本（few_shot）" value="few_shot" />
          </el-select>
        </el-form-item>
        <el-form-item label="域">
          <el-input v-model="l3Form.domain" placeholder="metric_query" />
        </el-form-item>
        <el-form-item label="问题 / 场景">
          <el-input v-model="l3Form.question" type="textarea" :rows="3"
                    placeholder="如：客户问「日均」时指什么口径" />
        </el-form-item>
        <el-form-item label="答案 / 规范">
          <el-input v-model="l3Form.answer" type="textarea" :rows="4"
                    placeholder="写成可直接照做的口径说明或规则" />
        </el-form-item>
        <el-form-item label="意图">
          <el-input v-model="l3Form.intent" placeholder="metric_query / diagnosis / knowledge_qa（可留空）" />
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="l3Form.tags" placeholder="逗号分隔，如：口径,存款" />
        </el-form-item>
        <el-form-item label="权重">
          <el-input-number v-model="l3Form.weight" :min="0" :max="10" :step="0.5" />
          <span class="mem-hint">召回重排时按权重放大（0.5~3 倍生效区间）</span>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="l3Form.status" style="width: 100%">
            <el-option label="已通过（参与召回）" value="approved" />
            <el-option label="待审核" value="pending" />
            <el-option label="已驳回" value="rejected" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="l3Drawer = false">取消</el-button>
        <el-button type="primary" :loading="mem.saving" @click="onSubmitL3">保存</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import {
  store, loadMemoryPage, loadL1Summaries, clearL1Memory, loadL2Preferences, setL2Filter,
  removeL2Preference, loadL3Memories, setL3Filter, createL3Memory, updateL3Memory,
  removeL3Memory, canWriteMemory, l3TypeLabel, l3StatusLabel, prefKeyLabel,
  toggleMemoryDisabled, L3_TYPE_LABEL, L3_STATUS_LABEL
} from '../store'

const mem = store.memoryPage
const l1 = mem.l1
const l2 = mem.l2
const l3 = mem.l3

const activeTab = ref(mem.tab || 'l1')
const filterL1 = ref(l1.keyword || '')
const filterL2 = reactive({ ...l2.filters })
const filterL3 = reactive({ ...l3.filters })

const summaryDrawer = ref(false)
const currentSummary = ref(null)
const l3Drawer = ref(false)
const l3Form = reactive({
  id: 0, domain: 'metric_query', type: 'rule', question: '', answer: '',
  intent: '', tags: '', weight: 1.0, status: 'approved'
})

const memoryOn = computed({
  get: () => !store.prefs.disabled,
  set: (v) => { store.prefs.disabled = !v }
})

/* 统计卡：L3 显示"管理员维护型"条数（已剔除点赞镜像），并额外提示待审数量 */
const statCards = computed(() => {
  const s = mem.stats || {}
  const l3s = s.l3 || {}
  const few = s.few_shot || {}
  return [
    { key: 'l1', label: '会话记忆 L1', value: (s.l1 && s.l1.summaries) ?? '-', hint: '滚动摘要（每会话一条）' },
    { key: 'l2', label: '用户记忆 L2', value: (s.l2 && s.l2.preferences) ?? '-', hint: '用户偏好（个人维度）' },
    {
      key: 'l3',
      label: '长期记忆 L3',
      value: l3s.curated ?? '-',
      hint: `规则/术语/纠错（待审 ${l3s.pending ?? 0}）`
    },
    {
      key: 'few',
      label: 'Few-Shot 样本',
      value: few.total ?? '-',
      hint: `启用 ${few.active ?? 0} · 点赞 ${few.like ?? 0} / 手工 ${few.manual ?? 0}`
    }
  ]
})

function fmtTime(t) {
  if (!t) return '-'
  try { return new Date(t).toLocaleString('zh-CN', { hour12: false }) } catch (e) { return t }
}
function fmtConfidence(c) {
  if (c == null) return '-'
  const n = Number(c)
  return isFinite(n) ? Math.round(n * 100) + '%' : String(c)
}
function shortId(id) {
  if (!id) return '-'
  return id.length > 10 ? id.slice(0, 8) + '…' : id
}
function statusTagType(s) {
  if (s === 'approved') return 'success'
  if (s === 'rejected') return 'danger'
  return 'warning'
}

async function onRefresh() {
  await loadMemoryPage()
  await Promise.all([loadL1Summaries(), loadL2Preferences(), loadL3Memories()]).catch(() => {})
  ElMessage.success('已刷新')
}

function onTabChange(name) {
  mem.tab = name
  if (name === 'l1') loadL1Summaries()
  if (name === 'l2') loadL2Preferences()
  if (name === 'l3') loadL3Memories()
}

/* ---- L1 ---- */
function openSummary(row) {
  currentSummary.value = row
  summaryDrawer.value = true
}

async function onClearL1(row) {
  const ok = await ElMessageBox.confirm(
    `清空会话「${row.session_title || row.session_id}」的记忆摘要？清空后该会话将不再有历史摘要上下文。`,
    '清空确认',
    { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    await clearL1Memory(row.session_id)
    ElMessage.success('已清空该会话记忆')
    summaryDrawer.value = false
  } catch (e) { ElMessage.error(e.message || '清空失败') }
}

/* ---- L2 ---- */
async function onRemoveL2(row) {
  const ok = await ElMessageBox.confirm(
    `删除用户「${row.display_name || row.username || row.user_id}」的偏好「${prefKeyLabel(row.key)}」？`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    await removeL2Preference(row.id)
    ElMessage.success('已删除')
  } catch (e) { ElMessage.error(e.message || '删除失败') }
}

/* ---- L3 ---- */
function resetL3Form() {
  Object.assign(l3Form, {
    id: 0, domain: 'metric_query', type: 'rule', question: '', answer: '',
    intent: '', tags: '', weight: 1.0, status: 'approved'
  })
}
function openCreate() {
  resetL3Form()
  l3Drawer.value = true
}
function openEdit(row) {
  Object.assign(l3Form, {
    id: row.id, domain: row.domain, type: row.type, question: row.question,
    answer: row.answer, intent: row.intent || '', tags: row.tags || '',
    weight: row.weight, status: row.status
  })
  l3Drawer.value = true
}

async function onSubmitL3() {
  if (!l3Form.question && !l3Form.answer) {
    ElMessage.warning('问题与答案不能同时为空')
    return
  }
  const body = {
    domain: l3Form.domain || 'metric_query',
    type: l3Form.type,
    question: l3Form.question,
    answer: l3Form.answer,
    intent: l3Form.intent || null,
    tags: l3Form.tags || '',
    weight: Number(l3Form.weight) || 1.0,
    status: l3Form.status
  }
  try {
    if (l3Form.id) {
      await updateL3Memory(l3Form.id, body)
      ElMessage.success('已保存')
    } else {
      await createL3Memory(body)
      ElMessage.success('已录入')
    }
    l3Drawer.value = false
  } catch (e) { ElMessage.error(e.message || '保存失败') }
}

async function onAudit(row, status) {
  try {
    await updateL3Memory(row.id, { status })
    ElMessage.success(status === 'approved' ? '已通过' : '已驳回')
  } catch (e) { ElMessage.error(e.message || '审核失败') }
}

async function onRemoveL3(row) {
  const ok = await ElMessageBox.confirm(
    `删除这条长期记忆（${l3TypeLabel(row.type)}：${row.question || row.answer}）？`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    await removeL3Memory(row.id)
    ElMessage.success('已删除')
  } catch (e) { ElMessage.error(e.message || '删除失败') }
}

/* ---- 记忆开关 ---- */
async function onToggleMemory(val) {
  try {
    await toggleMemoryDisabled(!val)
    ElMessage.success(val ? '已开启记忆注入' : '已关闭记忆注入')
  } catch (e) {
    store.prefs.disabled = val // 回滚
    ElMessage.error(e.message || '操作失败')
  }
}

/* 过滤条件回写到 store（store 是唯一数据源，切页返回时不丢过滤条件） */
watch(filterL1, (v) => { l1.keyword = v })
watch(filterL2, (v) => { Object.assign(l2.filters, v) })
watch(filterL3, (v) => { Object.assign(l3.filters, v) }, { deep: true })

onMounted(() => {
  if (!mem.stats) loadMemoryPage()
  else loadL1Summaries()
})
</script>

<style scoped>
/* 视图页根容器必须是滚动容器（body overflow:hidden + .main 不可滚） */
.mem-page {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  position: relative;
  padding: 18px 22px 28px;
  box-sizing: border-box;
}
.mem-page > * { flex: none; }
.mem-head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 12px; }
.mem-title { margin: 0; font-size: 18px; font-weight: 600; color: #1f2329; }
.mem-sub { font-size: 12px; color: #8a9099; margin-top: 4px; max-width: 760px; line-height: 1.6; }
.mem-head-actions { display: flex; align-items: center; gap: 10px; }
.mem-alert { margin: 6px 0; }

.mem-stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0 4px; }
.mem-stat {
  flex: 1 1 180px; min-width: 170px; background: var(--card, #fff);
  border: 1px solid var(--border, #e8eaee); border-radius: 10px; padding: 12px 14px;
}
.ms-label { font-size: 12px; color: #8a9099; }
.ms-value { font-size: 22px; font-weight: 600; color: #1f2329; margin: 4px 0 2px; }
.ms-hint { font-size: 11px; color: #a8adb5; }

.mem-tabs { margin-top: 6px; }
.mem-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.mem-search { width: 260px; }
.mem-select { width: 140px; }
.mem-hint { font-size: 12px; color: #8a9099; }
.mem-table { width: 100%; }
.mem-summary {
  font-size: 12px; color: #4e5969; line-height: 1.55;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.mem-mono { font-size: 11px; color: #a8adb5; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.mem-user { font-size: 13px; color: #1f2329; }
.mem-user-raw { font-size: 11px; color: #a8adb5; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.mem-empty { color: #8a9099; font-size: 13px; padding: 16px 0; line-height: 1.6; }
.mem-readonly { font-size: 12px; color: #a8adb5; }

.mem-drawer { padding: 4px 18px; }
.md-row { font-size: 13px; color: #1f2329; margin-bottom: 8px; }
.md-k { display: inline-block; width: 76px; color: #8a9099; font-size: 12px; }
.md-body {
  margin-top: 12px; padding: 12px; background: #f7f8fa; border-radius: 8px;
  font-size: 13px; line-height: 1.7; color: #1f2329; white-space: pre-wrap;
  font-family: inherit; max-height: 60vh; overflow: auto;
}
.mem-form { padding: 0 8px; }
</style>
