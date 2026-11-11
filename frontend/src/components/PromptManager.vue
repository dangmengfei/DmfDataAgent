<template>
  <div class="pm-view">
    <div class="pm-head">
      <div class="pm-title">系统提示词管理</div>
      <div class="pm-sub">内置基线 v{{ store.prompts.builtinVersion }} · 编辑保存生成新版本 · 回溯复制历史内容（审计链保留）</div>
    </div>

    <div class="pm-body">
      <!-- 左：模板列表 -->
      <div class="pm-side">
        <div v-loading="store.prompts.loading" class="pm-list">
          <div v-for="it in store.prompts.items" :key="it.name"
               class="pm-item" :class="{ on: current.name === it.name }" @click="select(it)">
            <div class="pm-name-row">
              <span class="pm-name">{{ it.name }}</span>
              <span class="pm-badge" :class="it.source === 'builtin' ? 'b-in' : 'b-mu'">{{ srcLabel(it.source) }}</span>
            </div>
            <div class="pm-desc">{{ it.description }}</div>
            <div class="pm-ver">v{{ it.version }} · 历史 {{ it.history_count }} 版</div>
          </div>
        </div>
      </div>

      <!-- 右：当前模板编辑 + 版本历史 -->
      <div class="pm-main">
        <template v-if="current.name">
          <div class="pm-panel">
            <div class="pm-panel-head">
              <span class="pm-current-title">{{ current.name }} · 当前生效 v{{ current.version }}（{{ srcLabel(current.source) }}）</span>
              <div class="pm-actions">
                <el-button v-if="!editing && canWrite" size="small" @click="startEdit">编辑</el-button>
                <template v-else-if="editing">
                  <el-button size="small" @click="cancelEdit">取消</el-button>
                  <el-button size="small" type="primary" :loading="saving" @click="doSave">保存为新版本</el-button>
                </template>
              </div>
            </div>
            <el-input v-model="content" type="textarea" :rows="12" resize="vertical"
                      class="pm-editor" :readonly="!editing" />
            <div v-if="editing" class="pm-hint">保存将生成新版本并立即生效（历史版本保留，可随时回溯）。</div>
          </div>

          <div class="pm-panel">
            <div class="pm-panel-head"><span class="pm-current-title">版本历史（{{ history.length }}）</span></div>
            <div v-if="!history.length" class="pm-empty">暂无历史版本（当前为内置基线）</div>
            <div v-for="h in history" :key="h.version" class="pm-hist"
                 :class="{ cur: h.version === current.version }">
              <span class="pm-ver-tag" @click="viewHistory(h.version)">v{{ h.version }}</span>
              <span class="pm-hist-src">{{ h.source === 'builtin' ? '内置基线' : h.source_version ? `回溯自 v${h.source_version}` : '管理端保存' }}</span>
              <span class="pm-hist-time">{{ fmtTime(h.created_at) }}</span>
              <el-button v-if="h.version !== current.version && canWrite" link type="primary" size="small"
                         @click="doRollback(h.version)">回溯到此版本</el-button>
              <span v-else class="pm-cur-tag">当前生效</span>
            </div>
          </div>
        </template>
        <div v-else class="pm-empty-big">选择左侧模板查看与管理</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref, computed } from 'vue'
import { loadPromptList, loadPromptHistory, loadPromptDetail, savePromptVersion, rollbackPromptVersion, hasPerm } from '../store'
import { store } from '../store'

/* 提示词写权限（仅 admin）；analyst / viewer 只读查看 */
const canWrite = computed(() => hasPerm('prompt:write'))

const current = reactive({ name: '', version: '', source: '' })
const history = ref([])
const content = ref('')
const editing = ref(false)
const saving = ref(false)

function srcLabel(s){ return { builtin: '内置', mysql: 'MySQL', memory: '内存' }[s] || s }
function fmtTime(t){ return t ? new Date(t * 1000).toLocaleString() : '—' }

