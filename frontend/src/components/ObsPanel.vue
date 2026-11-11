<template>
  <el-drawer v-model="visible" :title="sessionScope ? '对话观测' : '运行观测'" size="min(460px, 94vw)" :with-header="true">
    <div class="obs-body">
      <div v-if="store.obsLoading" class="obs-sec">加载中…</div>
      <div v-else-if="store.obsError" class="obs-sec">观测接口不可用：{{ store.obsError }}</div>
      <template v-else-if="store.obs">
        <div class="obs-scope-bar" :class="{ muted: !sessionScope }">
          <span>{{ sessionScope ? '当前对话的观测内容' : '全局观测（未选中会话）' }}</span>
          <el-button link size="small" @click="goObsPage">{{ sessionScope ? '在专页跟踪此对话' : '打开观测专页' }}</el-button>
        </div>
        <div class="obs-kpis">
          <div class="obs-kpi"><div class="k">对话轮次</div><div class="v">{{ s.total }}</div></div>
          <div class="obs-kpi"><div class="k">成功率</div><div class="v">{{ successRate }}%</div></div>
          <div class="obs-kpi"><div class="k">Token 总消耗</div><div class="v">{{ num(s.total_tokens) }}</div></div>
          <div class="obs-kpi"><div class="k">平均耗时</div><div class="v">{{ s.avg_elapsed_ms }}ms</div></div>
        </div>

        <div class="obs-sec">Token 消耗</div>
        <div class="obs-kpis">
          <div class="obs-kpi"><div class="k">输入 prompt</div><div class="v">{{ num(s.prompt_tokens) }}</div></div>
          <div class="obs-kpi"><div class="k">输出 completion</div><div class="v">{{ num(s.completion_tokens) }}</div></div>
          <div class="obs-kpi"><div class="k">mock 轮次</div><div class="v">{{ s.mock_count }}</div></div>
          <div class="obs-kpi"><div class="k">错误轮次</div><div class="v">{{ s.error_count }}</div></div>
        </div>

        <div class="obs-sec">意图分布</div>
        <div class="obs-list">
          <template v-if="intentEntries.length">
            <div v-for="it in intentEntries" :key="it[0]" class="row"><span>{{ it[0] }}</span><span>{{ it[1] }}</span></div>
          </template>
          <div v-else class="row"><span>暂无数据</span><span>—</span></div>
        </div>

        <div class="obs-sec">技能分布</div>
        <div class="obs-list">
          <template v-if="skillEntries.length">
            <div v-for="it in skillEntries" :key="it[0]" class="row"><span>{{ it[0] }}</span><span>{{ it[1] }}</span></div>
          </template>
          <div v-else class="row"><span>暂无数据</span><span>—</span></div>
        </div>

        <div class="obs-sec">{{ sessionScope ? '本对话轮次' : '最近对话' }}（{{ store.obs.traces.length }}）</div>
        <div v-if="!store.obs.traces.length" class="obs-sec">暂无记录，去对话页提问后回来查看</div>
        <div v-for="(t, i) in recent" :key="i" class="obs-ev">
          <div class="ev-line">
            <span class="ev-intent">{{ t.intent || '未识别' }}</span>
            <span class="ev-tag" :class="t.has_error ? 'err' : 'ok'">{{ t.has_error ? '失败' : '成功' }}</span>
            <span v-if="t.mock_mode" class="ev-tag mock">mock</span>
          </div>
          <div class="ev-meta">
            耗时 {{ t.elapsed_ms == null ? '-' : t.elapsed_ms + 'ms' }}
            <template v-if="t.total_tokens != null">
              · tok {{ t.prompt_tokens || 0 }}/{{ t.completion_tokens || 0 }}（{{ t.total_tokens }}）
            </template>
            <template v-if="t.skills && t.skills.length"> · {{ t.skills.join('、') }}</template>
          </div>
          <div class="ev-time">{{ fmtTime(t.saved_at) }}</div>
        </div>

        <el-button class="obs-refresh" plain @click="reload">刷新</el-button>
      </template>
    </div>
  </el-drawer>
