<template>
  <div class="md-view">
    <div class="md-head">
      <div class="md-title">模型管理</div>
      <div class="md-sub">
        对话 LLM 配置入库（OpenAI 兼容：qwen / zhipu / deepseek / vllm / ollama）。
        标记「默认」的启用模型作为<b>运行时生效模型</b>，覆盖环境变量 DMF_LLM-*；未配置默认模型时回落环境变量。
      </div>
      <div class="md-tip">
        <span v-if="store.models.effective" class="md-badge b-ok">生效模型：{{ store.models.effective }}</span>
        <span v-else class="md-badge b-on">生效模型：环境变量 DMF_LLM-*（未设库内默认）</span>
        <span v-if="store.models.default && store.models.default !== store.models.effective" class="md-badge b-warn">
          默认标记「{{ store.models.default }}」未启用 / 配置不完整，暂不生效
        </span>
      </div>
    </div>

    <div class="md-bar">
      <el-button v-if="canWrite" type="primary" @click="openAdd">
        <el-icon><Plus /></el-icon>&nbsp;新增模型
      </el-button>
      <span class="md-tip" v-if="canWrite">
        新增 / 编辑弹窗内可先「测试连接」再保存；保存后可对已存模型随时重测。
      </span>
    </div>

    <el-table :data="store.models.items" v-loading="store.models.loading" border class="md-table">
      <el-table-column label="名称" min-width="180">
        <template #default="{ row }">
          <div class="md-name">
            {{ row.name }}
            <span v-if="row.is_default" class="md-badge b-def">默认</span>
            <span v-if="store.models.effective === row.name" class="md-badge b-ok">生效中</span>
            <span class="md-badge" :class="row.status === 'enabled' ? 'b-on' : 'b-err'">
              {{ row.status === 'enabled' ? '启用' : '停用' }}
            </span>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="model" label="模型" min-width="140" show-overflow-tooltip />
      <el-table-column prop="base_url" label="端点 base_url" min-width="220" show-overflow-tooltip />
      <el-table-column label="参数" min-width="150">
        <template #default="{ row }">
          <span class="md-chip">temp {{ row.temperature }}</span>
          <span class="md-chip">timeout {{ row.timeout }}s</span>
          <span class="md-chip">retry {{ row.max_retries }}</span>
        </template>
      </el-table-column>
      <el-table-column label="上次测试" min-width="180">
        <template #default="{ row }">
          <span v-if="row.last_test && row.last_test.at" class="md-badge" :class="row.last_test.ok ? 'b-ok' : 'b-err'">
            {{ row.last_test.ok ? '连通' : '不通' }}
          </span>
          <span v-if="row.last_test && row.last_test.at" class="md-last">
            {{ fmtAt(row.last_test.at) }}<span v-if="row.last_test.elapsed_ms != null">（{{ row.last_test.elapsed_ms }} ms）</span>
          </span>
          <span v-else class="md-muted">未测试</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="250" fixed="right">
        <template #default="{ row }">
          <el-button v-if="canWrite && !row.is_default" link type="primary" :loading="setting === row.name" @click="doSetDefault(row.name)">
            设为默认
          </el-button>
          <el-button link type="primary" :loading="testing === row.name" @click="doTest(row.name)">连接测试</el-button>
          <el-button v-if="canWrite" link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button v-if="canWrite" link type="danger" @click="doDel(row.name)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <div v-if="!store.models.items.length && !store.models.loading" class="md-empty">
      暂无模型配置，点击「新增模型」添加一条（连接测试通过后再设为默认）
    </div>

    <!-- 新增 / 编辑对话框 -->
    <el-dialog v-model="dlg.visible" :title="dlg.editing ? `编辑模型：${dlg.editing}` : '新增模型'" width="600px" :close-on-click-modal="false">
      <el-form label-width="110px" label-position="left">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="模型标识（唯一），如 qwen" :disabled="!!dlg.editing" />
        </el-form-item>
        <el-form-item label="模型" required>
          <el-input v-model="form.model" placeholder="模型名，如 qwen-max / deepseek-chat" />
        </el-form-item>
        <el-form-item label="base_url" required>
          <el-input v-model="form.base_url" placeholder="OpenAI 兼容端点，如 https://dashscope.aliyuncs.com/compatible-mode/v1" />
        </el-form-item>
        <el-form-item :label="dlg.editing ? 'API Key（留空保留）' : 'API Key'">
          <el-input v-model="form.api_key" type="password" show-password
                    :placeholder="dlg.editing ? '已保存，留空则不变' : '自托管免鉴权可留空'" />
        </el-form-item>
        <el-form-item label="采样温度">
          <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" size="small" />
        </el-form-item>
        <el-form-item label="超时(秒)">
          <el-input-number v-model="form.timeout" :min="1" :max="600" size="small" />
        </el-form-item>
        <el-form-item label="重试次数">
          <el-input-number v-model="form.max_retries" :min="0" :max="10" size="small" />
        </el-form-item>
        <el-form-item label="状态">
          <el-radio-group v-model="form.status">
            <el-radio-button label="enabled">启用</el-radio-button>
            <el-radio-button label="disabled">停用</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.is_default" />
          <span class="md-tip" style="margin-left:8px">设为默认后，运行时立即改用该模型（覆盖环境变量 DMF_LLM-*）</span>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dlg.visible = false">取消</el-button>
        <el-button :loading="probing" @click="probeForm">测试连接</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { store, hasPerm, addModel, updateModel, removeModel, setModelDefault, probeModelCfg, testModelCfg } from '../store'

const canWrite = computed(() => hasPerm('model:write'))

