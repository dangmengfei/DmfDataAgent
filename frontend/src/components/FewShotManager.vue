<template>
  <div class="fs-page">
    <div class="fs-head">
      <div>
        <h2 class="fs-title">Few-Shot 样本管理</h2>
        <div class="fs-sub">
          意图识别阶段注入 prompt 的「少样本示例」。本页列出的样本**就是真正参与 BM25 召回的候选集合**
          （召回侧只额外排除「已停用」）。来源两类：用户点赞自动沉淀 + 管理面手工录入 / 批量导入。
        </div>
      </div>
      <div class="fs-head-actions">
        <el-button :icon="Aim" @click="openRecall()">试召回</el-button>
        <el-button :icon="Download" :loading="exporting" @click="onExport()">导出</el-button>
        <el-button v-if="writable" :icon="Upload" @click="openImport()">导入</el-button>
        <el-button v-if="writable" type="primary" :icon="Plus" @click="openCreate()">新增样本</el-button>
        <el-button :icon="Refresh" :loading="fs.loading" @click="onRefresh()">刷新</el-button>
      </div>
    </div>

    <el-alert
      v-if="!writable"
      title="当前角色对 Few-Shot 为只读：新增 / 编辑 / 停用 / 删除 / 导入需要 fewshot:write 权限点（分析师及以上）。"
      type="info" :closable="false" show-icon class="fs-alert"
    />
    <el-alert
      v-if="fs.error" :title="fs.error" type="error" :closable="false" show-icon class="fs-alert"
    />

    <!-- 统计卡（GET /fewshots/stats） -->
    <div class="fs-stats">
      <div v-for="c in statCards" :key="c.key" class="fs-stat">
        <div class="fss-label">{{ c.label }}</div>
        <div class="fss-value">{{ c.value }}</div>
        <div class="fss-hint">{{ c.hint }}</div>
      </div>
    </div>

    <div class="fs-toolbar">
      <el-input
        v-model="fs.filters.keyword" class="fs-search" clearable
        placeholder="匹配问题 / 答案 / 标签"
        @keyup.enter="onFilterChange()" @clear="onFilterChange()"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-select v-model="fs.filters.category" class="fs-select" clearable placeholder="类别"
                 @change="onFilterChange()">
        <el-option v-for="c in fs.facets.categories" :key="c" :label="c" :value="c" />
      </el-select>
      <el-select v-model="fs.filters.intent" class="fs-select" clearable placeholder="意图"
                 @change="onFilterChange()">
        <el-option v-for="i in fs.facets.intents" :key="i" :label="i" :value="i" />
      </el-select>
      <el-select v-model="fs.filters.source" class="fs-select-narrow" clearable placeholder="来源"
                 @change="onFilterChange()">
        <el-option label="点赞沉淀" value="like" />
        <el-option label="手工录入" value="manual" />
      </el-select>
      <el-select v-model="fs.filters.status" class="fs-select-narrow" clearable placeholder="状态"
                 @change="onFilterChange()">
        <el-option label="参与召回" value="active" />
        <el-option label="已停用" value="disabled" />
      </el-select>
      <el-button text :icon="RefreshLeft" @click="resetFewShotFilters()">重置</el-button>
      <span class="fs-hint">共 {{ fs.total }} 条 · 停用的样本保留在库中但不再被召回</span>
    </div>

    <el-table :data="fs.items" v-loading="fs.loading" border class="fs-table">
      <el-table-column label="ID" width="70">
        <template #default="{ row }"><span class="fs-mono">{{ row.id }}</span></template>
      </el-table-column>
      <el-table-column label="来源" width="100">
        <template #default="{ row }">
          <el-tag :type="row.source === 'like' ? 'success' : 'primary'" effect="plain" size="small">
            {{ fewShotSourceLabel(row.source) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="类别" width="110" show-overflow-tooltip>
        <template #default="{ row }"><span class="fs-mono">{{ row.category }}</span></template>
      </el-table-column>
      <el-table-column label="期望意图" width="120" show-overflow-tooltip>
        <template #default="{ row }">{{ row.intent || '-' }}</template>
      </el-table-column>
      <el-table-column prop="question" label="示例问题" min-width="230" show-overflow-tooltip />
      <el-table-column label="期望输出" min-width="240">
        <template #default="{ row }">
          <div class="fs-answer">{{ row.answer || '（空）' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="标签" width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.tags || '-' }}</template>
      </el-table-column>
      <el-table-column label="权重" width="72">
        <template #default="{ row }">{{ row.weight }}</template>
      </el-table-column>
      <el-table-column label="点赞数" width="80">
        <template #default="{ row }">{{ row.score }}</template>
      </el-table-column>
      <el-table-column label="状态" width="96">
        <template #default="{ row }">
          <el-tag :type="row.status === 'active' ? 'success' : 'info'" effect="plain" size="small">
            {{ row.status === 'active' ? '参与召回' : '已停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="168">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <template v-if="writable">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link :type="row.status === 'active' ? 'warning' : 'success'"
                       @click="onToggle(row)">
              {{ row.status === 'active' ? '停用' : '启用' }}
            </el-button>
            <el-button link type="danger" @click="onRemove(row)">删除</el-button>
          </template>
          <span v-else class="fs-readonly">只读</span>
        </template>
      </el-table-column>
      <template #empty>
        <div class="fs-empty">
          暂无样本。可以在对话里给回答点赞自动沉淀，或点右上角「新增样本」手工录入一条最可信的示例。
        </div>
      </template>
    </el-table>

    <div class="fs-pager">
      <el-pagination
        layout="total, prev, pager, next, sizes"
        :total="fs.total"
        :current-page="fs.page"
        :page-size="fs.pageSize"
        :page-sizes="[10, 20, 50, 100]"
        @current-change="setFewShotPage"
        @size-change="onSizeChange"
      />
    </div>

    <!-- 新增 / 编辑 -->
    <el-drawer v-model="editDrawer" :title="form.id ? '编辑样本' : '新增样本'" size="600px">
      <el-form label-width="96px" class="fs-form">
        <el-form-item label="示例问题">
          <el-input v-model="form.question" type="textarea" :rows="3"
                    placeholder="用户会怎么问，如：各分行存款余额排名" />
        </el-form-item>
        <el-form-item label="期望输出">
          <el-input v-model="form.answer" type="textarea" :rows="5"
                    placeholder="期望模型在这个问题上给出的判定 / 回答要点" />
        </el-form-item>
        <el-form-item label="期望意图">
          <el-input v-model="form.intent" placeholder="metric_query / diagnosis / knowledge_qa（可留空）" />
        </el-form-item>
        <el-form-item label="类别">
          <el-input v-model="form.category" placeholder="intention（意图识别）" />
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="form.tags" placeholder="逗号分隔，如：存款,排名" />
        </el-form-item>
        <el-form-item label="权重">
          <el-input-number v-model="form.weight" :min="0" :max="10" :step="0.5" />
          <span class="fs-hint">召回重排时按权重放大（点赞样本默认 1.0）</span>
        </el-form-item>
        <el-form-item v-if="form.id" label="状态">
          <el-radio-group v-model="form.status">
            <el-radio value="active">参与召回</el-radio>
            <el-radio value="disabled">已停用</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDrawer = false">取消</el-button>
        <el-button type="primary" :loading="fs.saving" @click="onSubmit()">保存</el-button>
      </template>
    </el-drawer>

    <!-- 试召回 -->
    <el-drawer v-model="recall.visible" title="试召回（验证样本是否会被命中）" size="640px">
      <div class="fs-recall">
        <el-input
          v-model="recall.query" clearable placeholder="输入一句真实提问，如：各分行存款余额排名"
          @keyup.enter="runRecall()"
        />
        <el-input-number v-model="recall.topK" :min="1" :max="20" size="default" />
        <el-button type="primary" :icon="Aim" :loading="recall.loading" @click="runRecall()">
          试召回
        </el-button>
      </div>
      <div class="fs-recall-tip">
        与线上意图识别注入**同源同口径**（复用同一套召回实现）。候选 {{ recall.result?.candidates ?? '-' }} 条。
      </div>
      <el-alert v-if="recall.error" :title="recall.error" type="error" :closable="false" show-icon />

      <template v-if="recall.result">
        <el-table :data="recall.result.hits" border size="small" class="fs-table">
          <el-table-column label="#" type="index" width="52" />
          <el-table-column label="来源" width="96">
            <template #default="{ row }">
              <el-tag :type="row.source === 'like' ? 'success' : 'primary'" effect="plain" size="small">
                {{ fewShotSourceLabel(row.source) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="question" label="命中问题" min-width="180" show-overflow-tooltip />
          <el-table-column label="期望意图" width="110" show-overflow-tooltip>
            <template #default="{ row }">{{ row.intent || '-' }}</template>
          </el-table-column>
          <el-table-column label="BM25" width="88">
            <template #default="{ row }"><span class="fs-mono">{{ fmtScore(row.bm25) }}</span></template>
          </el-table-column>
          <el-table-column label="重排分" width="92">
            <template #default="{ row }"><span class="fs-mono">{{ fmtScore(row.rerank) }}</span></template>
          </el-table-column>
          <template #empty>
            <div class="fs-empty">没有样本命中这句提问 —— 可考虑手工补一条同类样本。</div>
          </template>
        </el-table>

        <div class="fs-recall-block">
          <div class="fsr-title">实际注入 prompt 的文本</div>
          <pre class="fsr-body">{{ recall.result.injected_text || '（无命中，不注入）' }}</pre>
        </div>
      </template>
    </el-drawer>

    <!-- 批量导入 -->
    <el-drawer v-model="importDrawer" title="批量导入样本" size="620px">
      <el-alert
        title="支持导出同构 JSON（{items:[{question,answer,intent,category,tags,weight,status}]}），也兼容旧经验库的 query/content 字段。逐条校验：格式不对的只跳过该条并回报行号。"
        type="info" :closable="false" show-icon class="fs-alert"
      />
      <div class="fs-import-pick">
        <el-upload
          :auto-upload="false" :show-file-list="false" accept=".json,application/json"
          :on-change="onPickFile"
        >
          <el-button :icon="Upload">选择 JSON 文件</el-button>
        </el-upload>
        <span class="fs-hint">或直接粘贴到下方文本框</span>
      </div>
      <el-input
        v-model="importText" type="textarea" :rows="12"
        placeholder='{"items":[{"question":"各分行存款余额排名","answer":"metric_query","intent":"metric_query"}]}'
      />
      <div class="fs-import-opts">
        <el-checkbox v-model="importOverwrite">覆盖同问题的已有样本（默认跳过重复）</el-checkbox>
      </div>
      <div class="fs-import-cat">
        <span class="fs-hint">默认类别</span>
        <el-input v-model="importCategory" class="fs-import-cat-input" placeholder="intention" />
      </div>

      <el-alert
        v-if="importResult"
        :title="importSummary"
        :type="importResult.errors && importResult.errors.length ? 'warning' : 'success'"
        :closable="false" show-icon class="fs-alert"
      />
      <ul v-if="importResult && importResult.errors && importResult.errors.length" class="fs-import-errors">
        <li v-for="(e, i) in importResult.errors.slice(0, 20)" :key="i">{{ e }}</li>
      </ul>

      <template #footer>
        <el-button @click="importDrawer = false">取消</el-button>
        <el-button type="primary" :loading="fs.saving" @click="onImport()">开始导入</el-button>
      </template>
    </el-drawer>

    <!-- FR13 导出预览（含水印 + 截断告知） -->
    <ExportPreviewDialog v-model="exportVisible" :payload="exportPayload" kind="fewshot" />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Aim, Download, Plus, Refresh, RefreshLeft, Search, Upload } from '@element-plus/icons-vue'
import {
  store, loadFewShotPage, loadFewShots, setFewShotFilter, setFewShotPage, resetFewShotFilters,
  createFewShot, updateFewShot, removeFewShot, openFewShotRecall, runFewShotRecall,
  importFewShots, exportFewShots, fewShotSourceLabel, canWriteFewShot
} from '../store'
import ExportPreviewDialog from './ExportPreviewDialog.vue'

const fs = store.fewshotPage
const recall = fs.recall
const writable = computed(() => canWriteFewShot())

const editDrawer = ref(false)
const form = reactive({
  id: 0, question: '', answer: '', intent: '', category: 'intention', tags: '',
  weight: 1.0, status: 'active'
})

const importDrawer = ref(false)
const importText = ref('')
const importOverwrite = ref(false)
const importCategory = ref('intention')
const importResult = ref(null)
const exporting = ref(false)

const statCards = computed(() => {
  const s = fs.stats || {}
  return [
    { key: 'total', label: '样本总量', value: s.total ?? '-', hint: '点赞沉淀 + 手工录入' },
    { key: 'active', label: '参与召回', value: s.active ?? '-', hint: 'status=active' },
    { key: 'disabled', label: '已停用', value: s.disabled ?? '-', hint: '保留记录但不注入' },
    { key: 'like', label: '点赞沉淀', value: s.like ?? '-', hint: '对话中点赞自动生成' },
    { key: 'manual', label: '手工录入', value: s.manual ?? '-', hint: '管理面新增 / 导入' }
  ]
})

const importSummary = computed(() => {
  const r = importResult.value
  if (!r) return ''
  const bad = (r.errors && r.errors.length) || 0
  return `导入完成：新增 ${r.created} · 覆盖 ${r.updated} · 跳过 ${r.skipped}${bad ? ` · 异常 ${bad}` : ''}`
})

function fmtTime(t) {
  if (!t) return '-'
  try { return new Date(t).toLocaleString('zh-CN', { hour12: false }) } catch (e) { return t }
}
function fmtScore(v) {
  const n = Number(v)
  return isFinite(n) ? n.toFixed(3) : String(v ?? '-')
}

/* 过滤条件变更：统一回第 1 页再查（避免停在第 3 页却只有 1 页数据） */
function onFilterChange() {
  setFewShotFilter({})
}
function onSizeChange(size) {
  fs.pageSize = size
  fs.page = 1
  loadFewShots()
}
async function onRefresh() {
  await loadFewShotPage()
  ElMessage.success('已刷新')
}

/* ---- 新增 / 编辑 ---- */
function resetForm() {
  Object.assign(form, {
    id: 0, question: '', answer: '', intent: '', category: 'intention', tags: '',
    weight: 1.0, status: 'active'
  })
}
function openCreate() {
  resetForm()
  editDrawer.value = true
}
function openEdit(row) {
  Object.assign(form, {
    id: row.id, question: row.question, answer: row.answer || '',
    intent: row.intent || '', category: row.category || 'intention',
    tags: row.tags || '', weight: Number(row.weight) || 1.0,
    status: row.status || 'active'
  })
  editDrawer.value = true
}

async function onSubmit() {
  if (!form.question.trim()) {
    ElMessage.warning('示例问题不能为空')
    return
  }
  try {
    if (form.id) {
      await updateFewShot(form.id, {
        question: form.question, answer: form.answer, intent: form.intent || null,
        category: form.category, tags: form.tags, weight: Number(form.weight) || 1.0,
        status: form.status
      })
      ElMessage.success('已保存')
    } else {
      await createFewShot({
        question: form.question, answer: form.answer, intent: form.intent || null,
        category: form.category || 'intention', tags: form.tags,
        weight: Number(form.weight) || 1.0
      })
      ElMessage.success('已新增')
    }
    editDrawer.value = false
  } catch (e) {
    /* 同名重复时后端返回 409，这里的 detail 已是人话（含已有样本 id / 来源） */
    ElMessage.error(e.message || '保存失败')
  }
}

async function onToggle(row) {
  const next = row.status === 'active' ? 'disabled' : 'active'
  try {
    await updateFewShot(row.id, { status: next })
    ElMessage.success(next === 'active' ? '已启用（重新参与召回）' : '已停用（不再参与召回）')
  } catch (e) { ElMessage.error(e.message || '操作失败') }
}

async function onRemove(row) {
  const ok = await ElMessageBox.confirm(
    `删除样本「${row.question}」？此操作不可恢复。`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!ok) return
  try {
    await removeFewShot(row.id)
    ElMessage.success('已删除')
  } catch (e) { ElMessage.error(e.message || '删除失败') }
}

/* ---- 试召回 ---- */
function openRecall(query = '') {
  openFewShotRecall(query)
}

async function runRecall() {
  const q = (recall.query || '').trim()
  if (!q) {
    ElMessage.warning('请输入一句提问')
    return
  }
  const r = await runFewShotRecall()
  if (r) ElMessage.success(`命中 ${r.hits.length} 条（候选 ${r.candidates} 条）`)
}

/* ---- 导出 / 导入 ---- */
/* 导出改为「先预览（含水印）再下载」：payload 交给 ExportPreviewDialog 落盘 */
const exportVisible = ref(false)
const exportPayload = ref(null)

async function onExport() {
  exporting.value = true
  try {
    exportPayload.value = await exportFewShots()
    exportVisible.value = true
  } catch (e) {
    ElMessage.error(e.message || '导出失败')
  } finally {
    exporting.value = false
  }
}

function openImport() {
  importResult.value = null
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

async function onImport() {
  let payload
  try {
    payload = JSON.parse(importText.value || '{}')
  } catch (e) {
    ElMessage.error('JSON 解析失败，请检查格式')
    return
  }
  /* 兼容两种写法：完整导出对象 {items:[...]} 或直接给数组 [...] */
  const items = Array.isArray(payload) ? payload : (payload.items || [])
  if (!items.length) {
    ElMessage.warning('没有可导入的样本（需要 items 数组）')
    return
  }
  try {
    importResult.value = await importFewShots(items, importCategory.value || 'intention', importOverwrite.value)
    ElMessage.success('导入完成')
  } catch (e) {
    ElMessage.error(e.message || '导入失败')
  }
}

onMounted(() => {
  if (!fs.items.length && !fs.stats) loadFewShotPage()
  else loadFewShots()
})
</script>

<style scoped>
/* 视图页根容器必须是滚动容器（body overflow:hidden + .main 不可滚） */
.fs-page {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  position: relative;
  padding: 18px 22px 28px;
  box-sizing: border-box;
}
.fs-page > * { flex: none; }
.fs-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.fs-title { margin: 0; font-size: 18px; font-weight: 600; color: #1f2329; }
.fs-sub { font-size: 12px; color: #8a9099; margin-top: 4px; max-width: 760px; line-height: 1.6; }
.fs-head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.fs-alert { margin: 6px 0; }

.fs-stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0 4px; }
.fs-stat {
  flex: 1 1 150px; min-width: 140px; background: var(--card, #fff);
  border: 1px solid var(--border, #e8eaee); border-radius: 10px; padding: 12px 14px;
}
.fss-label { font-size: 12px; color: #8a9099; }
.fss-value { font-size: 22px; font-weight: 600; color: #1f2329; margin: 4px 0 2px; }
.fss-hint { font-size: 11px; color: #a8adb5; }

.fs-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 14px 0 12px; }
.fs-search { width: 250px; }
.fs-select { width: 150px; }
.fs-select-narrow { width: 124px; }
.fs-hint { font-size: 12px; color: #8a9099; }
.fs-table { width: 100%; }
.fs-answer {
  font-size: 12px; color: #4e5969; line-height: 1.55;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.fs-mono { font-size: 11px; color: #8a9099; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.fs-empty { color: #8a9099; font-size: 13px; padding: 16px 0; line-height: 1.6; }
.fs-readonly { font-size: 12px; color: #a8adb5; }
.fs-pager { display: flex; justify-content: flex-end; margin-top: 14px; }

.fs-form { padding: 0 8px; }
.fs-recall { display: flex; align-items: center; gap: 10px; padding: 4px 2px 10px; }
.fs-recall-tip { font-size: 12px; color: #8a9099; margin-bottom: 10px; line-height: 1.6; }
.fs-recall-block { margin-top: 14px; }
.fsr-title { font-size: 13px; color: #1f2329; font-weight: 500; margin-bottom: 6px; }
.fsr-body {
  margin: 0; padding: 12px; background: #f7f8fa; border-radius: 8px;
  font-size: 12px; line-height: 1.7; color: #1f2329; white-space: pre-wrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  max-height: 40vh; overflow: auto;
}
.fs-import-pick { display: flex; align-items: center; gap: 10px; margin: 10px 0; }
.fs-import-opts { margin-top: 10px; }
.fs-import-cat { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.fs-import-cat-input { width: 200px; }
.fs-import-errors { margin: 8px 0 0; padding-left: 18px; font-size: 12px; color: #b54708; line-height: 1.7; }
</style>