</template>

<script setup>
import { computed, watch } from 'vue'
import { store, refreshObs, sessionTitle, setView, setObsFilter } from '../store'

const props = defineProps({ modelValue: { type: Boolean, default: false } })
const emit = defineEmits(['update:modelValue'])
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

/* 会话范围：对话上方按钮打开时带当前会话；null = 全局 */
const sessionScope = computed(() => store.obsSessionId || null)

function reload() {
  refreshObs(sessionScope.value)
}

/* 跳到观测专页；会话模式下同步把该会话注入专页过滤器（即"跟踪当前对话"） */
function goObsPage() {
  const sid = sessionScope.value
  visible.value = false
  if (sid) setObsFilter({ session_id: sid, intent: '', has_error: '', keyword: '' })
  else setView('obs')
}

const EMPTY = {
  total: 0, mock_count: 0, error_count: 0, avg_elapsed_ms: 0,
  prompt_tokens: 0, completion_tokens: 0, total_tokens: 0,
  intent_distribution: {}, skill_distribution: {}
}
const s = computed(() => (store.obs && store.obs.summary) || EMPTY)
const intentEntries = computed(() => Object.entries(s.value.intent_distribution || {}))
const skillEntries = computed(() => Object.entries(s.value.skill_distribution || {}))
const successRate = computed(() => {
  const total = s.value.total || 0
  if (!total) return 0
  return Math.round(((total - (s.value.error_count || 0)) / total) * 100)
})
const recent = computed(() => ((store.obs && store.obs.traces) || []).slice().reverse())

function num(v) {
  return (v || 0).toLocaleString('zh-CN')
}
function fmtTime(t) {
  if (!t) return '—'
  const d = new Date(t)
  return isNaN(d.getTime()) ? String(t) : d.toLocaleString('zh-CN')
}

watch(() => props.modelValue, (v) => { if (v) reload() })
</script>

<style scoped>
.obs-body{flex:1;overflow-y:auto;padding:14px 18px;height:100%;box-sizing:border-box}
.obs-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:8px;margin-bottom:10px}
.obs-kpi{background:var(--bg);border:1px solid var(--border-soft);border-radius:10px;padding:8px 10px}
.obs-kpi .k{font-size:11px;color:var(--muted)}
.obs-kpi .v{font-size:15px;font-weight:600;color:var(--text);margin-top:2px;word-break:break-all}
.obs-sec{font-size:12px;font-weight:600;color:var(--text-2);margin:12px 0 6px}
.obs-list{font-size:12px;color:var(--text-2);line-height:1.8}
.obs-list .row{display:flex;justify-content:space-between;gap:8px;border-bottom:1px dashed var(--border-soft);padding:3px 0}
.obs-ev{font-size:11px;color:var(--muted);background:var(--bg);border:1px solid var(--border-soft);border-radius:8px;padding:7px 9px;margin-bottom:6px;line-height:1.7}
.obs-ev .ev-line{display:flex;align-items:center;gap:6px}
.obs-ev .ev-intent{font-size:12px;font-weight:600;color:var(--text)}
.obs-ev .ev-tag{border-radius:8px;padding:0 6px;font-size:10px;line-height:16px}
.obs-ev .ev-tag.ok{color:#00a325;background:#E8F5EE}
.obs-ev .ev-tag.err{color:#d93026;background:#FEF1F1}
.obs-ev .ev-tag.mock{color:#a76e00;background:#FFF7E6}
.obs-ev .ev-meta{color:var(--text-2);margin-top:2px}
.obs-ev .ev-time{color:#a9b0bb;font-size:10px;margin-top:1px}
.obs-refresh{width:100%;margin-top:12px}
.obs-scope-bar{display:flex;align-items:center;justify-content:space-between;font-size:12px;color:var(--primary);background:var(--primary-soft,#eef3ff);border:1px solid #d8e4ff;border-radius:8px;padding:6px 10px;margin-bottom:10px}
.obs-scope-bar.muted{color:var(--muted);background:var(--bg);border-color:var(--border-soft)}
</style>
