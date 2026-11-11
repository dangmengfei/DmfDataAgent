<template>
  <div class="pl-view">
    <div class="pl-head">
      <div class="pl-title">插件与工具</div>
      <div class="pl-sub">Skill（内置 + 自定义 + 上传 ZIP 整包）与 MCP Server 注册 / 连接测试（PRD 6.3 / FR14）</div>
    </div>

    <div class="pl-tabs">
      <span class="pl-tab" :class="{ on: tab === 'skill' }" @click="tab = 'skill'">Skill（{{ store.plugins.skills.length }}）</span>
      <span class="pl-tab" :class="{ on: tab === 'mcp' }" @click="tab = 'mcp'">MCP Server（{{ store.plugins.mcp.length }}）</span>
    </div>

    <!-- ================= Skill ================= -->
    <div v-if="tab === 'skill'" class="pl-panel">
      <div v-if="canWrite" class="pl-bar">
        <el-button type="primary" @click="openAddSkill">
          <el-icon><Plus /></el-icon>&nbsp;新增 Skill
        </el-button>
      </div>
      <div class="pl-tip">
        Skill 以 <b>ZIP 压缩包整包上传</b>：包根需含 <code>SKILL.md</code>（YAML frontmatter 声明工具 / 输入 / 步骤），
        可附带脚本（.py/.sh，经 <code>run_skill_script</code> 沙箱工具执行）与说明文档。
        执行全程在 Harness 治理下：白名单工具 + 受控脚本（路径围栏 / 环境剥离 / 资源与超时限制），
        内容含代码执行 / 外联 / 越权写法会在上传时被安全沙箱拒绝。
        <br />
        对话中由 <b>模型按 description 自动判断</b>是否触发某个 skill（对齐 Claude Code / Codex 机制）；
        声明 <b>triggers</b> 关键词（如 <code>triggers: [存款专题, 分行存款]</code>）可作为无 LLM 时的确定性兜底。
      </div>

      <div class="pl-list">
        <div v-for="s in store.plugins.skills" :key="'skill' + s.name" class="pl-item">
          <span class="pl-ico"><el-icon><MagicStick /></el-icon></span>
          <div class="pl-body">
            <div class="pl-name">
              {{ s.name }}
              <span class="pl-badge" :class="s.builtin ? 'b-in' : 'b-cu'">{{ s.builtin ? '内置' : '自定义' }}</span>
              <span v-if="s.is_package" class="pl-badge b-pkg">v{{ s.version }}</span>
              <span class="pl-badge b-on">{{ s.status }}</span>
            </div>
            <div class="pl-desc">{{ s.description || '（无描述）' }}</div>
            <div v-if="s.is_package" class="pl-meta">
              <span v-for="t in s.tools" :key="t" class="pl-chip">{{ t }}</span>
              <span class="pl-chip">步骤 {{ s.step_count }}</span>
              <span v-if="s.file_count" class="pl-chip">{{ s.file_count }} 个文件</span>
              <span v-if="s.has_scripts" class="pl-chip b-script">含脚本</span>
              <span class="pl-chip">{{ s.filename }} · {{ fmtSize(s.size) }}</span>
            </div>
            <div v-if="s.triggers && s.triggers.length" class="pl-meta">
              <span class="pl-chip b-trigger">触发：{{ s.triggers.join('、') }}</span>
            </div>
          </div>
          <div class="pl-actions">
            <el-button v-if="s.is_package" link type="primary" @click="openDetail(s.name)">详情</el-button>
            <el-button v-if="s.is_package" link type="primary" :disabled="!hasPerm('plugin')" @click="openRun(s.name)">执行</el-button>
            <el-button v-if="!s.builtin && canWrite" link type="danger" @click="doDel('skill', s.name)">删除</el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- ================= MCP ================= -->
    <div v-else class="pl-panel">
      <div v-if="canWrite" class="pl-bar">
        <el-button type="primary" @click="openAddMcp">
          <el-icon><Plus /></el-icon>&nbsp;新增 MCP Server
        </el-button>
      </div>
      <div v-if="canWrite" class="pl-tip">
        注册后点「连接测试」：建立会话 → initialize → list_tools，发现的工具清单会落库并在下方展示。
      </div>

      <div class="pl-list">
        <div v-if="!store.plugins.mcp.length" class="pl-empty">暂无 MCP Server，添加一个试试（如指标平台、风控引擎）</div>
        <div v-for="m in store.plugins.mcp" :key="'mcp' + m.name" class="pl-item pl-item-col">
          <div class="pl-row">
            <span class="pl-ico"><el-icon><Connection /></el-icon></span>
            <div class="pl-body">
              <div class="pl-name">
                {{ m.name }}
                <span class="pl-badge b-cu">{{ cfgOf(m).transport || 'stdio' }}</span>
                <span v-if="cfgOf(m).last_test" class="pl-badge" :class="cfgOf(m).last_test.ok ? 'b-ok' : 'b-err'">
                  {{ cfgOf(m).last_test.ok ? '连通' : '不通' }}
                </span>
              </div>
              <div class="pl-desc">{{ m.endpoint || '（未填写地址）' }}</div>
              <div v-if="cfgOf(m).last_test" class="pl-desc">
                上次测试：{{ cfgOf(m).last_test.ok ? `成功，发现 ${cfgOf(m).last_test.tool_count} 个工具` : `失败 — ${cfgOf(m).last_test.error}` }}
                <span v-if="cfgOf(m).last_test.elapsed_ms != null">（{{ cfgOf(m).last_test.elapsed_ms }} ms）</span>
              </div>
            </div>
            <div class="pl-actions">
              <el-button link type="primary" :loading="testing === m.name" @click="doTest(m.name)">连接测试</el-button>
              <el-button v-if="canWrite" link type="danger" @click="doDel('mcp', m.name)">删除</el-button>
            </div>
          </div>
          <div v-if="toolsOf(m).length" class="pl-tools">
            <span class="pl-tools-title">工具清单（{{ toolsOf(m).length }}）</span>
            <div v-for="t in toolsOf(m)" :key="t.name" class="pl-tool">
              <span class="pl-tool-name">{{ t.name }}</span>
              <span class="pl-tool-desc">{{ t.description || '（无描述）' }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 新增 Skill 对话框 -->
    <el-dialog v-model="addSkillVisible" title="新增 Skill" width="560px">
      <el-radio-group v-model="skillForm.mode" class="pl-mode">
        <el-radio-button label="manual">手动登记</el-radio-button>
        <el-radio-button label="upload">上传整包（ZIP）</el-radio-button>
      </el-radio-group>

      <template v-if="skillForm.mode === 'manual'">
        <div class="pl-form-row">
          <span class="pl-form-label">名称<span class="pl-req">必填</span></span>
          <el-input v-model="skillForm.name" placeholder="如：存款专题分析" />
        </div>
        <div class="pl-form-row">
          <span class="pl-form-label">描述</span>
          <el-input v-model="skillForm.description" type="textarea" :rows="2" placeholder="如：按分行维度做存款专题归因分析" />
        </div>
        <div class="pl-tip">
          手动登记只记录名称与描述，不含可执行内容（对话中不会被触发）；
          需要能被对话触发，请改用「上传整包」。
        </div>
      </template>

      <template v-else>
        <div class="pl-form-row">
          <span class="pl-form-label">压缩包<span class="pl-req">必填</span></span>
          <div class="pl-form-file">
            <el-button :disabled="uploading" @click="pickFile">
              <el-icon><Upload /></el-icon>&nbsp;选择 ZIP
            </el-button>
            <span class="pl-desc">{{ skillForm.file ? skillForm.file.name : '未选择（仅支持 .zip）' }}</span>
            <input ref="fileInput" type="file" class="pl-file" accept=".zip" @change="onFileChange" />
          </div>
        </div>
        <div class="pl-form-row">
          <span class="pl-form-label">描述</span>
          <el-input v-model="skillForm.description" placeholder="留空则用 SKILL.md 里的 description" />
        </div>
        <div class="pl-tip">
          压缩包需在根目录含 <code>SKILL.md</code>（YAML frontmatter 声明工具 / 输入 / 步骤），
          可附带脚本（.py/.sh）与说明文档；上传即校验：后缀 / 大小 / 安全解压 / 内容沙箱扫描 /
          包解析 / 工具白名单，任一不过即拒绝且不落库。对话中由模型按 description 自动判断是否触发，
          声明 <code>triggers</code> 关键词可作无 LLM 时的确定性兜底。
        </div>
      </template>

      <template #footer>
        <el-button @click="addSkillVisible = false">取消</el-button>
        <el-button type="primary" :loading="store.plugins.loading || uploading" @click="submitSkill">确定</el-button>
      </template>
    </el-dialog>

    <!-- 新增 MCP 对话框 -->
    <el-dialog v-model="addMcpVisible" title="新增 MCP Server" width="560px">
      <div class="pl-form-row">
        <span class="pl-form-label">名称<span class="pl-req">必填</span></span>
        <el-input v-model="mcpForm.name" placeholder="如：指标平台服务" />
      </div>
      <div class="pl-form-row">
        <span class="pl-form-label">传输方式</span>
        <el-select v-model="mcpForm.transport" class="pl-select">
          <el-option label="stdio（本地进程）" value="stdio" />
          <el-option label="http（streamable-http）" value="http" />
        </el-select>
      </div>
      <template v-if="mcpForm.transport === 'stdio'">
        <div class="pl-form-row">
          <span class="pl-form-label">启动命令<span class="pl-req">必填</span></span>
          <el-input v-model="mcpForm.command" placeholder="如：python" />
        </div>
        <div class="pl-form-row">
          <span class="pl-form-label">参数</span>
          <el-input v-model="mcpForm.args" placeholder="逗号分隔，如 /path/server.py,--flag" />
        </div>
      </template>
      <div v-else class="pl-form-row">
        <span class="pl-form-label">地址<span class="pl-req">必填</span></span>
        <el-input v-model="mcpForm.url" placeholder="http(s)://地址，如 http://127.0.0.1:9000/mcp" />
      </div>
      <div class="pl-form-row">
        <span class="pl-form-label">超时(秒)</span>
        <el-input v-model.number="mcpForm.timeout" class="pl-input-sm" placeholder="30" />
      </div>

      <template #footer>
        <el-button @click="addMcpVisible = false">取消</el-button>
        <el-button :loading="probing" @click="probeMcp">测试连接</el-button>
        <el-button type="primary" :loading="store.plugins.loading" @click="submitMcp">确定</el-button>
      </template>
    </el-dialog>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="Skill 详情" size="620px">
      <div v-if="detail" class="pl-detail">
        <div class="pl-detail-name">{{ detail.package.name }} <span class="pl-badge b-pkg">v{{ detail.package.version }}</span></div>
        <div class="pl-desc">{{ detail.package.description || '（无描述）' }}</div>
        <div class="pl-meta">
          <span class="pl-chip">{{ detail.package.filename }} · {{ fmtSize(detail.package.size) }}</span>
          <span class="pl-chip">sha256 {{ (detail.package.sha256 || '').slice(0, 12) }}</span>
        </div>
        <div class="pl-sec-title">触发词（模型按描述判断优先；命中即确定性兜底触发）</div>
        <div v-if="!detail.package.triggers || !detail.package.triggers.length" class="pl-desc">
          （无）—— 对话中由模型按 description 自动判断是否触发；无 LLM 时无法触发，建议声明 triggers
        </div>
        <div v-else class="pl-meta">
          <span v-for="t in detail.package.triggers" :key="t" class="pl-chip b-trigger">{{ t }}</span>
        </div>
        <div class="pl-sec-title">包内文件（{{ detail.package.files.length }}）</div>
        <div v-if="!detail.package.files.length" class="pl-desc">（单文件包，无额外文件）</div>
        <div v-for="f in detail.package.files" :key="f.path" class="pl-file-row">
          <span class="pl-file-kind" :class="f.kind === 'script' ? 'b-script' : ''">{{ f.kind }}</span>
          <span class="pl-file-path">{{ f.path }}</span>
          <span class="pl-chip">{{ fmtSize(f.size) }}</span>
        </div>
        <div class="pl-sec-title">输入</div>
        <div v-if="!detail.package.inputs.length" class="pl-desc">（无）</div>
        <div v-for="i in detail.package.inputs" :key="i.name" class="pl-desc">
          · {{ i.name }}<span v-if="i.required" class="pl-req">必填</span> — {{ i.description || '（无说明）' }}
        </div>
        <div class="pl-sec-title">步骤（{{ detail.package.steps.length }}）</div>
        <div v-for="(st, i) in detail.package.steps" :key="i" class="pl-step">
          <span class="pl-tool-name">{{ i + 1 }}. {{ st.tool }}</span>
          <pre class="pl-pre">{{ JSON.stringify(st.arguments, null, 2) }}</pre>
        </div>
        <div class="pl-sec-title">正文</div>
        <pre class="pl-pre">{{ detail.package.body }}</pre>
      </div>
    </el-drawer>

    <!-- 执行对话框 -->
    <el-dialog v-model="runVisible" :title="`执行 Skill：${runName}`" width="720px">
      <div class="pl-sec-title">输入</div>
      <div v-for="i in runInputs" :key="i.name" class="pl-run-input">
        <span class="pl-run-label">{{ i.name }}<span v-if="i.required" class="pl-req">必填</span></span>
        <el-input v-model="runValues[i.name]" :placeholder="i.description || ''" />
      </div>
      <div class="pl-run-actions">
        <el-button type="primary" :loading="running" @click="doRun">执行</el-button>
      </div>

      <template v-if="runResult">
        <div class="pl-sec-title">步骤轨迹</div>
        <div v-for="st in runResult.steps || []" :key="st.index" class="pl-step">
          <span class="pl-tool-name">
            {{ st.index + 1 }}. {{ st.tool }}
            <span class="pl-badge" :class="st.status === 'ok' ? 'b-ok' : 'b-err'">{{ st.status }}</span>
            <span v-if="st.elapsed_ms != null" class="pl-chip">{{ st.elapsed_ms }} ms</span>
          </span>
          <div class="pl-desc">{{ st.summary || st.error || '' }}</div>
        </div>
        <div class="pl-sec-title">答复</div>
        <pre class="pl-pre">{{ runResult.answer || runResult.message }}</pre>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { Connection, MagicStick, Plus, Upload } from '@element-plus/icons-vue'
import {
  store, addSkillPlugin, addMcpPlugin, removePlugin, hasPerm,
  uploadSkillPlugin, fetchSkillPackage, runSkillPlugin, testMcpPlugin, probeMcpPlugin
} from '../store'

/* 插件写权限（仅 admin）；analyst / viewer 只读查看 */
const canWrite = computed(() => hasPerm('plugin:write'))

const tab = ref('skill')
const skillForm = reactive({ mode: 'manual', name: '', description: '', file: null })
const mcpForm = reactive({ name: '', transport: 'stdio', command: '', args: '', url: '', timeout: 30 })

/* ---------- Skill：新增（手动登记 / 上传 ZIP 整包） ---------- */
const addSkillVisible = ref(false)
const fileInput = ref(null)
const uploading = ref(false)

function openAddSkill() {
  skillForm.mode = 'manual'
  skillForm.name = ''
  skillForm.description = ''
  skillForm.file = null
  addSkillVisible.value = true
}
function pickFile() {
  fileInput.value && fileInput.value.click()
}
function onFileChange(e) {
  const file = e.target.files && e.target.files[0]
  e.target.value = '' // 允许重复选同一个文件
  if (file) skillForm.file = file
}
async function submitSkill() {
  if (skillForm.mode === 'upload') {
    if (!skillForm.file) { ElMessage({ message: '请先选择 ZIP 压缩包', type: 'warning' }); return }
    uploading.value = true
    try {
      const j = await uploadSkillPlugin(skillForm.file, skillForm.description.trim())
      ElMessage({ message: j.message, type: j.ok ? 'success' : 'error', duration: j.ok ? 3000 : 6000 })
      if (j.ok) addSkillVisible.value = false
    } finally {
      uploading.value = false
    }
    return
  }
  if (!skillForm.name.trim()) { ElMessage({ message: 'Skill 名称不能为空', type: 'warning' }); return }
  const j = await addSkillPlugin(skillForm.name.trim(), skillForm.description.trim())
  ElMessage({ message: j.message, type: j.ok ? 'success' : 'warning' })
  if (j.ok) addSkillVisible.value = false
}

/* ---------- Skill：详情 ---------- */
const detailVisible = ref(false)
const detail = ref(null)
async function openDetail(name) {
  detail.value = null
  detailVisible.value = true
  const j = await fetchSkillPackage(name)
  if (j.ok) detail.value = j
  else ElMessage({ message: j.message, type: 'error' })
}

/* ---------- Skill：沙箱执行 ---------- */
const runVisible = ref(false)
const runName = ref('')
const runInputs = ref([])
const runValues = reactive({})
const running = ref(false)
const runResult = ref(null)

async function openRun(name) {
  runName.value = name
  runResult.value = null
  runVisible.value = true
  const j = await fetchSkillPackage(name)
  if (!j.ok || !j.package) { ElMessage({ message: j.message || '无法加载 skill', type: 'error' }); return }
  runInputs.value = j.package.inputs || []
  Object.keys(runValues).forEach(k => delete runValues[k])
  runInputs.value.forEach(i => { runValues[i.name] = i.default == null ? '' : i.default })
}
async function doRun() {
  running.value = true
  try {
    runResult.value = await runSkillPlugin(runName.value, { ...runValues })
    if (!runResult.value.ok) ElMessage({ message: runResult.value.message, type: 'warning' })
  } finally {
    running.value = false
  }
}

/* ---------- MCP ---------- */
const testing = ref('')
const probing = ref(false)
const addMcpVisible = ref(false)

function openAddMcp() {
  mcpForm.name = ''
  mcpForm.transport = 'stdio'
  mcpForm.command = ''
  mcpForm.args = ''
  mcpForm.url = ''
  mcpForm.timeout = 30
  addMcpVisible.value = true
}
function cfgOf(m) {
  try {
    const c = JSON.parse(m.config || '{}')
    return c && typeof c === 'object' ? c : {}
  } catch (e) { return {} }
}
function toolsOf(m) {
  const tools = cfgOf(m).tools
  return Array.isArray(tools) ? tools : []
}
function mcpPayload() {
  const isStdio = mcpForm.transport === 'stdio'
  const payload = {
    name: mcpForm.name.trim(),
    description: '',
    transport: mcpForm.transport,
    timeout_seconds: Number(mcpForm.timeout) || 30
  }
  if (isStdio) {
    payload.command = mcpForm.command.trim()
    payload.args = mcpForm.args.split(/[,，]/).map(s => s.trim()).filter(Boolean)
  } else {
    payload.url = mcpForm.url.trim()
  }
  return payload
}
/* 表单校验：名称必填 + 按传输方式校验必填项；通过返回 true */
function validateMcpForm() {
  if (!mcpForm.name.trim()) { ElMessage({ message: 'MCP 名称不能为空', type: 'warning' }); return false }
  const isStdio = mcpForm.transport === 'stdio'
  if (isStdio && !mcpForm.command.trim()) { ElMessage({ message: 'stdio 传输需填写启动命令', type: 'warning' }); return false }
  if (!isStdio && !mcpForm.url.trim()) { ElMessage({ message: 'http 传输需填写地址', type: 'warning' }); return false }
  return true
}
async function probeMcp() {
  if (!validateMcpForm()) return
  probing.value = true
  try {
    const j = await probeMcpPlugin(mcpPayload())
    const n = Array.isArray(j.tools) ? j.tools.length : 0
    ElMessage({ message: j.ok ? `连接成功，发现 ${n} 个工具` : j.message, type: j.ok ? 'success' : 'error', duration: 5000 })
  } finally {
    probing.value = false
  }
}
async function submitMcp() {
  if (!validateMcpForm()) return
  const j = await addMcpPlugin(mcpPayload())
  ElMessage({ message: j.message, type: j.ok ? 'success' : 'warning' })
  if (j.ok) addMcpVisible.value = false
}
async function doTest(name) {
  testing.value = name
  try {
    const j = await testMcpPlugin(name)
    ElMessage({
      message: j.message + (j.tools && j.tools.length ? `：${j.tools.map(t => t.name).join('、')}` : ''),
      type: j.ok ? 'success' : 'error',
      duration: j.ok ? 4000 : 8000
    })
  } finally {
    testing.value = ''
  }
}

/* ---------- 公共 ---------- */
function fmtSize(bytes) {
  const n = Number(bytes) || 0
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}
async function doDel(type, name) {
  const j = await removePlugin(type, name)
  ElMessage({ message: j.message, type: j.ok ? 'success' : 'warning' })
}
</script>

<style scoped>
.pl-view{max-width:960px;margin:0 auto;padding:24px 20px 40px}
.pl-head{margin-bottom:16px}
.pl-title{font-size:20px;font-weight:700;color:var(--text)}
.pl-sub{font-size:12px;color:var(--muted);margin-top:4px}
.pl-tabs{display:flex;background:var(--bg);border:1px solid var(--border);border-radius:20px;padding:3px;width:fit-content}
.pl-tab{padding:5px 16px;border-radius:16px;font-size:13px;color:var(--text-2);cursor:pointer;transition:all .15s}
.pl-tab.on{background:var(--primary);color:#fff;font-weight:600}
.pl-panel{margin-top:16px}
.pl-bar{display:flex;gap:10px;align-items:center}
.pl-input-sm{width:110px}
.pl-select{width:180px}
.pl-file{display:none}
.pl-mode{margin-bottom:14px}
.pl-form-row{display:flex;align-items:flex-start;gap:12px;margin-bottom:12px}
.pl-form-label{flex:none;width:80px;font-size:13px;color:var(--text-2);line-height:32px;text-align:right}
.pl-form-row .el-input,.pl-form-row .el-select,.pl-form-row .el-textarea{flex:1}
.pl-form-row .pl-input-sm{flex:none;width:110px}
.pl-form-file{flex:1;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pl-form-file .pl-desc{margin-top:0}
.pl-tip{font-size:12px;color:var(--muted);margin:10px 2px;line-height:1.7}
.pl-list{margin-top:10px;display:flex;flex-direction:column;gap:10px}
.pl-empty{text-align:center;color:var(--muted);padding:40px 0;font-size:13px}
.pl-item{display:flex;align-items:flex-start;gap:12px;background:var(--bg);border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.pl-item-col{flex-direction:column}
.pl-row{display:flex;align-items:flex-start;gap:12px;width:100%}
.pl-ico{flex:none;width:34px;height:34px;border-radius:10px;background:var(--primary-soft);color:var(--primary);display:flex;align-items:center;justify-content:center;font-size:16px}
.pl-body{flex:1;min-width:0}
.pl-name{font-size:14px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pl-badge{font-size:10px;border-radius:8px;padding:1px 7px;line-height:16px}
.b-in{color:var(--primary);background:var(--primary-soft)}
.b-cu{color:#389E6D;background:#E8F5EE}
.b-on{color:var(--muted);background:var(--bg)}
.b-pkg{color:#8A5A00;background:#FFF4E0}
.b-ok{color:#389E6D;background:#E8F5EE}
.b-err{color:#C0392B;background:#FDECEA}
.pl-desc{font-size:12px;color:var(--muted);margin-top:4px;word-break:break-all;line-height:1.6}
.pl-meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.pl-chip{font-size:11px;color:var(--text-2);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:1px 7px}
.pl-chip.b-trigger{color:#1F6F43;background:#EAF7EF;border-color:#BFE3CD}
.pl-chip.b-script{color:#7A4FB2;background:#F3EBFC;border-color:#DDC9F2}
.pl-file-row{display:flex;align-items:center;gap:8px;font-size:12px;padding:4px 0;border-bottom:1px dashed var(--border)}
.pl-file-row:last-child{border-bottom:none}
.pl-file-kind{flex:none;min-width:46px;text-align:center;font-size:10px;border-radius:8px;padding:1px 6px;color:var(--muted);background:var(--bg);border:1px solid var(--border)}
.pl-file-kind.b-script{color:#7A4FB2;background:#F3EBFC;border-color:#DDC9F2}
.pl-file-path{flex:1;min-width:0;color:var(--text-2);word-break:break-all}
.pl-actions{flex:none;display:flex;gap:4px;align-items:center}
.pl-tools{margin-top:10px;padding-top:10px;border-top:1px dashed var(--border);width:100%}
.pl-tools-title{font-size:12px;color:var(--muted)}
.pl-tool{display:flex;gap:8px;font-size:12px;margin-top:4px}
.pl-tool-name{color:var(--primary);font-weight:600;flex:none}
.pl-tool-desc{color:var(--muted);word-break:break-all}
.pl-detail-name{font-size:16px;font-weight:700;color:var(--text);display:flex;align-items:center;gap:8px}
.pl-sec-title{font-size:13px;font-weight:600;color:var(--text);margin:16px 0 6px}
.pl-req{font-size:10px;color:#C0392B;margin-left:4px}
.pl-step{margin-top:8px}
.pl-pre{font-size:12px;color:var(--text-2);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin:4px 0 0;white-space:pre-wrap;word-break:break-all;max-height:240px;overflow:auto}
.pl-run-input{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.pl-run-label{font-size:13px;color:var(--text-2);flex:none;width:120px}
.pl-run-actions{margin-top:12px}
</style>
