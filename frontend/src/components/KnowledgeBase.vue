<template>
  <div class="kb-view">
    <div class="kb-head">
      <div class="kb-title">{{ store.kb.current ? store.kb.current.name : '知识库' }}</div>

      <template v-if="!store.kb.current">
        <div class="kb-tabs">
          <span class="kb-tab" :class="{ on: store.kb.scope === 'personal' }" @click="switchKbScope('personal')">个人知识库</span>
          <span class="kb-tab" :class="{ on: store.kb.scope === 'enterprise' }" @click="switchKbScope('enterprise')">企业知识库</span>
        </div>
        <el-input
          v-model="store.kb.keyword"
          class="kb-search"
          placeholder="搜索知识库名称"
          clearable
          @keyup.enter="loadKbBases"
          @clear="loadKbBases"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button v-if="canWrite" type="primary" class="kb-new" @click="showCreate = true">新建知识库</el-button>
      </template>

      <template v-else>
        <el-button text @click="closeKbBase">← 返回列表</el-button>
        <el-input
          v-model="store.kb.docKeyword"
          class="kb-search"
          placeholder="搜索文档标题"
          clearable
          @keyup.enter="reloadDocs"
          @clear="reloadDocs"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button class="kb-new" type="primary" :loading="store.kb.docsLoading" @click="reloadDocs">搜索</el-button>
      </template>
    </div>

    <div v-if="!store.kb.current" class="kb-tip">
      {{
        store.kb.scope === 'personal'
          ? '个人知识库仅自己可见，参与语义解析召回。归属：' + USER_ID
          : '企业知识库全员共享，作为统一口径与制度依据'
      }}
    </div>

    <!-- 知识库列表 -->
    <div v-if="!store.kb.current" v-loading="store.kb.loading" class="kb-list">
      <div v-if="!store.kb.bases.length" class="kb-empty">暂无知识库，点击「新建知识库」开始</div>
      <div v-for="b in store.kb.bases" :key="b.id" class="kb-item kb-clickable" @click="openKbBase(b)">
        <span class="kb-ico"><el-icon><Collection /></el-icon></span>
        <div class="kb-body">
          <div class="kb-name">
            {{ b.name }}
            <span class="kb-badge" :class="b.type === 'personal' ? 'b-p' : 'b-e'">
              {{ b.type === 'personal' ? '个人' : '企业' }}
            </span>
            <span class="kb-badge b-v">{{ b.visibility === 'public' ? '共享' : '私有' }}</span>
          </div>
          <div class="kb-desc">{{ b.description || '（无描述）' }}</div>
          <div class="kb-meta">{{ b.doc_count || 0 }} 个文档 · 更新于 {{ fmtTime(b.updated_at || b.created_at) }}</div>
        </div>
        <el-button v-if="canWrite" class="kb-del" link type="danger" @click.stop="onRemoveBase(b)">删除</el-button>
      </div>
    </div>

    <!-- 文档管理 -->
    <template v-else>
      <!-- FR7 来源分级：权威源优先，其次沉淀源，最后用户上传 -->
      <div v-if="store.kb.authority" class="kb-auth">
        <span class="kb-auth-title">
          <el-icon><Medal /></el-icon> 来源分级
        </span>
        <span v-for="lv in authorityLevels" :key="lv.key" class="kb-auth-item" :class="lv.key">
          <b>{{ lv.label }}</b>
          <em>{{ authorityCount(lv.key) }}</em>
          <span class="kb-auth-w">×{{ weightOf(lv.key) }}</span>
        </span>
        <span class="kb-auth-rerank">
          精排：{{ store.kb.authority.rerank_available ? '已启用' : '未启用（纯 RRF 排序）' }}
        </span>
      </div>

      <div v-if="canWrite" class="kb-add">
        <el-input v-model="docForm.title" class="kb-title-input" placeholder="文档标题，如：存款日均口径说明" />
        <el-input v-model="docForm.text" class="kb-content-input" placeholder="文档内容（将切分并向量化入库）" />
        <el-button type="primary" :loading="adding" @click="doAddDoc">录入文本</el-button>
        <el-upload :show-file-list="false" :before-upload="onUpload" accept=".txt,.md,.markdown,.pdf">
          <el-button :loading="uploading">上传文件</el-button>
        </el-upload>
        <el-button
          v-if="admin"
          class="kb-seed"
          :loading="store.kb.seeding"
          @click="onSeedDictionary"
        >
          灌入指标口径字典
        </el-button>
      </div>
      <div class="kb-tip">
        支持 .txt / .md / .pdf；上传后自动切分并向量化，供问答检索召回。
        <span v-if="admin">
          口径字典由语义层自动生成（幂等覆盖），灌入后作为<b>权威源</b>优先于用户上传资料被检索。
        </span>
      </div>

      <div v-loading="store.kb.docsLoading" class="kb-list">
        <div v-if="!store.kb.docs.length" class="kb-empty">该知识库暂无文档</div>
        <div v-for="d in store.kb.docs" :key="d.id" class="kb-item">
          <span class="kb-ico"><el-icon><Document /></el-icon></span>
          <div class="kb-body">
            <div class="kb-name">
              {{ d.title }}
              <span class="kb-badge b-v">{{ d.source_type }}</span>
              <span
                v-if="d.authority_level && d.authority_level !== 'user'"
                class="kb-badge"
                :class="d.authority_level === 'authoritative' ? 'b-auth' : 'b-sed'"
              >
                {{ d.authority_level === 'authoritative' ? '权威源' : '沉淀源' }}
              </span>
              <span class="kb-badge" :class="d.status === 'indexed' ? 'b-ok' : 'b-v'">{{ statusText(d.status) }}</span>
            </div>
            <div class="kb-meta">{{ d.chunk_count || 0 }} 分块 · {{ fmtTime(d.created_at) }}</div>
          </div>
          <el-button class="kb-del" link type="primary" @click.stop="openChunks(d)">分块</el-button>
          <el-button v-if="canWrite" class="kb-del" link type="danger" @click.stop="onRemoveDoc(d)">删除</el-button>
        </div>
      </div>
    </template>

    <!-- 新建知识库对话框 -->
    <el-dialog v-model="showCreate" title="新建知识库" width="440px">
      <el-form label-width="72px">
        <el-form-item label="名称">
          <el-input v-model="createForm.name" placeholder="如：存款指标口径库" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" type="textarea" :rows="2" placeholder="可选，一句话说明用途" />
        </el-form-item>
        <el-form-item label="类型">
          <el-tag :type="store.kb.scope === 'personal' ? '' : 'success'" effect="plain">
            {{ store.kb.scope === 'personal' ? '个人知识库（私有）' : '企业知识库（共享）' }}
          </el-tag>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreate = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="doCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 文档分块查看抽屉（只读，核验切分/入库质量） -->
    <el-drawer v-model="chunksVisible" :title="chunksTitle" size="560px">
      <div v-loading="chunksLoading" class="kb-chunks">
        <div v-if="!chunksLoading && !chunks.length" class="kb-empty">该文档没有分块</div>
        <div v-for="c in chunks" :key="c.id" class="kb-chunk">
          <div class="kb-chunk-head">
            <span class="kb-chunk-idx">#{{ c.chunk_index + 1 }}</span>
            <span v-if="c.vector_ref" class="kb-badge b-ok">已向量化</span>
            <span v-else class="kb-badge b-v">未向量化</span>
            <span class="kb-chunk-len">{{ c.content.length }} 字</span>
          </div>
          <pre class="kb-chunk-body">{{ c.content }}</pre>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { reactive, ref, computed, onMounted } from 'vue'
