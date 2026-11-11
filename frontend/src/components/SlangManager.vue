<template>
  <div class="sl-page">
    <div class="sl-head">
      <div>
        <h2 class="sl-title">黑话词典管理</h2>
        <div class="sl-sub">
          三级作用域（同名以高层级为准）：<b>用户级</b> &gt; <b>公司级</b> &gt; <b>行业级</b>。
          意图识别 / 要素提取之前，先用本词典做黑话替换与 query 改写；读路径 <b>Redis 优先</b>，
          写路径 <b>先写 MySQL 再同步 Redis</b> —— 发布一条，下一句对话立刻生效。
        </div>
      </div>
      <div class="sl-head-actions">
        <el-button :icon="MagicStick" @click="openPreview()">在线试改写</el-button>
        <el-button :icon="Download" :loading="exporting" @click="onExport()">导出</el-button>
        <el-button v-if="writable" :icon="Upload" @click="openImport()">导入</el-button>
        <el-button v-if="writable" type="primary" :icon="Plus" @click="openCreate()">新增词条</el-button>
        <el-button :icon="Refresh" :loading="sp.loading" @click="onRefresh()">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="!writable"
      title="当前角色对黑话词典为只读：新增 / 编辑 / 删除 / 导入需要 slang:write，发布 / 停用 / 回滚需要 slang:review（分析师及以上；且非管理员仅能维护本人用户级词条）。"
      type="info" :closable="false" show-icon class="sl-alert"
    />
    <el-alert
      v-if="sp.error" :title="sp.error" type="error" :closable="false" show-icon class="sl-alert"
    />

    <!-- 统计卡（GET /slang/stats） -->
    <div class="sl-stats">
      <div v-for="c in statCards" :key="c.key" class="sl-stat">
        <div class="sls-label">{{ c.label }}</div>
        <div class="sls-value">{{ c.value }}</div>
        <div class="sls-hint">{{ c.hint }}</div>
      </div>
    </div>

    <div class="sl-toolbar">
      <el-input
        v-model="sp.filters.keyword" class="sl-search" clearable
        placeholder="匹配 黑话 / 标准名 / 备注"
        @keyup.enter="onFilterChange()" @clear="onFilterChange()" @input="onKeywordInput()"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-select v-model="sp.filters.scope" class="sl-select" clearable placeholder="层级"
                 @change="onFilterChange()">
        <el-option v-for="s in sp.facets.scopes" :key="s" :label="slangScopeLabel(s)" :value="s" />
      </el-select>
      <el-select v-model="sp.filters.entry_type" class="sl-select" clearable placeholder="类型"
                 @change="onFilterChange()">
        <el-option v-for="t in sp.facets.entry_types" :key="t" :label="slangTypeLabel(t)" :value="t" />
      </el-select>
      <el-select v-model="sp.filters.status" class="sl-select-narrow" clearable placeholder="状态"
                 @change="onFilterChange()">
        <el-option v-for="s in sp.facets.statuses" :key="s" :label="slangStatusLabel(s)" :value="s" />
      </el-select>
      <el-select v-model="sp.filters.source" class="sl-select-narrow" clearable placeholder="来源"
                 @change="onFilterChange()">
        <el-option v-for="s in sp.facets.sources" :key="s" :label="slangSourceLabel(s)" :value="s" />
      </el-select>
      <el-button text :icon="RefreshLeft" @click="resetSlangFilters()">重置</el-button>
      <span class="sl-hint">共 {{ sp.total }} 条 · 草稿不参与线上改写，发布后即时生效</span>
    </div>

    <el-table :data="sp.items" v-loading="sp.loading" border class="sl-table">
      <el-table-column label="ID" width="66">
        <template #default="{ row }"><span class="sl-mono">{{ row.id }}</span></template>
      </el-table-column>
      <el-table-column label="黑话 / 口语" min-width="140" show-overflow-tooltip>
        <template #default="{ row }"><span class="sl-term">{{ row.term }}</span></template>
      </el-table-column>
      <el-table-column label="标准名 / 维值" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.entry_type === 'stop'" class="sl-muted">（停用词，命中即删除）</span>
          <span v-else-if="row.entry_type === 'dim_value'">
            {{ row.standard }} = <b>{{ row.dim_value }}</b>
          </span>
          <span v-else>{{ row.standard }}</span>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="90">
        <template #default="{ row }">
          <el-tag effect="plain" size="small">{{ slangTypeLabel(row.entry_type) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="层级" width="90">
        <template #default="{ row }">
          <el-tag :type="scopeTagType(row.scope)" effect="plain" size="small">
            {{ slangScopeLabel(row.scope) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="模式" width="80">
        <template #default="{ row }">
          <span class="sl-mono">{{ slangModeLabel(row.rewrite_mode) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="优先级" width="80">
        <template #default="{ row }"><span class="sl-mono">{{ row.priority }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="92">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" effect="plain" size="small">
            {{ slangStatusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="命中" width="66">
        <template #default="{ row }"><span class="sl-mono">{{ row.hit_count }}</span></template>
      </el-table-column>
      <el-table-column label="备注" min-width="150" show-overflow-tooltip>
        <template #default="{ row }"><span class="sl-muted">{{ row.remark || '-' }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="204" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="onTryWord(row)">试改写</el-button>
          <template v-if="rowWritable(row)">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          </template>
          <el-dropdown trigger="click" class="sl-more">
            <el-button link>更多<el-icon class="sl-more-i"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="openVersions(row)">版本历史</el-dropdown-item>
                <el-dropdown-item
                  v-if="rowWritable(row) && reviewable"
                  @click="onToggle(row)"
                >{{ row.status === 'published' ? '停用' : '发布' }}</el-dropdown-item>
                <el-dropdown-item
                  v-if="rowWritable(row)" divided
                  @click="onRemove(row)"
                >删除</el-dropdown-item>
                <el-dropdown-item v-if="!rowWritable(row)" disabled>只读（无权限）</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </template>
      </el-table-column>
      <template #empty>
        <div class="sl-empty">
          暂无词条。行业级内置种子在服务启动时自动灌入；公司级 / 用户级可由管理员或本人新增。
        </div>
      </template>
    </el-table>

    <div class="sl-pager">
      <el-pagination
        layout="total, prev, pager, next, sizes"
        :total="sp.total"
        :current-page="sp.page"
        :page-size="sp.pageSize"
        :page-sizes="[10, 20, 50, 100]"
        @current-change="setSlangPage"
        @size-change="onSizeChange"
      />
    </div>

    <!-- 新增 / 编辑 -->
    <el-drawer v-model="editDrawer" :title="form.id ? '编辑词条' : '新增词条'" size="600px">
      <el-form label-width="104px" class="sl-form">
        <el-form-item label="类型">
          <el-radio-group v-model="form.entry_type">
            <el-radio value="metric">指标</el-radio>
            <el-radio value="dimension">维度</el-radio>
            <el-radio value="dim_value">枚举值</el-radio>
            <el-radio value="term">术语</el-radio>
            <el-radio value="stop">停用词</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="层级">
          <el-select v-model="form.scope" class="sl-form-wide">
            <el-option
              v-for="s in scopeOptions" :key="s" :label="slangScopeLabel(s)" :value="s"
            />
          </el-select>
          <span class="sl-hint">非管理员只能维护「用户级」（本人）</span>
        </el-form-item>
        <el-form-item label="黑话 / 口语">
          <el-input v-model="form.term" placeholder="用户会怎么叫，如：AUM / 网银 / 个贷" />
        </el-form-item>
        <el-form-item v-if="form.entry_type !== 'stop'" label="标准名">
          <el-select
            v-if="standardOptions.length" v-model="form.standard" filterable allow-create
            default-first-option class="sl-form-wide"
            :placeholder="form.entry_type === 'dim_value' ? '选择维度名' : '选择语义层标准名（可手输）'"
          >
            <el-option v-for="n in standardOptions" :key="n" :label="n" :value="n" />
          </el-select>
          <el-input v-else v-model="form.standard" placeholder="语义层标准名（须真实存在）" />
        </el-form-item>
        <el-form-item v-if="form.entry_type === 'dim_value'" label="维值">
          <el-input v-model="form.dim_value" placeholder="逗号分隔，如：手机银行 / 网上银行" />
        </el-form-item>
        <el-form-item label="改写模式">
          <el-radio-group v-model="form.rewrite_mode" :disabled="form.entry_type === 'stop'">
            <el-radio value="expand">扩展（保留原词）</el-radio>
            <el-radio value="replace">替换</el-radio>
            <el-radio value="stop">删除</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="优先级">
          <el-input-number v-model="form.priority" :min="0" :max="1000" :step="10" />
          <span class="sl-hint">越大越优先（护栏词条为 500）</span>
        </el-form-item>
        <el-form-item label="状态">
          <el-radio-group v-model="form.status">
            <el-radio value="draft">草稿</el-radio>
            <el-radio value="published">已发布</el-radio>
            <el-radio value="disabled">已停用</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.remark" type="textarea" :rows="2"
                    placeholder="口径提醒 / 出处（如 AUM 严格指管理资产规模）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDrawer = false">取消</el-button>
        <el-button type="primary" :loading="sp.saving" @click="onSubmit()">保存</el-button>
      </template>
    </el-drawer>

    <!-- 在线试改写 -->
    <el-drawer v-model="pv.visible" title="在线试改写（复用线上解析器）" size="640px">
      <div class="sl-preview">
        <el-input
          v-model="pv.query" clearable placeholder="输入一句真实提问，如：帮我看下AUM走势"
          @keyup.enter="runPreview()"
        />
        <el-checkbox v-model="pv.includeDraft">包含草稿</el-checkbox>
        <el-button type="primary" :icon="MagicStick" :loading="pv.loading" @click="runPreview()">
          试改写
        </el-button>
      </div>
      <div class="sl-hint sl-preview-tip">
        与线上改写<b>同源同口径</b>（同一解析器：Redis 优先 + 最长优先 + 三模式）。
        勾选「包含草稿」时绕过缓存直读 MySQL（缓存里只有已发布词条），便于发布前预演。
      </div>
      <el-alert v-if="pv.error" :title="pv.error" type="error" :closable="false" show-icon class="sl-alert" />

      <template v-if="pv.result">
        <div class="sl-preview-block">
          <div class="slp-title">原句</div>
          <pre class="slp-body">{{ pv.result.original }}</pre>
        </div>
        <div class="sl-preview-block">
          <div class="slp-title">
            改写结果
            <span class="sl-hint">
              缓存来源 {{ pv.result.cache_source }} · {{ pv.result.elapsed_ms }}ms
              <template v-if="pv.result.degraded"> · 已降级为原句透传</template>
            </span>
          </div>
          <pre class="slp-body slp-rewritten">{{ pv.result.rewritten }}</pre>
        </div>
        <el-table :data="pv.result.hits" border size="small" class="sl-table">
          <el-table-column label="#" type="index" width="50" />
          <el-table-column prop="term" label="命中黑话" min-width="110" show-overflow-tooltip />
          <el-table-column label="标准名 / 维值" min-width="150" show-overflow-tooltip>
            <template #default="{ row }">
              {{ (row.expand && row.expand.length) ? row.expand.join(' / ') : row.standard || '-' }}
            </template>
          </el-table-column>
          <el-table-column label="模式" width="76">
            <template #default="{ row }">{{ slangModeLabel(row.rewrite_mode) }}</template>
          </el-table-column>
          <el-table-column label="层级" width="86">
            <template #default="{ row }">{{ slangScopeLabel(row.scope) }}</template>
          </el-table-column>
          <el-table-column label="区间" width="96">
            <template #default="{ row }">
              <span class="sl-mono">{{ (row.span || []).join('~') }}</span>
            </template>
          </el-table-column>
          <template #empty>
            <div class="sl-empty">没有词条命中这句话 —— 可新增一条词条后再试。</div>
          </template>
        </el-table>
      </template>
    </el-drawer>

    <!-- 版本历史 -->
    <el-drawer v-model="vs.visible" :title="`版本历史：${vs.term}`" size="620px">
      <el-alert v-if="vs.error" :title="vs.error" type="error" :closable="false" show-icon class="sl-alert" />
      <el-table :data="vs.items" v-loading="vs.loading" border size="small" class="sl-table">
        <el-table-column label="版本" width="70">
          <template #default="{ row }"><span class="sl-mono">v{{ row.version }}</span></template>
        </el-table-column>
        <el-table-column label="变更" width="90">
          <template #default="{ row }">{{ changeTypeLabel(row.change_type) }}</template>
        </el-table-column>
        <el-table-column label="内容" min-width="220">
          <template #default="{ row }">
            <span class="sl-mono sl-version-body">{{ versionSummary(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作人" width="100" show-overflow-tooltip>
          <template #default="{ row }">{{ row.operator || '-' }}</template>
        </el-table-column>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button v-if="reviewable" link type="warning" @click="onRollback(row)">回滚</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>

    <!-- 批量导入 -->
    <el-drawer v-model="importDrawer" title="批量导入词条" size="620px">
      <el-alert
        title="支持导出同构 JSON。导入前建议先「预检」：只做校验与冲突检测、不落库，返回非法/冲突明细。standard 必须真实存在于语义层。"
        type="info" :closable="false" show-icon class="sl-alert"
      />
      <div class="sl-import-opts">
        <span class="sl-hint">导入到层级</span>
        <el-select v-model="importScope" class="sl-import-scope">
          <el-option v-for="s in scopeOptions" :key="s" :label="slangScopeLabel(s)" :value="s" />
        </el-select>
        <span class="sl-hint">状态</span>
        <el-select v-model="importStatus" class="sl-import-status">
          <el-option label="草稿" value="draft" />
          <el-option label="已发布" value="published" />
        </el-select>
        <el-checkbox v-model="importOverwrite">覆盖同层同词</el-checkbox>
      </div>
      <div class="sl-import-pick">
        <el-upload
          :auto-upload="false" :show-file-list="false" accept=".json,application/json"
          :on-change="onPickFile"
        >
          <el-button :icon="Upload">选择 JSON 文件</el-button>
        </el-upload>
        <span class="sl-hint">或直接粘贴到下方文本框</span>
      </div>
      <el-input
        v-model="importText" type="textarea" :rows="11"
        placeholder='{"items":[{"term":"AUM","standard":"零售存款余额","entry_type":"metric"}]}'
      />
      <el-alert
        v-if="importResult"
        :title="importSummary"
        :type="importResult.errors && importResult.errors.length ? 'warning' : 'success'"
        :closable="false" show-icon class="sl-alert"
      />
      <ul v-if="importResult && importResult.errors && importResult.errors.length" class="sl-import-errors">
        <li v-for="(e, i) in importResult.errors.slice(0, 20)" :key="i">
          第 {{ e.index }} 行{{ e.term ? `「${e.term}」` : '' }}：{{ e.error }}
        </li>
      </ul>

      <template #footer>
        <el-button @click="importDrawer = false">取消</el-button>
        <el-button :icon="View" :loading="sp.saving" @click="onImport(true)">预检</el-button>
        <el-button type="primary" :loading="sp.saving" @click="onImport(false)">确认导入</el-button>
      </template>
    </el-drawer>

    <!-- FR13 导出预览（含水印 + 截断告知） -->
    <ExportPreviewDialog v-model="exportVisible" :payload="exportPayload" kind="slang" />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowDown, Download, MagicStick, Plus, Refresh, RefreshLeft, Search, Upload, View
} from '@element-plus/icons-vue'
import {
  store, loadSlangPage, loadSlang, setSlangFilter, setSlangPage, resetSlangFilters,
  createSlang, updateSlang, removeSlang, publishSlang, disableSlang,
  openSlangPreview, runSlangPreview, openSlangVersions, rollbackSlangVersion,
  importSlang, exportSlang,
  slangScopeLabel, slangTypeLabel, slangStatusLabel, slangModeLabel, slangSourceLabel,
  canWriteSlang, canReviewSlang, isAdmin
} from '../store'
import ExportPreviewDialog from './ExportPreviewDialog.vue'

const sp = store.slangPage
const pv = sp.preview
const vs = sp.versions

const writable = computed(() => canWriteSlang())
const reviewable = computed(() => canReviewSlang())
/* 非管理员只能在「用户级」里建；管理员三档都可选 */
const scopeOptions = computed(() => (isAdmin() ? ['tenant', 'industry', 'user'] : ['user']))

const editDrawer = ref(false)
const form = reactive({
  id: 0, scope: 'user', term: '', standard: '', entry_type: 'metric',
  dim_value: '', rewrite_mode: 'expand', priority: 100, status: 'draft', remark: '',
  version: null
})

const importDrawer = ref(false)
const importText = ref('')
const importScope = ref('user')
const importStatus = ref('draft')
const importOverwrite = ref(false)
const importResult = ref(null)
const exporting = ref(false)

const statCards = computed(() => {
  const s = sp.stats || {}
  const sc = s.by_scope || {}
  return [
    { key: 'industry', label: '行业级', value: sc.industry ?? '-', hint: '平台内置（共享）' },
    { key: 'tenant', label: '公司级', value: sc.tenant ?? '-', hint: '本租户内共享' },
    { key: 'user', label: '用户级', value: sc.user ?? '-', hint: '仅本人可见' },
    { key: 'published', label: '已发布', value: s.published ?? '-', hint: '参与线上改写' },
    { key: 'draft', label: '草稿', value: s.draft ?? '-', hint: '发布前不生效' },
    { key: 'disabled', label: '已停用', value: s.disabled ?? '-', hint: '保留记录不生效' }
  ]
})

/* 标准名候选：指标 / 维度 / 枚举值 分别对应不同词表 */
const standardOptions = computed(() => {
  if (form.entry_type === 'metric') return sp.facets.metric_names || []
  if (form.entry_type === 'dimension' || form.entry_type === 'dim_value') {
    return sp.facets.dimension_names || []
  }
  return []
})

const importSummary = computed(() => {
  const r = importResult.value
  if (!r) return ''
  const bad = (r.errors && r.errors.length) || 0
  const conflicts = (r.conflicts && r.conflicts.length) || 0
  return `结果：新增 ${r.created} · 覆盖 ${r.updated} · 跳过 ${r.skipped}` +
    `${conflicts ? ` · 冲突 ${conflicts}` : ''}${bad ? ` · 非法 ${bad}` : ''}`
})

function scopeTagType(s) {
  return s === 'user' ? 'success' : (s === 'tenant' ? 'warning' : 'info')
}
function statusTagType(s) {
  return s === 'published' ? 'success' : (s === 'draft' ? 'warning' : 'info')
}
function changeTypeLabel(t) {
  return {
    create: '新增', update: '修改', publish: '发布', disable: '停用',
    delete: '删除', rollback: '回滚'
  }[t] || t || '-'
}
function versionSummary(row) {
  const j = row.after_json || row.before_json || {}
  if (!j || typeof j !== 'object') return '-'
  const bits = [`黑话=${j.term ?? '-'}`]
  if (j.standard) bits.push(`标准=${j.standard}`)
  if (j.dim_value) bits.push(`维值=${j.dim_value}`)
  if (j.status) bits.push(`状态=${slangStatusLabel(j.status)}`)
  if (j.priority != null) bits.push(`优先级=${j.priority}`)
  return bits.join(' · ')
}
function fmtTime(t) {
  if (!t) return '-'
  try { return new Date(t).toLocaleString('zh-CN', { hour12: false }) } catch (e) { return t }
}
/* 行级写权限：与非 admin 的 scope 守卫保持一致（只能动本人用户级） */
function rowWritable(row) {
  if (!writable.value) return false
  if (isAdmin()) return true
  const me = store.auth.user || {}
  return row.scope === 'user' && row.user_id === me.id
}

/* ---- 列表 / 过滤 ---- */
function onFilterChange() { setSlangFilter({ }) }
/* 关键词输入：300ms 防抖后自动过滤（否则用户以为"搜了没反应"） */
let kwTimer = 0
function onKeywordInput() {
  clearTimeout(kwTimer)
  kwTimer = setTimeout(() => { setSlangFilter({}) }, 300)
}
function onSizeChange(size) {
  sp.pageSize = size
  sp.page = 1
  loadSlang()
}
async function onRefresh() {
  await loadSlangPage()
  ElMessage.success('已刷新')
}

/* ---- 新增 / 编辑 ---- */
function resetForm() {
  Object.assign(form, {
    // 新建默认落在「公司级」：行业级是平台内置层（种子 / 导入维护），避免误改共享词典
    id: 0, scope: isAdmin() ? 'tenant' : 'user', term: '', standard: '',
    entry_type: 'metric', dim_value: '', rewrite_mode: 'expand', priority: 100,
    status: 'draft', remark: '', version: null
  })
}
function openCreate() {
  resetForm()
  editDrawer.value = true
}
function openEdit(row) {
  Object.assign(form, {
    id: row.id, scope: row.scope, term: row.term, standard: row.standard || '',
    entry_type: row.entry_type || 'metric', dim_value: row.dim_value || '',
    rewrite_mode: row.rewrite_mode || 'expand', priority: Number(row.priority) || 100,
    status: row.status || 'draft', remark: row.remark || '', version: row.version
  })
  editDrawer.value = true
}

async function onSubmit() {
  if (!form.term.trim()) {
    ElMessage.warning('黑话 / 口语不能为空')
    return
  }
  if (form.entry_type !== 'stop' && !String(form.standard || '').trim()) {
    ElMessage.warning('请填写语义层标准名')
    return
  }
  const payload = {
    term: form.term.trim(),
    standard: form.entry_type === 'stop' ? '' : String(form.standard || '').trim(),
    entry_type: form.entry_type,
    dim_value: form.entry_type === 'dim_value' ? form.dim_value : '',
    rewrite_mode: form.rewrite_mode,
    priority: Number(form.priority) || 100,
    status: form.status,
    remark: form.remark
  }
  try {
    if (form.id) {
      /* 乐观锁：带上 version，冲突时后端返回 409 */
      await updateSlang(form.id, { ...payload, version: form.version })
      ElMessage.success('已保存')
    } else {
      await createSlang({ ...payload, scope: form.scope })
      ElMessage.success('已新增')
    }
    editDrawer.value = false
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  }
}

async function onToggle(row) {
  try {
    if (row.status === 'published') {
      await disableSlang(row.id)
      ElMessage.success('已停用（不再参与改写）')
    } else {
      await publishSlang(row.id)
      ElMessage.success('已发布（立即生效）')
    }
  } catch (e) { ElMessage.error(e.message || '操作失败') }
}

async function onRemove(row) {
  const ok = await ElMessageBox.confirm(
    `删除词条「${row.term}」？此操作不可恢复（会写入版本审计）。`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    await removeSlang(row.id)
    ElMessage.success('已删除')
  } catch (e) { ElMessage.error(e.message || '删除失败') }
}

/* ---- 在线试改写 ---- */
function openPreview(query = '') { openSlangPreview(query) }
function onTryWord(row) { openSlangPreview(row.term) }
async function runPreview() {
  const q = (pv.query || '').trim()
  if (!q) { ElMessage.warning('请输入一句话'); return }
  const r = await runSlangPreview()
  if (r) ElMessage.success(`命中 ${r.hits.length} 条词条`)
}

/* ---- 版本 ---- */
function openVersions(row) { openSlangVersions(row) }
async function onRollback(row) {
  const ok = await ElMessageBox.confirm(
    `回滚到版本 v${row.version}？会生成一条新版本（不删除历史）。`,
    '回滚确认', { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    await rollbackSlangVersion(vs.entryId, row.version, `回滚至 v${row.version}`)
    ElMessage.success(`已回滚到 v${row.version}`)
  } catch (e) { ElMessage.error(e.message || '回滚失败') }
}

/* ---- 导出 / 导入 ---- */
/* 导出改为「先预览（含水印）再下载」：payload 交给 ExportPreviewDialog 落盘 */
const exportVisible = ref(false)
const exportPayload = ref(null)

async function onExport() {
  exporting.value = true
  try {
    exportPayload.value = await exportSlang()
    exportVisible.value = true
  } catch (e) {
    ElMessage.error(e.message || '导出失败')
  } finally {
    exporting.value = false
  }
}

function openImport() {
  importResult.value = null
  importScope.value = isAdmin() ? 'industry' : 'user'
  importDrawer.value = true
}

function onPickFile(uploadFile) {
  const raw = uploadFile && uploadFile.raw
  if (!raw) return
  const reader = new FileReader()
  reader.onload = () => { importText.value = String(reader.result || '') }
  reader.onerror = () => ElMessage.error('读取文件失败')
  reader.readAsText(raw)
}

async function onImport(dryRun) {
  let payload
  try {
    payload = JSON.parse(importText.value || '{}')
  } catch (e) {
    ElMessage.error('JSON 解析失败，请检查格式')
    return
  }
  const items = Array.isArray(payload) ? payload : (payload.items || [])
  if (!items.length) {
    ElMessage.warning('没有可导入的词条（需要 items 数组）')
    return
  }
  try {
    importResult.value = await importSlang(items, {
      scope: importScope.value, status: importStatus.value,
      overwrite: importOverwrite.value, dryRun
    })
    if (dryRun) ElMessage.success('预检完成（未落库）')
    else ElMessage.success('导入完成')
  } catch (e) {
    ElMessage.error(e.message || '导入失败')
  }
}

onMounted(() => {
  if (!sp.items.length && !sp.stats) loadSlangPage()
  else loadSlang()
})
</script>

<style scoped>
/* 视图页根容器必须是滚动容器（body overflow:hidden + .main 不可滚） */
.sl-page {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  position: relative;
  padding: 18px 22px 28px;
  box-sizing: border-box;
}
.sl-page > * { flex: none; }
.sl-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.sl-title { margin: 0; font-size: 18px; font-weight: 600; color: #1f2329; }
.sl-sub { font-size: 12px; color: #8a9099; margin-top: 4px; max-width: 780px; line-height: 1.7; }
.sl-head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sl-alert { margin: 6px 0; }

.sl-stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0 4px; }
.sl-stat {
  flex: 1 1 130px; min-width: 120px; background: var(--card, #fff);
  border: 1px solid var(--border, #e8eaee); border-radius: 10px; padding: 12px 14px;
}
.sls-label { font-size: 12px; color: #8a9099; }
.sls-value { font-size: 22px; font-weight: 600; color: #1f2329; margin: 4px 0 2px; }
.sls-hint { font-size: 11px; color: #a8adb5; }

.sl-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 14px 0 12px; }
.sl-search { width: 250px; }
.sl-select { width: 148px; }
.sl-select-narrow { width: 118px; }
.sl-hint { font-size: 12px; color: #8a9099; }
.sl-table { width: 100%; }
/* 操作列：折叠「更多」下拉，避免链接按钮换行撑高行 */
.sl-more { margin-left: 10px; vertical-align: middle; }
.sl-more-i { margin-left: 2px; vertical-align: middle; }
.sl-term { font-weight: 500; color: #1f2329; }
.sl-muted { font-size: 12px; color: #a8adb5; }
.sl-mono { font-size: 11px; color: #8a9099; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.sl-empty { color: #8a9099; font-size: 13px; padding: 16px 0; line-height: 1.6; }
.sl-pager { display: flex; justify-content: flex-end; margin-top: 14px; }

.sl-form { padding: 0 8px; }
.sl-form-wide { width: 100%; }

.sl-preview { display: flex; align-items: center; gap: 10px; padding: 4px 2px 8px; }
.sl-preview-tip { margin-bottom: 10px; }
.sl-preview-block { margin: 10px 0; }
.slp-title { font-size: 13px; color: #1f2329; font-weight: 500; margin-bottom: 6px; }
.slp-body {
  margin: 0; padding: 10px 12px; background: #f7f8fa; border-radius: 8px;
  font-size: 12px; line-height: 1.7; color: #4e5969; white-space: pre-wrap; word-break: break-all;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.slp-rewritten { color: #1f2329; background: #eef6ff; }

.sl-version-body { display: inline-block; white-space: normal; line-height: 1.6; }
.sl-import-opts { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0 4px; }
.sl-import-scope { width: 120px; }
.sl-import-status { width: 108px; }
.sl-import-pick { display: flex; align-items: center; gap: 10px; margin: 10px 0; }
.sl-import-errors { margin: 8px 0 0; padding-left: 18px; font-size: 12px; color: #b54708; line-height: 1.7; }
</style>
