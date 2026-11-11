<template>
  <el-card class="src-card" shadow="never">
    <template #header>
      <div class="src-hd">
        <span class="src-title">语义层来源</span>
        <el-tag v-if="loaded" :type="sourceTag.type" size="small" effect="light">
          {{ sourceTag.text }}
        </el-tag>
        <el-tag v-if="stale" type="warning" size="small" effect="dark">有更新未加载</el-tag>
        <el-tag v-if="pendingReview" type="warning" size="small" effect="plain">待审 {{ pendingReview }}</el-tag>
        <span class="src-spacer" />
        <el-button size="small" :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button
          v-if="admin" size="small" type="primary" plain :icon="MagicStick"
          :loading="reloading" @click="onReload"
        >热更新</el-button>
        <el-button
          v-if="admin" size="small" plain :loading="publishing" @click="onPublishDraft"
        >发布为草稿</el-button>
        <el-button
          v-if="admin" size="small" type="warning" plain :icon="Edit" @click="editorVisible = true"
        >租户覆盖</el-button>
      </div>
    </template>

    <el-alert
      v-if="error" :title="error" type="error" :closable="false" show-icon class="src-alert"
    />

    <el-alert
      v-if="stale" type="warning" show-icon :closable="false" class="src-alert"
      title="DB 里有更新的已发布快照尚未被当前进程加载"
      description="取数仍在用旧定义。点「热更新」立即生效（不必重启）。"
    />
    <el-alert
      v-else-if="fellBack" type="warning" show-icon :closable="false" class="src-alert"
      title="配置要求 DB 快照，但没有可用的 published 快照"
      description="已自动回落 YAML 基座，取数不受影响。发布并审批一份快照即可切到 DB 源。"
    />

    <div v-if="loaded" class="src-grid">
      <div class="src-item">
        <span class="k">实际生效</span>
        <span class="v">{{ loaded.effective_source === 'db' ? 'DB 发布快照' : 'YAML 基座' }}</span>
      </div>
      <div class="src-item">
        <span class="k">配置要求</span>
        <span class="v">DMF_SEMANTIC-SOURCE={{ loaded.requested_source }}</span>
      </div>
      <div class="src-item">
        <span class="k">生效范围</span>
        <span class="v">{{ scopeText }}</span>
      </div>
      <div class="src-item">
        <span class="k">租户</span>
        <span class="v">{{ loaded.tenant_id || '（全局）' }}</span>
      </div>
      <div class="src-item">
        <span class="k">基座版本</span>
        <span class="v">{{ loaded.base_version == null ? '—' : 'v' + loaded.base_version }}</span>
      </div>
      <div class="src-item">
        <span class="k">叠加 overlay</span>
        <span class="v">{{ loaded.overlay_versions.length ? loaded.overlay_versions.map(v => 'v' + v).join(' / ') : '无' }}</span>
      </div>
      <div class="src-item">
        <span class="k">模型 / 指标 / 维度</span>
        <span class="v">{{ loaded.models }} / {{ loaded.metrics }} / {{ loaded.dimensions }}</span>
      </div>
      <div class="src-item">
        <span class="k">加载时间</span>
        <span class="v">{{ loaded.loaded_at }}</span>
      </div>
      <div class="src-item wide">
        <span class="k">清单路径</span>
        <span class="v mono">{{ loaded.manifest_path }}</span>
      </div>
      <div v-if="yamlBase && yamlBase.metrics != null" class="src-item wide">
        <span class="k">YAML 基座</span>
        <span class="v">{{ yamlBase.models }} 模型 / {{ yamlBase.metrics }} 指标</span>
      </div>
    </div>

    <div class="src-section">
      生效版本链
      <span class="src-hint">（版本号是租户内独立序列，全局与租户同名版本并不相同）</span>
    </div>
    <el-table v-if="chain.length" :data="chain" size="small" border class="src-table">
      <el-table-column prop="scope" label="范围" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="row.scope === 'global' ? 'info' : 'success'" effect="plain">
            {{ row.scope === 'global' ? '全局' : '租户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="version" label="版本" width="70">
        <template #default="{ row }">v{{ row.version }}</template>
      </el-table-column>
      <el-table-column prop="mode" label="模式" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.mode === 'overlay' ? 'warning' : 'primary'" effect="plain">
            {{ row.mode === 'overlay' ? '差异覆盖' : '整份' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="state" label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="stateTag(row.state)">{{ stateText(row.state) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_by" label="操作人" min-width="120" show-overflow-tooltip />
    </el-table>
    <el-empty
      v-else description="当前生效的是 YAML 基座，没有 DB 快照参与合并"
      :image-size="56"
    />

    <div class="src-section">
      多租户请求级装配
      <span class="src-hint">
        （按请求解析本租户的层；链指纹未变即复用，不重复反序列化）
      </span>
    </div>
    <template v-if="scopeCache && scopeCache.enabled">
      <div class="src-grid">
        <div class="src-item">
          <span class="k">缓存租户</span>
          <span class="v">{{ scopeCache.cached_tenants }} / {{ scopeCache.max_entries }}</span>
        </div>
        <div class="src-item">
          <span class="k">重建 / 命中</span>
          <span class="v">{{ scopeCache.builds }} / {{ scopeCache.hits }}</span>
        </div>
        <div class="src-item">
          <span class="k">解析异常</span>
          <span class="v">{{ scopeCache.errors }}</span>
        </div>
      </div>
      <el-table
        v-if="(scopeCache.tenants || []).length" :data="scopeCache.tenants"
        size="small" border class="src-table"
      >
        <el-table-column prop="tenant_id" label="租户" min-width="120" show-overflow-tooltip />
        <el-table-column prop="scope" label="生效范围" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.scope === 'tenant' ? 'success' : 'info'" effect="plain">
              {{ row.scope === 'tenant' ? '全局链 + 覆盖' : '全局链' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="base_version" label="基座" width="80">
          <template #default="{ row }">
            {{ row.base_version == null ? '—' : 'v' + row.base_version }}
          </template>
        </el-table-column>
        <el-table-column label="叠加覆盖" width="110">
          <template #default="{ row }">
            {{ (row.overlay_versions || []).length ? row.overlay_versions.map(v => 'v' + v).join(' / ') : '无' }}
          </template>
        </el-table-column>
        <el-table-column prop="snapshots" label="链上快照" width="90" />
      </el-table>
      <el-empty
        v-else description="本进程还没有租户请求，或所有租户都与全局同链"
        :image-size="56"
      />
    </template>
    <el-alert
      v-else-if="scopeCache" type="info" :closable="false" show-icon class="src-alert"
      title="当前未启用请求级租户装配"
      description="DMF_SEMANTIC-SOURCE=yaml 时不解析租户（YAML 没有租户概念）；切到 db 后按租户生效。"
    />

    <div class="src-section">
      快照版本历史
      <el-select
        v-if="admin"
        v-model="histTenant"
        size="small"
        clearable
        placeholder="全部租户（跨租户审批）"
        class="src-scope"
        style="width: 190px"
      >
        <el-option label="全部租户（跨租户）" value="" />
        <el-option
          v-for="t in tenants"
          :key="t.id || t"
          :label="t.name || t.code || t.id || t"
          :value="t.id || t"
        />
      </el-select>
      <span v-else class="src-scope-hint">仅本租户 + 全局（只读）</span>
      <el-radio-group v-model="histState" size="small" class="src-scope">
        <el-radio-button label="">全部</el-radio-button>
        <el-radio-button label="draft">草稿</el-radio-button>
        <el-radio-button label="review">待审</el-radio-button>
        <el-radio-button label="published">已发布</el-radio-button>
      </el-radio-group>
    </div>
    <div v-if="admin" class="src-batch">
      <span class="src-batch-hint">已选 {{ selected.length }} 项待审快照（仅「待审」可勾选）</span>
      <el-button
        size="small" type="primary" :disabled="!selected.length" :loading="batching"
        @click="batchAct('approve')"
      >批量通过</el-button>
      <el-button
        size="small" type="warning" :disabled="!selected.length" :loading="batching"
        @click="batchAct('reject')"
      >批量驳回</el-button>
    </div>
    <el-table
      v-loading="loading" :data="snapshots" size="small" border class="src-table"
      :empty-text="loading ? '加载中…' : '暂无快照'"
      @selection-change="onSelectionChange"
    >
      <el-table-column
        v-if="admin" type="selection" width="42"
        :selectable="(row) => row.state === 'review'"
      />
      <el-table-column prop="id" label="#" width="60" />
      <el-table-column prop="version" label="版本" width="70">
        <template #default="{ row }">v{{ row.version }}</template>
      </el-table-column>
      <el-table-column prop="mode" label="模式" width="120">
        <template #default="{ row }">
          <el-tooltip v-if="row.mode === 'overlay'" placement="top">
            <template #content>
              覆盖模型：{{ (row.overlay?.model_names || []).join('、') || '无' }}<br>
              覆盖指标：{{ (row.overlay?.metric_names || []).join('、') || '无' }}
            </template>
            <el-tag size="small" type="warning" effect="plain">差异覆盖 ×{{ row.overlay?.metric_count ?? row.metric_count }}</el-tag>
          </el-tooltip>
          <el-tag v-else size="small" type="primary" effect="plain">整份</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="内容" width="120">
        <template #default="{ row }">{{ row.model_count }} 模型 / {{ row.metric_count }} 指标</template>
      </el-table-column>
      <el-table-column prop="state" label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="stateTag(row.state)">{{ stateText(row.state) }}</el-tag>
          <el-tooltip v-if="row.reject_reason" placement="top">
            <template #content>
              驳回人：{{ row.rejected_by || '—' }}<br>原因：{{ row.reject_reason }}
            </template>
            <el-tag size="small" type="danger" effect="plain" class="src-reject-tag">已驳回</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="created_by" label="创建人" width="110" show-overflow-tooltip />
      <el-table-column prop="created_at" label="创建时间" min-width="160" show-overflow-tooltip />
      <el-table-column label="操作" width="280" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.state === 'draft' && canWrite" link type="primary" size="small"
            @click="act('submit', row)"
          >提交审核</el-button>
          <el-button
            v-if="row.state === 'review' && admin" link type="primary" size="small"
            @click="act('approve', row)"
          >审批发布</el-button>
          <el-button
            v-if="row.state === 'review' && admin" link type="warning" size="small"
            @click="act('reject', row)"
          >驳回</el-button>
          <el-button
            v-if="row.state === 'draft'" link type="info" size="small"
            @click="act('deprecate', row)"
          >放弃</el-button>
          <el-button
            v-if="row.state === 'published'" link type="danger" size="small"
            @click="act('deprecate', row)"
          >废弃</el-button>
          <el-button
            v-if="row.state === 'deprecated' && canWrite" link type="primary" size="small"
            @click="act('revive', row)"
          >复活为草稿</el-button>
          <el-button
            v-if="admin && row.state !== 'draft'" link type="warning" size="small"
            @click="act('rollback', row)"
          >回滚</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 给某租户发 overlay：选租户 → 选基座条目 → 改字段 → 差异预览 → 提交草稿 -->
    <SemanticOverlayEditor
      v-model="editorVisible" :tenants="tenants" :default-tenant="tenantId"
      @published="load"
    />
  </el-card>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Edit, MagicStick, Refresh } from '@element-plus/icons-vue'
import * as api from '../utils/api'
import { hasPerm, isAdmin } from '../store'
import SemanticOverlayEditor from './SemanticOverlayEditor.vue'

const admin = computed(() => isAdmin())
const canWrite = computed(() => hasPerm('semantic:write') || hasPerm('semantic'))

const loading = ref(false)
const reloading = ref(false)
const publishing = ref(false)
const error = ref('')
const info = ref(null)
const snapshots = ref([])
const histTenant = ref('')  // 仅 admin 用：''=全部租户（跨租户审批）/ 具体 id=全局+该租户；非 admin 恒由后端收敛为本租户+全局
const histState = ref('')
/* overlay 编辑面：租户下拉要一份租户清单（admin 才拉得到，失败就留空由后端兜底） */
const editorVisible = ref(false)
const tenants = ref([])
/* 批量审批（仅 admin）：快照表勾选态 + 进行中标志 */
const selected = ref([])
const batching = ref(false)
const onSelectionChange = (rows) => { selected.value = rows || [] }

const loaded = computed(() => (info.value ? info.value.loaded : null))
const yamlBase = computed(() => (info.value ? info.value.yaml_base : null))
const stale = computed(() => !!(info.value && info.value.stale))
const fellBack = computed(() => !!(loaded.value && loaded.value.fell_back))
const chain = computed(() => (loaded.value ? loaded.value.chain || [] : []))
const tenantId = computed(() => (loaded.value ? loaded.value.tenant_id || '' : ''))
/* 请求级租户装配概况（后端 /runtime 的 scope_cache）：多租户下"谁在用哪份定义"
   不该只靠推演，面板要能直接看见 */
const scopeCache = computed(() => (info.value ? info.value.scope_cache : null))

/* 待审快照数（全局 + 当前租户）：审批流里唯一需要"有人去做"的状态，值得在标题上提示 */
const pendingReview = computed(() => {
  const latest = info.value ? info.value.latest : null
  if (!latest) return 0
  const g = (latest.global_state_counts || {}).review || 0
  const t = (latest.tenant_state_counts || {}).review || 0
  return g + t
})

const sourceTag = computed(() => {
  if (!loaded.value) return { type: 'info', text: '未知' }
  if (loaded.value.effective_source === 'yaml') {
    return loaded.value.fell_back
      ? { type: 'warning', text: 'YAML 基座（已回落）' }
      : { type: 'info', text: 'YAML 基座' }
  }
  return loaded.value.scope === 'tenant'
    ? { type: 'success', text: 'DB 快照 · 租户覆盖' }
    : { type: 'primary', text: 'DB 快照 · 全局' }
})

const scopeText = computed(() => {
  const s = loaded.value && loaded.value.scope
  if (s === 'tenant') return '全局链 + 租户 overlay'
  if (s === 'global') return '全局链'
  return 'YAML 清单目录'
})

const STATE_TEXT = { draft: '草稿', review: '待审', published: '已发布', deprecated: '已废弃' }
const STATE_TAG = { draft: 'info', review: 'warning', published: 'success', deprecated: 'danger' }
const stateText = (s) => STATE_TEXT[s] || s || '-'
const stateTag = (s) => STATE_TAG[s] || 'info'

async function load() {
  loading.value = true
  error.value = ''
  try {
    info.value = await api.semanticRuntime()
    // admin：按下拉选择的租户作用域（''=全部租户）；非 admin：恒由后端收敛为本租户+全局
    const tenant = admin.value ? (histTenant.value || '') : ''
    const res = await api.listSnapshots(tenant, histState.value, 50)
    snapshots.value = res.items || []
    selected.value = []  // 列表刷新后旧勾选可能已失效，清空避免误批
  } catch (e) {
    error.value = '语义层来源读取失败：' + (e && e.message ? e.message : e)
  } finally {
    loading.value = false
  }
}

watch([histTenant, histState], () => { load() })

/* 租户清单只在真正要发 overlay 时拉一次：非 admin 拉不到（403），
   此时下拉由后端强制归属兜底，界面上也不展示可选项。 */
async function loadTenants() {
  if (!admin.value || tenants.value.length) return
  try {
    const res = await api.listTenants()
    tenants.value = res.items || res || []
  } catch {
    tenants.value = []
  }
}
watch(editorVisible, (v) => { if (v) loadTenants() })

async function onReload() {
  reloading.value = true
  try {
    const res = await api.reloadSemantic()
    ElMessage.success(
      `语义层已热更新：${res.runtime.effective_source === 'db' ? 'DB 快照' : 'YAML 基座'}，指标 ${res.runtime.metrics} 个`
    )
    await load()
  } catch (e) {
    ElMessage.error('热更新失败：' + (e && e.message ? e.message : e))
  } finally {
    reloading.value = false
  }
}

async function onPublishDraft() {
  try {
    await ElMessageBox.confirm(
      '将把当前 YAML 基座发布为一份**草稿**快照（需再经提交审核 → 管理员审批才会生效）。是否继续？',
      '发布为草稿',
      { type: 'info', confirmButtonText: '发布', cancelButtonText: '取消' }
    )
  } catch (e) {
    return
  }
  publishing.value = true
  try {
    const res = await api.publishSnapshot({ tenant_id: tenantId.value, mode: 'full', state: 'draft' })
    ElMessage.success(`已生成草稿快照 v${res.snapshot.version}`)
    histState.value = 'draft'
    await load()
  } catch (e) {
    ElMessage.error('发布失败：' + (e && e.message ? e.message : e))
  } finally {
    publishing.value = false
  }
}

const ACT_LABEL = {
  submit: '提交审核',
  approve: '审批发布',
  reject: '驳回',
  deprecate: '废弃',
  revive: '复活为草稿',
  rollback: '回滚到此版本'
}
const ACT_WARN = {
  approve: '审批通过后该快照立即对取数生效（并热更新当前进程）。',
  deprecate: '废弃后运行时将退回到上一个已发布版本。',
  rollback: '将把该版本内容作为**新版本**写入（历史版本保留，不做物理回退）。'
}

/* 驳回原因弹窗：必填（后端 SnapshotRejectRequest.reason min_length=1）。
 * 取消返回 null，调用方据此中止。 */
async function promptRejectReason(title = '驳回快照') {
  try {
    const { value } = await ElMessageBox.prompt('请填写驳回原因（将记录驳回人与原因，供起草人修订）', title, {
      type: 'warning',
      confirmButtonText: '确认驳回',
      cancelButtonText: '取消',
      inputType: 'textarea',
      inputPlaceholder: '例如：口径与总行口径冲突，请对齐后再提',
      inputValidator: (v) => (v && v.trim() ? true : '驳回原因不能为空')
    })
    return (value || '').trim()
  } catch (e) {
    return null
  }
}

async function act(kind, row) {
  let reason = null
  if (kind === 'reject') {
    reason = await promptRejectReason(`驳回快照 v${row.version}`)
    if (!reason) return
  } else if (ACT_WARN[kind]) {
    try {
      await ElMessageBox.confirm(ACT_WARN[kind], ACT_LABEL[kind], {
        type: kind === 'approve' ? 'info' : 'warning',
        confirmButtonText: ACT_LABEL[kind],
        cancelButtonText: '取消'
      })
    } catch (e) {
      return
    }
  }
  try {
    if (kind === 'submit') await api.submitSnapshot(row.id)
    else if (kind === 'approve') await api.approveSnapshot(row.id)
    else if (kind === 'reject') await api.rejectSnapshot(row.id, reason)
    else if (kind === 'deprecate') await api.deprecateSnapshot(row.id)
    else if (kind === 'revive') await api.reviveSnapshot(row.id)
    else if (kind === 'rollback') await api.rollbackSnapshot(row.id)
    ElMessage.success(`${ACT_LABEL[kind]}成功（v${row.version}）`)
    await load()
  } catch (e) {
    ElMessage.error(`${ACT_LABEL[kind]}失败：` + (e && e.message ? e.message : e))
  }
}

/* 批量通过 / 驳回：对勾选的待审快照逐条执行，后端容错并回传失败明细。 */
async function batchAct(action) {
  const ids = selected.value.map((r) => r.id)
  if (!ids.length) return
  const label = action === 'approve' ? '批量通过' : '批量驳回'
  let reason = null
  if (action === 'reject') {
    reason = await promptRejectReason(`批量驳回 ${ids.length} 份快照`)
    if (!reason) return
  } else {
    try {
      await ElMessageBox.confirm(
        `将通过 ${ids.length} 份待审快照，通过后立即对取数生效。是否继续？`, label,
        { type: 'info', confirmButtonText: '确认通过', cancelButtonText: '取消' }
      )
    } catch (e) {
      return
    }
  }
  batching.value = true
  try {
    const res = await api.batchSnapshots(action, ids, reason)
    const ok = (action === 'approve' ? res.approved : res.rejected) || []
    const failed = res.failed || []
    if (failed.length) {
      const detail = failed.map((f) => `#${f.id}：${f.error}`).join('；')
      ElMessage.warning(`${label}：成功 ${ok.length} 条，失败 ${failed.length} 条（${detail}）`)
    } else {
      ElMessage.success(`${label}成功：共 ${ok.length} 条`)
    }
    await load()
  } catch (e) {
    ElMessage.error(`${label}失败：` + (e && e.message ? e.message : e))
  } finally {
    batching.value = false
  }
}

onMounted(load)
// admin 进入面板即预拉租户清单，供「跨租户审批」下拉使用（非 admin 拉不到、也不展示）
if (admin.value) onMounted(loadTenants)
</script>

<style scoped>
.src-card { border-radius: 8px; }
.src-batch {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin: 4px 0 10px;
}
.src-batch-hint { font-size: 12px; color: var(--el-text-color-secondary); }
.src-reject-tag { margin-left: 6px; }
.src-hd { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.src-title { font-weight: 600; }
.src-spacer { flex: 1; }
.src-alert { margin-bottom: 12px; border-radius: 8px; }

.src-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px 18px; margin-bottom: 6px;
}
.src-item { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.src-item.wide { grid-column: 1 / -1; }
.src-item .k { font-size: 12px; color: var(--muted); }
.src-item .v { font-size: 13px; color: var(--text); word-break: break-all; }
.src-item .v.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }

.src-section {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 16px 0 8px; font-weight: 600; font-size: 13px;
}
.src-hint { font-weight: 400; font-size: 12px; color: var(--muted); }
.src-scope { margin-left: auto; }
.src-table { width: 100%; }
</style>