import { Collection, Document, Medal, Search } from '@element-plus/icons-vue'
import {
  store, USER_ID, loadKbBases, switchKbScope, createKbBase, removeKbBase,
  openKbBase, closeKbBase, loadKbDocs, addKbDocText, uploadKbDoc, removeKbDoc, fetchDocChunks,
  hasPerm, isAdmin, loadAuthorityStats, seedMetricDictionary
} from '../store'

/* 知识库写权限（admin / analyst）；viewer 仅只读，写按钮隐藏（后端亦 403 兜底） */
const canWrite = computed(() => hasPerm('knowledge:write'))
/* 权威源灌入仅管理员（后端 require_role(ADMIN) 兜底） */
const admin = computed(() => isAdmin())

/* FR7 来源分级展示项：与后端 authority_weights 键名一致 */
const authorityLevels = [
  { key: 'authoritative', label: '权威源' },
  { key: 'sediment', label: '沉淀源' },
  { key: 'user', label: '用户上传' }
]
function authorityCount(key) {
  return (store.kb.authority && store.kb.authority.counts && store.kb.authority.counts[key]) || 0
}
function weightOf(key) {
  const w = store.kb.authority && store.kb.authority.weights
  return (w && w[key] != null) ? w[key] : 1.0
}

onMounted(() => {
  loadAuthorityStats()
})

