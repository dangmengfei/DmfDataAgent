<!--
  FR13 导出预览弹窗（Watermark + 落盘）
  ============================================================================
  三个导出端点（obs / slang / fewshot）共用：先看「导了什么、导了多少、会带上谁的水印」，
  确认后再点「下载 JSON」落盘。

  为什么要有这一步（而不是导出即下载）：
  1) 合规可见性 —— 导出件内含 `watermark`（导出人 / 租户 / 时间 / 行数），预览里把它显式
     摊开给操作者看，避免「下载了一个不知道盖了谁章的文件」。这是 ElWatermark 屏幕水印
     与后端文件水印的「双保险」中的第一层（屏幕层）。
  2) 截断告知 —— 超过 `export_max_rows` 时后端只截断不报错；这里用醒目告警说明被截断，
     避免误以为拿到了全量。
  3) 审批闸门 —— 开启 `security.export_require_approval` 且无权时后端 403，调用方在
     catch 里提示；本弹窗只在拿到 payload 后才打开。
-->
<template>
  <el-dialog
    v-model="visible"
    :title="`导出预览 · ${typeLabel}`"
    width="680px"
    class="exp-dlg"
    append-to-body
  >
    <template v-if="payload">
      <el-alert
        v-if="wm && wm.truncated"
        type="warning"
        :closable="false"
        show-icon
        class="exp-alert"
        title="结果已按导出上限截断"
        :description="`本次导出命中 ${wm.rows} 行上限（security.export_max_rows=${wm.max_rows}），超出部分未包含。如需全量请缩小筛选范围后分批导出。`"
      />

      <el-descriptions :column="2" size="small" border class="exp-desc">
        <el-descriptions-item label="导出类型">{{ typeLabel }}</el-descriptions-item>
        <el-descriptions-item label="记录数">
          <b>{{ totalCount }}</b>
        </el-descriptions-item>
        <el-descriptions-item label="导出人">
          {{ wm ? (wm.exported_by || '-') : '-（未开启水印）' }}
        </el-descriptions-item>
        <el-descriptions-item label="租户">{{ wm ? (wm.tenant_id || '全局') : '-' }}</el-descriptions-item>
        <el-descriptions-item label="导出时间" :span="2">
          {{ wm ? fmtTime(wm.exported_at) : '-' }}
        </el-descriptions-item>
      </el-descriptions>

      <!-- 屏幕水印层：与导出件内的 watermark 块互为双保险 -->
      <el-watermark
        v-if="wm && wmText"
        :content="wmText"
        :font="{ color: 'rgba(31,35,41,.16)', fontSize: 13 }"
        :gap="[120, 90]"
        class="exp-wm"
      >
        <div class="exp-preview">
          <div class="exp-preview-hd">内容预览（前 {{ previewItems.length }} 条，完整内容以下载文件为准）</div>
          <pre class="exp-json">{{ previewText }}</pre>
        </div>
      </el-watermark>
      <div v-else class="exp-preview">
        <div class="exp-preview-hd">内容预览（前 {{ previewItems.length }} 条）</div>
        <pre class="exp-json">{{ previewText }}</pre>
      </div>

      <div v-if="wm" class="exp-notice">
        <el-icon><InfoFilled /></el-icon>
        <span>{{ wm.notice }}</span>
      </div>
    </template>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" :icon="Download" @click="onDownload">下载 JSON</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, InfoFilled } from '@element-plus/icons-vue'
import { downloadJson } from '../store'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /* 后端导出返回体：{ version, exported_at, count, watermark, items } */
  payload: { type: Object, default: null },
  /* fewshot | slang | obs，仅用于文案与文件名 */
  kind: { type: String, default: 'data' }
})

const emit = defineEmits(['update:modelValue'])
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

const FILE_STEM = { fewshot: 'few-shots', slang: 'slang-dict', obs: 'obs-traces' }
const TYPE_LABEL = { fewshot: 'Few-Shot 样本', slang: '黑话词条', obs: '运行观测 Trace' }

const typeLabel = computed(() => TYPE_LABEL[props.kind] || '数据')
const wm = computed(() => (props.payload && props.payload.watermark) || null)

/* 兼容三类导出体的形状：
   - fewshot / slang: { count, items: [...] }
   - obs(otlp):       { resourceSpans: [...] }（无 count/items）
   - obs(json):       { rounds: [...], spans: [...] } */
const previewItems = computed(() => {
  const p = props.payload || {}
  const arr = p.items || p.spans || p.rounds || []
  return arr.slice(0, 5)
})
/* OTLP 形状没有现成 rows，用 resourceSpans 里的 span 数近似 */
function countOf(p) {
  if (!p) return 0
  if (typeof p.count === 'number') return p.count
  const direct = p.items || p.spans || p.rounds
  if (direct) return direct.length
  const rs = p.resourceSpans
  if (Array.isArray(rs)) {
    return rs.reduce((n, r) => n + ((r.scopeSpans && r.scopeSpans[0] && r.scopeSpans[0].spans) || []).length, 0)
  }
  return 0
}
const totalCount = computed(() => countOf(props.payload))
const previewText = computed(() => {
  try {
    return JSON.stringify(previewItems.value, null, 2)
  } catch (e) {
    return '（预览序列化失败）'
  }
})
const wmText = computed(() => {
  const w = wm.value
  if (!w) return ''
  return `${w.exported_by || '未知'} · ${w.tenant_id || '全局'} · ${fmtTime(w.exported_at)}`
})

function fmtTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function localDay() {
  /* 用本地日期而非 toISOString()（后者是 UTC，跨零点会差一天） */
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function onDownload() {
  const stem = FILE_STEM[props.kind] || 'export'
  try {
    downloadJson(props.payload, `${stem}-${localDay()}.json`)
    ElMessage.success(`已下载 ${totalCount.value} 条（含导出水印）`)
    visible.value = false
  } catch (e) {
    ElMessage.error('下载失败：' + (e && e.message ? e.message : e))
  }
}
</script>

<style scoped>
.exp-alert { margin-bottom: 12px; }
.exp-desc { margin-bottom: 12px; }
.exp-wm { border-radius: 8px; }
.exp-preview {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fbfcfd;
  overflow: hidden;
}
.exp-preview-hd {
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-2);
  background: var(--border-soft);
  border-bottom: 1px solid var(--border);
}
.exp-json {
  margin: 0;
  padding: 10px;
  max-height: 260px;
  overflow: auto;
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-all;
}
.exp-notice {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--muted);
}
</style>