const dlg = reactive({ visible: false, editing: '' })
const form = reactive({
  name: '', model: '', base_url: '', api_key: '',
  temperature: 0.1, timeout: 120, max_retries: 2, status: 'enabled', is_default: false,
})
const probing = ref(false)
const saving = ref(false)
const testing = ref('')
const setting = ref('')

function resetForm() {
  form.name = ''
  form.model = ''
  form.base_url = ''
  form.api_key = ''
  form.temperature = 0.1
  form.timeout = 120
  form.max_retries = 2
  form.status = 'enabled'
  form.is_default = false
}
function openAdd() {
  resetForm()
  dlg.editing = ''
  dlg.visible = true
}
function openEdit(row) {
  resetForm()
  dlg.editing = row.name
  form.name = row.name
  form.model = row.model
  form.base_url = row.base_url
  // api_key 列表接口只回脱敏值，编辑留空 = 保留原值
  form.api_key = ''
  form.temperature = row.temperature ?? 0.1
  form.timeout = row.timeout ?? 120
  form.max_retries = row.max_retries ?? 2
  form.status = row.status || 'enabled'
  form.is_default = !!row.is_default
  dlg.visible = true
}

/* 表单校验：名称 / 模型 / base_url 必填，base_url 需 http(s) 开头 */
function validateForm() {
  if (!form.name.trim()) { ElMessage({ message: '模型名称不能为空', type: 'warning' }); return false }
  if (!form.model.trim()) { ElMessage({ message: '模型名（model）不能为空', type: 'warning' }); return false }
  if (!form.base_url.trim()) { ElMessage({ message: 'base_url 不能为空', type: 'warning' }); return false }
  if (!/^https?:\/\//.test(form.base_url.trim())) { ElMessage({ message: 'base_url 需以 http(s):// 开头', type: 'warning' }); return false }
  return true
}
function payload() {
  return {
    name: form.name.trim(),
    model: form.model.trim(),
    base_url: form.base_url.trim(),
    api_key: form.api_key.trim(), // 编辑时留空 -> 后端保留原 key
    temperature: form.temperature,
    timeout: form.timeout,
    max_retries: form.max_retries,
    status: form.status,
    is_default: !!form.is_default,
  }
}

/* 弹窗预检试连：用当前表单配置（不落库）；编辑时 name 传给后端补齐留空字段 */
async function probeForm() {
  if (!validateForm()) return
  probing.value = true
  try {
    const j = await probeModelCfg({
      name: dlg.editing || '',
      model: form.model.trim(),
      base_url: form.base_url.trim(),
      api_key: form.api_key.trim(),
      timeout: Math.min(form.timeout || 30, 60),
    })
    const extra = j.elapsed_ms != null ? `（${j.elapsed_ms} ms）` : ''
    ElMessage({ message: j.ok ? `连接成功，模型可用${extra}` : j.message, type: j.ok ? 'success' : 'error', duration: 6000 })
  } finally {
    probing.value = false
  }
}

async function submit() {
  if (!validateForm()) return
  saving.value = true
  try {
    const j = dlg.editing
      ? await updateModel(dlg.editing, payload())
      : await addModel(payload())
    ElMessage({ message: j.message, type: j.ok ? 'success' : 'warning', duration: 4000 })
    if (j.ok) dlg.visible = false
  } finally {
    saving.value = false
  }
}

/* 行级连接测试（已保存模型，结果回写 last_test） */
async function doTest(name) {
  testing.value = name
  try {
    const j = await testModelCfg(name)
    const extra = j.elapsed_ms != null ? `（${j.elapsed_ms} ms）` : ''
    ElMessage({ message: (j.ok ? '连接成功，模型可用' : j.message) + extra, type: j.ok ? 'success' : 'error', duration: 6000 })
  } finally {
    testing.value = ''
  }
}

async function doSetDefault(name) {
  setting.value = name
  try {
    const j = await setModelDefault(name)
    ElMessage({ message: j.message, type: j.ok ? 'success' : 'warning', duration: 4000 })
  } finally {
    setting.value = ''
  }
}

async function doDel(name) {
  try {
    await ElMessageBox.confirm(`删除模型「${name}」？删除后若其为默认模型，运行时回落环境变量 DMF_LLM-*。`, '确认删除', { type: 'warning' })
  } catch (e) { return }
  const j = await removeModel(name)
  ElMessage({ message: j.message, type: j.ok ? 'success' : 'warning', duration: 4000 })
}

function fmtAt(iso) {
  try {
    const d = new Date(iso)
    const p = (n) => String(n).padStart(2, '0')
    return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
  } catch (e) { return iso }
}
</script>

<style scoped>
.md-view{max-width:1080px;margin:0 auto;padding:24px 20px 40px}
.md-head{margin-bottom:14px}
.md-title{font-size:20px;font-weight:700;color:var(--text)}
.md-sub{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.7}
.md-tip{font-size:12px;color:var(--muted);line-height:1.7}
.md-bar{display:flex;gap:10px;align-items:center;margin:12px 0}
.md-bar .md-tip{margin-top:0}
.md-table{margin-top:4px}
.md-name{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-weight:600;color:var(--text)}
.md-badge{font-size:10px;border-radius:8px;padding:1px 7px;line-height:16px}
.b-def{color:#8A5A00;background:#FFF4E0}
.b-ok{color:#389E6D;background:#E8F5EE}
.b-err{color:#C0392B;background:#FDECEA}
.b-on{color:var(--muted);background:var(--bg)}
.b-warn{color:#8A5A00;background:#FFF4E0}
.md-chip{font-size:11px;color:var(--text-2);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:1px 7px;margin-right:4px;white-space:nowrap}
.md-last{display:block;font-size:12px;color:var(--muted);margin-top:2px}
.md-muted{font-size:12px;color:var(--muted)}
.md-empty{text-align:center;color:var(--muted);padding:32px 0;font-size:13px}
</style>