async function onSeedDictionary() {
  const cf = await ElMessageBox.confirm(
    '将按当前指标语义层重新生成「指标口径字典」并灌入知识库（权威源）。\n' +
      '同名旧文档会被覆盖更新，不会产生重复。是否继续？',
    '灌入口径字典',
    { type: 'info', confirmButtonText: '灌入', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!cf) return
  try {
    const r = await seedMetricDictionary()
    ElMessage({
      message: `口径字典已灌入（${r.chunks} 个分块${r.replaced ? '，已替换旧版本' : ''}）`,
      type: 'success'
    })
  } catch (e) {
    ElMessage({ message: '灌入失败：' + e.message, type: 'error' })
  }
}

const showCreate = ref(false)
const creating = ref(false)
const adding = ref(false)
const uploading = ref(false)
const createForm = reactive({ name: '', description: '' })
const docForm = reactive({ title: '', text: '' })

/* 文档分块查看（只读） */
const chunksVisible = ref(false)
const chunksLoading = ref(false)
const chunksTitle = ref('')
const chunks = ref([])
async function openChunks(d) {
  chunksTitle.value = `${d.title}（${d.chunk_count || 0} 个分块）`
  chunks.value = []
  chunksVisible.value = true
  chunksLoading.value = true
  try {
    chunks.value = await fetchDocChunks(d.id)
  } finally {
    chunksLoading.value = false
  }
}

function fmtTime(t) {
  if (!t) return '—'
  const d = new Date(t)
  return isNaN(d.getTime()) ? String(t) : d.toLocaleString('zh-CN')
}
function statusText(s) {
  return { pending: '待索引', indexed: '已索引', failed: '索引失败' }[s] || s
}
function reloadDocs() {
  if (store.kb.current) loadKbDocs(store.kb.current.id)
}

async function doCreate() {
  if (!createForm.name.trim()) {
    ElMessage({ message: '知识库名称不能为空', type: 'warning' })
    return
  }
  creating.value = true
  try {
    await createKbBase(createForm.name.trim(), createForm.description.trim())
    ElMessage({ message: '知识库已创建', type: 'success' })
    showCreate.value = false
    createForm.name = ''
    createForm.description = ''
  } catch (e) {
    ElMessage({ message: '创建失败：' + e.message, type: 'error' })
  } finally {
    creating.value = false
  }
}

async function onRemoveBase(b) {
  const cf = await ElMessageBox.confirm(
    `删除知识库「${b.name}」及其下全部文档与向量索引？此操作不可恢复。`,
    '删除确认',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
  ).catch(() => null)
  if (!cf) return
  try {
    const r = await removeKbBase(b.id)
    ElMessage({ message: `已删除（含 ${r.documents_removed || 0} 个文档）`, type: 'success' })
  } catch (e) {
    ElMessage({ message: '删除失败：' + e.message, type: 'error' })
  }
}

async function doAddDoc() {
  if (!docForm.title.trim() || !docForm.text.trim()) {
    ElMessage({ message: '标题与内容均不能为空', type: 'warning' })
    return
  }
  adding.value = true
  try {
    await addKbDocText(docForm.title.trim(), docForm.text.trim())
    ElMessage({ message: '文档已录入并入库', type: 'success' })
    docForm.title = ''
    docForm.text = ''
  } catch (e) {
    ElMessage({ message: '录入失败：' + e.message, type: 'error' })
  } finally {
    adding.value = false
  }
}

async function onUpload(file) {
  uploading.value = true
  try {
    await uploadKbDoc(file)
    ElMessage({ message: '文件已上传并入库', type: 'success' })
  } catch (e) {
    ElMessage({ message: '上传失败：' + e.message, type: 'error' })
  } finally {
    uploading.value = false
  }
  return false
}

async function onRemoveDoc(d) {
  const cf = await ElMessageBox.confirm(`删除文档「${d.title}」及其分块与向量？`, '删除确认', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  }).catch(() => null)
  if (!cf) return
  try {
    await removeKbDoc(d.id)
    ElMessage({ message: '文档已删除', type: 'success' })
  } catch (e) {
    ElMessage({ message: '删除失败：' + e.message, type: 'error' })
  }
}
</script>

