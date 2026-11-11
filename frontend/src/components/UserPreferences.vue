<template>
  <div class="prefs-page">
    <div class="pf-head">
      <div>
        <h2 class="pf-title">我的偏好</h2>
        <div class="pf-sub">L2 个人记忆 · 用户手动设置优先于系统从点赞自动学习</div>
      </div>
      <div class="pf-head-actions">
        <el-switch
          v-model="memoryOn"
          :loading="store.prefs.saving"
          active-text="记忆注入"
          inline-prompt
          style="--el-switch-on-color: #12b76a"
          @change="onToggleMemory"
        />
        <el-button :icon="Refresh" @click="loadPreferences()">刷新</el-button>
        <el-button type="danger" plain :icon="Delete" :disabled="!store.prefs.items.length" @click="onClearAll">
          清空全部
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="!memoryOn"
      title="记忆注入已关闭：系统不会把历史会话摘要与本页偏好注入到对话中，直到你重新开启。"
      type="warning" :closable="false" show-icon class="pf-alert"
    />
    <el-alert v-if="store.prefs.error" :title="store.prefs.error" type="error" :closable="false" show-icon class="pf-alert" />

    <el-card shadow="never" class="pf-card">
      <div class="pf-card-title">新增 / 覆盖偏好</div>
      <div class="pf-form">
        <el-select
          v-model="form.key" filterable allow-create default-first-option
          placeholder="偏好项（可输入自定义）" style="width: 220px"
        >
          <el-option v-for="k in PRESET_KEYS" :key="k.value" :label="k.label" :value="k.value" />
        </el-select>
        <el-input v-model="form.value" placeholder="偏好值（如 月 / 大区,渠道 / 折线图）" style="flex: 1; min-width: 220px" @keyup.enter="onSubmit" />
        <el-button type="primary" :icon="Plus" :loading="store.prefs.saving" @click="onSubmit">保存</el-button>
      </div>
      <div class="pf-hint">同一偏好项只保留一条，重复保存会覆盖；「来源」标记该条是用户设置还是系统学习。</div>
    </el-card>

    <el-table :data="store.prefs.items" v-loading="store.prefs.loading" border class="pf-table">
      <el-table-column label="偏好项" min-width="180">
        <template #default="{ row }">
          <div class="pf-k">{{ prefKeyLabel(row.key) }}</div>
          <div class="pf-k-raw">{{ row.key }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="value" label="值" min-width="220" show-overflow-tooltip />
      <el-table-column label="来源" width="120">
        <template #default="{ row }">
          <el-tag :type="row.source === 'user' ? 'primary' : 'info'" effect="plain" size="small">
            {{ row.source === 'user' ? '用户设置' : '系统学习' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="置信度" width="100">
        <template #default="{ row }">{{ fmtConfidence(row.confidence) }}</template>
      </el-table-column>
      <el-table-column label="更新时间" min-width="160">
        <template #default="{ row }">{{ fmtTime(row.updated_at || row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="pf-empty">暂无偏好。设置后，回答时会自动沿用你的习惯（如默认粒度、常用维度）。</div>
      </template>
    </el-table>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Plus, Refresh } from '@element-plus/icons-vue'
import {
  store, loadPreferences, upsertPreference, deletePreference, clearPreferences,
  toggleMemoryDisabled, prefKeyLabel
} from '../store'

const PRESET_KEYS = [
  { label: '默认时间粒度', value: 'granularity' },
  { label: '常用维度', value: 'dimensions' },
  { label: '常用指标', value: 'metrics' },
  { label: '默认时间范围', value: 'time_range' },
  { label: '偏好图表类型', value: 'chart_type' },
  { label: '偏好单位', value: 'unit' }
]

const form = reactive({ key: '', value: '' })
const memoryOn = computed({
  get: () => !store.prefs.disabled,
  set: (v) => { store.prefs.disabled = !v }
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

async function onSubmit() {
  if (!form.key || !form.value) {
    ElMessage.warning('偏好项与值都不能为空')
    return
  }
  try {
    await upsertPreference(form.key.trim(), form.value.trim(), 'user')
    ElMessage.success('偏好已保存')
    form.value = ''
  } catch (e) {
    ElMessage.error(e.message || '保存失败')
  }
}

async function onDelete(row) {
  const ok = await ElMessageBox.confirm(`删除偏好「${prefKeyLabel(row.key)}」？`, '删除确认', {
    type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消'
  }).catch(() => null)
  if (!ok) return
  try {
    await deletePreference(row.key)
    ElMessage.success('已删除')
  } catch (e) { ElMessage.error(e.message || '删除失败') }
}

async function onClearAll() {
  const ok = await ElMessageBox.confirm('清空全部偏好？系统将不再沿用你的个人习惯。', '清空确认', {
    type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消'
  }).catch(() => null)
  if (!ok) return
  try {
    const r = await clearPreferences()
    ElMessage.success(`已清空 ${r.removed ?? 0} 条偏好`)
  } catch (e) { ElMessage.error(e.message || '清空失败') }
}

async function onToggleMemory(val) {
  try {
    await toggleMemoryDisabled(!val)
    ElMessage.success(val ? '已开启记忆注入' : '已关闭记忆注入')
  } catch (e) {
    store.prefs.disabled = val // 回滚
    ElMessage.error(e.message || '操作失败')
  }
}

onMounted(() => { if (!store.prefs.items.length) loadPreferences() })
</script>

<style scoped>
/* 视图页根容器必须是滚动容器（body overflow:hidden + .main 不可滚） */
.prefs-page {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  position: relative;
  padding: 18px 22px 28px;
  box-sizing: border-box;
}
.prefs-page > * { flex: none; }
.pf-head { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 10px; }
.pf-title { margin: 0; font-size: 18px; font-weight: 600; color: #1f2329; }
.pf-sub { font-size: 12px; color: #8a9099; margin-top: 4px; }
.pf-head-actions { display: flex; align-items: center; gap: 10px; }
.pf-alert { margin: 8px 0; }
.pf-card { margin: 6px 0 16px; }
.pf-card-title { font-size: 13px; font-weight: 600; color: #1f2329; margin-bottom: 10px; }
.pf-form { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pf-hint { font-size: 12px; color: #8a9099; margin-top: 10px; }
.pf-table { width: 100%; }
.pf-k { font-size: 13px; color: #1f2329; }
.pf-k-raw { font-size: 11px; color: #a8adb5; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.pf-empty { color: #8a9099; font-size: 13px; padding: 16px 0; }
</style>