async function select(it){
  current.name = it.name; current.version = it.version; current.source = it.source
  editing.value = false
  const d = await loadPromptDetail(it.name, it.version)
  content.value = d ? d.system : ''
  history.value = await loadPromptHistory(it.name)
}
function startEdit(){ editing.value = true }
function cancelEdit(){
  editing.value = false
  loadPromptDetail(current.name, current.version).then(d => { if(d) content.value = d.system })
}
async function doSave(){
  if(!content.value.trim()){ ElMessage({ message: '提示词内容不能为空', type: 'warning' }); return }
  saving.value = true
  const j = await savePromptVersion(current.name, content.value.trim(), '')
  saving.value = false
  ElMessage({ message: j.message, type: j.ok ? 'success' : 'warning' })
  if(j.ok){ editing.value = false; await select(store.prompts.items.find(i => i.name === current.name)) }
}
async function doRollback(version){
  const cf = await ElMessageBox.confirm(
    `将复制 v${version} 的内容生成新版本并设为当前生效（历史保留，可再回溯）。是否继续？`,
    '回溯确认', { confirmButtonText: '回溯', cancelButtonText: '取消', type: 'warning' }
  ).catch(() => null)
  if(!cf) return
  const j = await rollbackPromptVersion(current.name, version)
  ElMessage({ message: j.message, type: j.ok ? 'success' : 'warning' })
  if(j.ok) await select(store.prompts.items.find(i => i.name === current.name))
}
async function viewHistory(version){
  const d = await loadPromptDetail(current.name, version)
  if(d) ElMessageBox.alert(d.system, `v${version} 内容（只读）`, { confirmButtonText: '关闭' })
}
</script>

<style scoped>
.pm-view{max-width:1040px;margin:0 auto;padding:24px 20px 40px}
.pm-head{margin-bottom:16px}
.pm-title{font-size:20px;font-weight:700;color:var(--text)}
.pm-sub{font-size:12px;color:var(--muted);margin-top:4px}
.pm-body{display:flex;gap:16px;align-items:flex-start}
.pm-side{width:300px;flex:none}
.pm-list{display:flex;flex-direction:column;gap:8px}
.pm-item{background:var(--bg);border:1px solid var(--border);border-radius:12px;padding:11px 13px;cursor:pointer;transition:all .15s}
.pm-item:hover{border-color:var(--primary)}
.pm-item.on{border-color:var(--primary);background:var(--primary-soft)}
.pm-name-row{display:flex;align-items:center;gap:8px}
.pm-name{font-size:14px;font-weight:600;color:var(--text)}
.pm-badge{font-size:10px;border-radius:8px;padding:1px 7px;line-height:16px}
.b-in{color:var(--primary);background:#EAF1FF}
.b-mu{color:#389E6D;background:#E8F5EE}
.pm-desc{font-size:11px;color:var(--muted);margin-top:5px;line-height:1.5}
.pm-ver{font-size:11px;color:var(--text-2);margin-top:4px}
.pm-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:16px}
.pm-panel{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
.pm-panel-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;gap:10px;flex-wrap:wrap}
.pm-current-title{font-size:13px;font-weight:600;color:var(--text)}
.pm-actions{display:flex;gap:8px}
.pm-editor :deep(textarea){font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;line-height:1.7;background:var(--bg)}
.pm-hint{font-size:11px;color:var(--muted);margin-top:8px}
.pm-empty,.pm-empty-big{text-align:center;color:var(--muted);padding:32px 0;font-size:13px}
.pm-empty-big{padding:80px 0}
.pm-hist{display:flex;align-items:center;gap:12px;padding:9px 4px;border-bottom:1px dashed var(--border-soft);font-size:12.5px}
.pm-hist.cur{background:var(--primary-soft);border-radius:8px;padding:9px 12px}
.pm-ver-tag{font-family:ui-monospace,Menlo,monospace;font-weight:600;color:var(--primary);cursor:pointer;text-decoration:underline dotted}
.pm-hist-src{color:var(--text-2);font-size:12px}
.pm-hist-time{color:var(--muted);font-size:11px;margin-left:auto}
.pm-cur-tag{font-size:11px;color:#389E6D;background:#E8F5EE;border-radius:8px;padding:2px 8px}
</style>