<style scoped>
.kb-view{max-width:960px;margin:0 auto;padding:24px 20px 40px}
.kb-head{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.kb-title{font-size:20px;font-weight:700;color:var(--text)}
.kb-tabs{display:flex;background:var(--bg);border:1px solid var(--border);border-radius:20px;padding:3px}
.kb-tab{padding:5px 16px;border-radius:16px;font-size:13px;color:var(--text-2);cursor:pointer;transition:all .15s}
.kb-tab.on{background:var(--primary);color:#fff;font-weight:600}
.kb-search{width:220px}
.kb-new{margin-left:auto}
.kb-tip{font-size:12px;color:var(--muted);margin:12px 2px}
.kb-add{display:flex;gap:10px;margin:14px 0 0;flex-wrap:wrap}
/* FR7 来源分级条：权威源 > 沉淀源 > 用户上传 */
.kb-auth{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:14px 0 0;
  padding:8px 12px;border:1px solid var(--border);border-radius:10px;background:var(--bg)}
.kb-auth-title{display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:600;color:var(--text-2)}
.kb-auth-item{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--text-2)}
.kb-auth-item b{font-weight:600}
.kb-auth-item em{font-style:normal;font-weight:700;color:var(--text)}
.kb-auth-w{font-size:11px;color:var(--muted)}
.kb-auth-item.authoritative b{color:#005BFF}
.kb-auth-item.sediment b{color:#FF8A3D}
.kb-auth-rerank{margin-left:auto;font-size:11px;color:var(--muted)}
.kb-seed{margin-left:auto}
.kb-title-input{width:220px}
.kb-content-input{flex:1;min-width:240px}
.kb-list{margin-top:14px;display:flex;flex-direction:column;gap:10px}
.kb-empty{text-align:center;color:var(--muted);padding:48px 0;font-size:13px}
.kb-item{display:flex;align-items:flex-start;gap:12px;background:var(--bg);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.kb-clickable{cursor:pointer;transition:all .15s}
.kb-clickable:hover{border-color:var(--primary);background:var(--primary-soft)}
.kb-ico{flex:none;width:34px;height:34px;border-radius:10px;background:var(--primary-soft);color:var(--primary);display:flex;align-items:center;justify-content:center;font-size:16px}
.kb-body{flex:1;min-width:0}
.kb-name{font-size:14px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.kb-badge{font-size:10px;border-radius:8px;padding:1px 7px;line-height:16px;font-weight:400}
.b-p{color:var(--primary);background:var(--primary-soft)}
.b-e{color:#389E6D;background:#E8F5EE}
.b-v{color:var(--muted);background:var(--card);border:1px solid var(--border)}
.b-ok{color:#00a325;background:#E8F5EE}
/* 来源分级徽标：权威源（口径字典）/ 沉淀源（点赞审核入库） */
.b-auth{color:#fff;background:#005BFF}
.b-sed{color:#A8550A;background:#FFF1E0}
.kb-desc{font-size:12px;color:var(--text-2);margin-top:5px;line-height:1.5}
.kb-meta{font-size:11px;color:var(--muted);margin-top:4px}
.kb-del{flex:none}
/* 文档分块查看抽屉 */
.kb-chunks{display:flex;flex-direction:column;gap:10px}
.kb-chunk{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.kb-chunk-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.kb-chunk-idx{font-size:12px;font-weight:700;color:var(--primary)}
.kb-chunk-len{font-size:11px;color:var(--muted);margin-left:auto}
.kb-chunk-body{font-size:12px;color:var(--text-2);line-height:1.7;white-space:pre-wrap;word-break:break-all;margin:0;max-height:200px;overflow:auto}
</style>
