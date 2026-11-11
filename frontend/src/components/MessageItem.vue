<template>
  <div class="msg" :class="msg.role">
    <div class="avatar">{{ msg.role === 'user' ? '我' : 'DA' }}</div>
    <div class="body">
      <!-- 阶段进度条（assistant 流式期间） -->
      <div v-if="msg.role === 'assistant' && (msg.pending || hasStages)" class="stages">
        <span
          v-for="(s, i) in STAGES"
          :key="i"
          class="stage"
          :class="{ on: msg.stages && msg.stages[i] === 1, err: msg.stages && msg.stages[i] === 2 }"
        >{{ msg.stages && msg.stages[i] ? (msg.stages[i] === 2 ? '⚠ ' : '') + s + ' ✓' : s }}</span>
      </div>
      <!-- 思考过程：节点级流式时间线（各模块执行过程 + 中间产物） -->
      <div v-if="msg.role === 'assistant' && msg.trace && msg.trace.length" class="trace-box">
        <div class="trace-head" @click="open = !open">
          <span class="trace-title">思考过程</span>
          <span class="trace-head-sub">
            {{ msg.trace.length }} 步
            <template v-if="traceSumMs">· 节点合计 {{ traceSumMs }}ms</template>
            <template v-else-if="msg.pending">· 计时中…</template>
            <template v-if="msg.elapsedMs != null && traceSumMs">· 整轮 {{ msg.elapsedMs }}ms</template>
          </span>
          <span class="trace-toggle">{{ open ? '收起 ▲' : '展开 ▼' }}</span>
        </div>
        <div v-show="open" class="trace-body">
          <div v-for="(t, i) in msg.trace" :key="i" class="trace-node" :class="t.status">
            <span class="t-ico">
              <span v-if="t.status === 'pending'" class="spinner sm"></span>
              <span v-else>{{ ico(t.status) }}</span>
            </span>
            <div class="t-main">
              <div class="t-label">
                <span class="t-name">{{ t.label }}</span>
                <span class="t-st" :class="t.status">{{ stText(t.status) }}</span>
                <span v-if="t.elapsed_ms != null" class="t-dur">{{ t.elapsed_ms }}ms</span>
                <span v-else-if="t.status === 'pending'" class="t-dur live">
                  {{ liveMs(t.started_at) }}ms
                </span>
              </div>
              <div v-if="t.detail" class="t-detail">{{ t.detail }}</div>
              <pre v-if="t.json && openJson === i" class="t-json">{{ jsonText(t.json) }}</pre>
            </div>
            <button v-if="t.json" class="t-json-btn" @click="openJson = openJson === i ? -1 : i">
              {{ openJson === i ? '收起' : 'JSON' }}
            </button>
          </div>
          <div v-if="msg.pending" class="trace-pending">
            <span class="spinner"></span>后续模块执行中…
          </div>
        </div>
      </div>
      <!-- 查 SQL 面板：本轮执行的 SQL + 取数明细（查了什么、来自哪张表、返回多少行、耗时） -->
      <div v-if="msg.role === 'assistant' && msg.sqlInfo && msg.sqlInfo.sql" class="sql-box">
        <div class="sql-head" @click="sqlOpen = !sqlOpen">
          <span class="sql-title">执行的 SQL</span>
          <span class="sql-head-sub">
            <template v-if="msg.sqlInfo.engine">引擎 {{ msg.sqlInfo.engine }}</template>
            <template v-if="msg.sqlInfo.models && msg.sqlInfo.models.length"> · 表 {{ msg.sqlInfo.models.join('、') }}</template>
            <template v-if="msg.sqlInfo.rowCount != null"> · {{ msg.sqlInfo.rowCount }} 行</template>
            <template v-if="msg.sqlInfo.elapsedMs != null"> · {{ msg.sqlInfo.elapsedMs }}ms</template>
            <template v-if="msg.sqlInfo.cached"> · <span class="sql-cached">缓存命中</span></template>
            <template v-if="msg.sqlInfo.dataAsOf"> · 数据截至 {{ msg.sqlInfo.dataAsOf }}</template>
          </span>
          <span class="sql-toggle">{{ sqlOpen ? '收起 ▲' : '展开 ▼' }}</span>
        </div>
        <!-- FR12 口径说明：默认可见（结果可解释性），明细按需展开。
             刻意放在 sql-body 之外——口径是结果的属性，不该藏在「执行的 SQL」折叠面板里。 -->
        <div v-if="msg.sqlInfo.caliber" class="sql-caliber" title="口径说明：本次结果所用的指标公式 / 时间窗 / 过滤 / 同环比 / 版本，用于结果可追溯">
          <div class="sc-line">
            <span class="sc-label">口径</span>{{ msg.sqlInfo.caliber.watermark }}
          </div>
          <details v-if="caliberRows(msg.sqlInfo.caliber).length" class="sc-more">
            <summary>口径明细<span class="sc-ver" v-if="msg.sqlInfo.caliber.version"> · {{ msg.sqlInfo.caliber.version }}</span></summary>
            <div class="sc-detail">
              <div v-for="(r, i) in caliberRows(msg.sqlInfo.caliber)" :key="'sc' + i" class="sc-row">
                <span class="sc-k">{{ r.k }}</span>
                <span class="sc-v">{{ r.v }}</span>
              </div>
            </div>
          </details>
        </div>
        <div v-show="sqlOpen" class="sql-body">
          <div class="sql-actions">
            <button class="sql-btn" @click="copySql">复制 SQL</button>
          </div>
          <pre class="sql-pre"><code>{{ msg.sqlInfo.sql }}</code></pre>
          <div v-if="msg.sqlInfo.error" class="sql-err">⚠ 取数异常：{{ msg.sqlInfo.error }}</div>
        </div>
      </div>
      <!-- 查询改写提示（黑话翻译/指代消解等） -->
      <div v-if="msg.rewriteMeta" class="rewrite-badge" :title="rewriteTitle(msg)">
        <span class="rw-tag">{{ msg.rewriteMeta.hits && msg.rewriteMeta.hits.length ? '黑话改写' : '改写' }}</span>
        <span class="rw-orig">{{ msg.rewriteMeta.original }}</span>
        <span class="rw-arrow">→</span>
        <span class="rw-new">{{ msg.rewriteMeta.rewritten }}</span>
        <span v-if="msg.rewriteMeta.hits && msg.rewriteMeta.hits.length" class="rw-src">
          命中 {{ msg.rewriteMeta.hits.length }} 条：{{ rewriteDesc(msg).slice(0, 40) }}
        </span>
        <span v-if="msg.rewriteMeta.degraded" class="rw-grain">降级</span>
        <span v-if="!msg.rewriteMeta.degraded && canFeedback" class="rw-fb">
          <button class="rw-fb-btn" :class="{ on: slangFb === 1 }" title="改写正确（再点取消）" @click="onRewriteFeedback(true)">👍</button>
          <button class="rw-fb-btn" :class="{ on: slangFb === -1 }" title="误改写（再点取消）" @click="onRewriteFeedback(false)">👎</button>
        </span>
      </div>
      <!-- 时间要素识别（相对时间/节假日/二十四节气） -->
      <div v-if="msg.timeExtract" class="time-badge" title="时间要素识别（相对时间 / 节假日 / 二十四节气）">
        <span class="rw-tag">时间</span>
        <template v-if="msg.timeExtract.nodes && msg.timeExtract.nodes.length">
          <span>{{ msg.timeExtract.nodes.join('、') }}</span>
        </template>
        <template v-else-if="msg.timeExtract.start && msg.timeExtract.end">
          <span>{{ msg.timeExtract.start }} ~ {{ msg.timeExtract.end }}</span>
        </template>
        <span v-if="msg.timeExtract.grain" class="rw-grain">{{ msg.timeExtract.grain }}</span>
        <span v-if="msg.timeExtract.entities && msg.timeExtract.entities.length" class="rw-src">
          命中：{{ msg.timeExtract.entities.slice(0, 3).map(e => e.name).join('、') }}
        </span>
      </div>
      <!-- 意图徽标 -->
      <span v-if="msg.intentMeta && msg.intentMeta.intent" class="intent-badge">
        意图 <b>{{ msg.intentMeta.intent }}</b>
        <span v-if="msg.intentMeta.confidence != null" class="conf"> · 置信度 {{ Math.round(msg.intentMeta.confidence * 100) }}%</span>
        <span v-if="srcLabel" class="src"> · {{ srcLabel }}</span>
        <span v-if="msg.wasMock" class="src"> · mock 数据</span>
      </span>
      <div v-if="msg.role === 'user' && editing" class="edit-box">
        <textarea v-model="draft" class="edit-area" placeholder="修改提问后重新发送…" @keydown.ctrl.enter="saveEdit" @keydown.meta.enter="saveEdit"></textarea>
        <div class="edit-actions">
          <button class="ma-btn primary" :title="'保留原对话，把改后的提问作为新一轮追加到末尾'" @click="saveEdit">追加发送</button>
          <button
            v-if="msg.id"
            class="ma-btn danger"
            :title="'删除这条提问及其之后的全部轮次，再以改后的内容重发（旧问答不可恢复）'"
            @click="saveReplace"
          >替换该轮</button>
          <button class="ma-btn ghost" @click="cancelEdit">取消</button>
        </div>
        <div class="edit-hint">追加发送＝保留原对话再问一遍；替换该轮＝删掉这条及其之后的轮次重问</div>
      </div>
      <div v-else class="bubble">
        <!-- 流式打字动画 -->
        <span v-if="msg.pending && !msg.content" class="typing-dots"><i></i><i></i><i></i></span>
        <!-- Markdown 渲染（离线防 XSS 渲染器） -->
        <template v-else-if="msg.content">
          <div v-html="html"></div>
          <!-- 数据可视化（趋势线/排名柱/占比/归因贡献） -->
          <ChartBox v-for="(v, i) in msg.viz" :key="i" :viz="v" />
        </template>
      </div>
      <!-- 用户消息底部操作条：编辑 / 复制（图标形式，置于会话下方） -->
      <div v-if="isUser && !msg.pending && !editing" class="msg-bottom">
        <button class="mb-btn" title="编辑这条提问并重新发送" @click="startEdit">
          <svg viewBox="0 0 24 24" class="mb-ico"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a.9959.9959 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
        </button>
        <button class="mb-btn" :title="copied ? '已复制' : copyTip" @click="copyText">
          <svg v-if="!copied" viewBox="0 0 24 24" class="mb-ico"><path fill="currentColor" d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
          <svg v-else viewBox="0 0 24 24" class="mb-ico"><path fill="currentColor" d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
        </button>
      </div>
      <!-- 反问澄清：结构化确认表单（指标/维度/时间，候选按置信度排序）；
           用户点选提交后系统自动继续执行后续取数步骤 -->
      <ClarifyForm v-if="msg.role === 'assistant' && msg.clarify" :msg="msg" />
      <!-- 本轮观测信息（OpenTelemetry：耗时 / token / span 数 / trace_id） -->
      <div v-if="obs" class="obs-meta">
        <span class="om-dot" :class="msg.failed ? 'err' : 'ok'"></span>
        <span class="om-item">耗时 <b>{{ obs.elapsed == null ? '—' : obs.elapsed + 'ms' }}</b></span>
        <span v-if="obs.tokens" class="om-item">
          Token <b>{{ fmtNum(obs.tokens.total) }}</b><i>{{ fmtNum(obs.tokens.prompt) }}/{{ fmtNum(obs.tokens.completion) }}</i>
        </span>
        <span v-if="obs.spanCount" class="om-item">Span <b>{{ obs.spanCount }}</b></span>
        <span v-if="obs.traceId" class="om-item mono" :title="obs.traceId">trace {{ obs.traceId.slice(0, 10) }}…</span>
        <button v-if="obs.nodes.length" class="om-btn" @click="obsOpen = !obsOpen">
          {{ obsOpen ? '收起节点' : '节点耗时' }}
        </button>
      </div>
      <div v-if="obsOpen && obs && obs.nodes.length" class="obs-nodes">
        <div v-for="(nd, i) in obs.nodes" :key="i" class="obs-node">
          <span class="on-name" :title="nd.name">{{ nodeLabel(nd.name) }}</span>
          <span class="on-bar"><i :style="{ width: nodePct(nd) + '%' }" :class="nd.status_code === 'ERROR' ? 'err' : ''"></i></span>
          <span class="on-dur">{{ nd.duration_ms == null ? '—' : nd.duration_ms + 'ms' }}</span>
        </div>
      </div>
      <!-- 点赞 / 点踩反馈：点赞进 few-shot 样本库，点踩进 bad case 库；再点一次同类型 = 取消 -->
      <div v-if="msg.role === 'assistant' && !msg.pending" class="feedback-bar">
        <!-- 重新生成：仅最后一轮可见（中间轮重生成会连带丢弃后续对话） -->
        <button
          v-if="canRegenerate"
          class="fb-btn fb-regen"
          title="重新生成本轮回答：删除当前回答，以同一提问重新执行"
          @click="onRegenerate"
        >
          <svg viewBox="0 0 24 24" class="fb-ico"><path fill="currentColor" d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-8 8s3.57 8 8 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
          <span class="fb-txt">重新生成</span>
        </button>
        <button
          class="fb-btn"
          :class="{ active: fb === 'like' }"
          @click="onFeedback('like')"
          title="点赞：加入 few-shot 样本库，意图识别时优先匹配（再点一次取消）"
        >
          <svg viewBox="0 0 24 24" class="fb-ico"><path fill="currentColor" d="M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z"/></svg>
          <span class="fb-txt">赞</span>
        </button>
        <button
          class="fb-btn is-dislike"
          :class="{ active: fb === 'dislike' }"
          @click="onFeedback('dislike')"
          title="点踩：加入 bad case 库，供后续分析与回流（再点一次取消）"
        >
          <svg viewBox="0 0 24 24" class="fb-ico"><path fill="currentColor" d="M15 3H6c-.83 0-1.54.5-1.84 1.22l-3.02 7.05c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z"/></svg>
          <span class="fb-txt">踩</span>
        </button>
        <button class="mb-btn fb-copy" :title="copied ? '已复制' : copyTip" @click="copyText">
          <svg v-if="!copied" viewBox="0 0 24 24" class="mb-ico"><path fill="currentColor" d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
          <svg v-else viewBox="0 0 24 24" class="mb-ico"><path fill="currentColor" d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { STAGES, SRC_MAP, store, send, replaceRound, regenerateRound, submitFeedback, submitSlangRewriteFeedback } from '../store'
import { renderMarkdown } from '../utils/markdown'
import { caliberRows } from '../utils/caliber'
import ChartBox from './ChartBox.vue'
import ClarifyForm from './ClarifyForm.vue'

const props = defineProps({
  msg: { type: Object, required: true },
  /* 是否为会话最后一条消息：重新生成仅对最后一轮开放 */
  isLast: { type: Boolean, default: false }
})
const open = ref(true)
const openJson = ref(-1)
const obsOpen = ref(false)
const sqlOpen = ref(true)
const nowTick = ref(Date.now())
let tickHandle = null

watch(() => props.msg.pending, (pending) => {
  if (pending) {
    nowTick.value = Date.now()
    if (!tickHandle) tickHandle = setInterval(() => { nowTick.value = Date.now() }, 100)
  } else if (tickHandle) {
    clearInterval(tickHandle)
    tickHandle = null
  }
}, { immediate: true })
onBeforeUnmount(() => { if (tickHandle) clearInterval(tickHandle) })

function liveMs(startedAt) {
  if (!startedAt) return '0'
  // 后端 started_at 是 unix 秒（time.time()），转成 ms 算 delta
  const s = typeof startedAt === 'number' ? startedAt : new Date(startedAt).getTime() / 1000
  const ms = Math.max(0, Math.round((nowTick.value / 1000 - s) * 1000))
  return ms.toLocaleString('zh-CN')
}

const html = computed(() => renderMarkdown(props.msg.content))
const srcLabel = computed(() => {
  const s = props.msg.intentMeta && props.msg.intentMeta.source
  return s ? SRC_MAP[s] || s : ''
})
const hasStages = computed(() => props.msg.stages && props.msg.stages.some(v => v > 0))

/* 「思考过程」里可见步骤的耗时合计（与逐条展示一致，便于自检） */
const traceSumMs = computed(() => {
  const list = props.msg.trace || []
  const total = list.reduce((acc, t) => acc + (typeof t.elapsed_ms === 'number' ? t.elapsed_ms : 0), 0)
  return total > 0 ? Math.round(total * 10) / 10 : 0
})

/* ---- 黑话改写徽标：命中明细（黑话 → 标准名 / 删除） ---- */
function rwHitText(h) {
  const target = h.standard || (Array.isArray(h.expand) ? h.expand.join('/') : '') || '删除'
  return `${h.term} → ${target}`
}
function rewriteDesc(msg) {
  const hits = (msg.rewriteMeta && msg.rewriteMeta.hits) || []
  return hits.map(rwHitText).join('、')
}

/* 改写反馈：对话内 👍/👎 回写，喂给「改写命中率/误改写率看板」
   - 点击显示、再点同类型取消（toggle）；状态写入后端 + 本地乐观高亮，重开会话回显不变
   - message_id 用 roundId（= 后端 messages.id = 改写事件回落定位键），与点赞/点踩同口径 */
const canFeedback = computed(() => {
  const rm = props.msg.rewriteMeta
  return !!rm && !rm.degraded
})
async function onRewriteFeedback(correct) {
  const rm = props.msg.rewriteMeta
  if (!rm) return
  await submitSlangRewriteFeedback(props.msg.roundId, correct, {
    original: rm.original,
    sessionId: props.msg.sessionId || ''
  })
}
function rewriteTitle(msg) {
  const meta = msg.rewriteMeta || {}
  const head = '查询改写（PRD 5.3.1：黑话翻译 / 口语归一 / 指代消解）'
  if (!meta.hits || !meta.hits.length) return head
  return `${head}\n${meta.hits.map(h => `· ${rwHitText(h)}（${h.scope || ''} / ${h.rewrite_mode || ''}）`).join('\n')}`
}

/* 本轮观测信息：后端 node_trace.otel + elapsed_ms/tokens（历史消息同样具备） */
const obs = computed(() => {
  const m = props.msg
  if (m.role !== 'assistant') return null
  const o = m.otel || {}
  const nodes = Array.isArray(o.node_spans) ? o.node_spans : []
  const spanCount = o.span_count || 0
  if (m.elapsedMs == null && !m.tokens && !spanCount && !o.trace_id && !nodes.length) return null
  return { elapsed: m.elapsedMs, tokens: m.tokens, spanCount, traceId: o.trace_id || '', nodes }
})
function nodePct(nd) {
  const nodes = (obs.value && obs.value.nodes) || []
  const max = Math.max(...nodes.map(x => x.duration_ms || 0), 1)
  return Math.max(3, Math.round(((nd.duration_ms || 0) / max) * 100))
}
function fmtNum(v) {
  return (v || 0).toLocaleString('zh-CN')
}

function ico(s){ return { ok: '✓', pending: '…', skip: '–', ask: '?', empty: '!', error: '✕' }[s] || '·' }
function stText(s){ return { ok: '完成', pending: '运行中', skip: '跳过', ask: '反问', empty: '空数据', error: '失败' }[s] || '处理中' }
function jsonText(j){ return typeof j === 'string' ? j : JSON.stringify(j, null, 2) }

/* 节点耗时：将后端 OTel span 名（如 node.rewrite_query）映射为中文显示；
   未知节点保留原始名（去掉 node. 前缀）作为兜底 */
const NODE_LABELS = {
  rewrite_query: '查询改写',
  classify_intent: '意图识别',
  apply_clarification: '应用澄清',
  extract_elements: '要素抽取',
  validate_elements: '要素校验',
  ask_clarification: '反问澄清',
  build_dsl: '构建查询',
  execute_query: '执行查询',
  analyze_data: '数据分析',
  diagnose_data: '数据诊断',
  retrieve_knowledge: '知识检索',
  create_metric: '创建指标',
  synthesize_answer: '答案合成',
  subagent_dispatch: '子任务分发',
  subagent: '子任务执行',
  reason: '推理',
  retrieve: '检索',
  generate: '生成',
}
function nodeLabel(name) {
  if (!name) return name || ''
  const key = String(name).replace(/^node\./, '')
  return NODE_LABELS[key] || key
}

/* 当前消息的反馈状态（点赞 / 点踩）：来自 store.feedbackMap，按 roundId 索引 */
const fb = computed(() => store.feedbackMap[props.msg.roundId] || null)
function onFeedback(type) {
  submitFeedback(props.msg.roundId, type)
}

/* 重新生成：仅最后一轮的助手消息、且当前无在途生成时可点（失败轮同样可重生成） */
const canRegenerate = computed(
  () => props.isLast && !props.msg.pending && !store.sending && !!store.sessionId
)
function onRegenerate() {
  regenerateRound(props.msg)
}
/* 当前消息的黑话改写反馈状态（👍 正确 / 👎 误改写）：来自 store.slangFeedbackMap，按 roundId 索引
   - 1 正确 / -1 误改写 / 0 未评；点击显示、再点同类型取消，重开会话从后端回显 */
const slangFb = computed(() => store.slangFeedbackMap[props.msg.roundId] || 0)

/* ---- 复制 / 编辑重发 ----
   - 复制：用户提问与助手回答均可一键复制原文（markdown 源码，便于复用 / 二次编辑）
   - 编辑：仅用户消息可编辑，两种发送模式：
     · 追加发送（默认）：以修改后的内容重新发起一轮，原对话保留——「改了这条再问一遍」；
     · 替换该轮：删掉这条提问及其之后的全部轮次再重发——「我当初就该这么问」，上下文更干净。
       依赖后端 `DELETE /sessions/{id}/messages/after/{message_id}`（T198e）。 */
const isUser = computed(() => props.msg.role === 'user')
const editing = ref(false)
const draft = ref('')
const copied = ref(false)
const copyTip = computed(() => (isUser.value ? '复制问题' : '复制回答'))
function startEdit() {
  if (store.sending) { ElMessage.warning('正在生成回答，请稍后再编辑'); return }
  draft.value = props.msg.content || ''
  editing.value = true
}
function cancelEdit() { editing.value = false }
async function saveEdit() {
  const t = (draft.value || '').trim()
  if (!t) { ElMessage.warning('内容不能为空'); return }
  editing.value = false
  // 内容未变 → 等同于取消，不再发起新轮
  if (t === (props.msg.content || '').trim()) return
  if (store.sending) { ElMessage.warning('正在生成回答，请稍后再发送'); return }
  // 以修改后的内容重新发起一轮对话
  await send(t)
}
/* 「替换该轮」：先确认（不可恢复），再交由 store 裁掉这条及之后的轮次并重发 */
async function saveReplace() {
  const t = (draft.value || '').trim()
  if (!t) { ElMessage.warning('内容不能为空'); return }
  if (store.sending) { ElMessage.warning('正在生成回答，请稍后再发送'); return }
  if (!props.msg.id) { ElMessage.error('该消息缺少服务端标识，无法替换'); return }
  if (!window.confirm('「替换该轮」会删除这条提问及其之后的全部轮次，且不可恢复。是否继续？')) return
  editing.value = false
  await replaceRound(props.msg.id, t)
}
async function copyText() {
  const text = props.msg.content || ''
  if (!text) return
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    copied.value = true
    setTimeout(() => { copied.value = false }, 1500)
  } catch (e) {
    ElMessage.error('复制失败')
  }
}

async function copySql() {
  const sql = (props.msg.sqlInfo && props.msg.sqlInfo.sql) || ''
  if (!sql) return
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(sql)
    } else {
      const ta = document.createElement('textarea')
      ta.value = sql
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success('已复制 SQL')
  } catch (e) {
    ElMessage.error('复制失败')
  }
}
</script>

<style scoped>
.trace-box{margin:8px 0 4px;background:var(--bg);border:1px solid var(--border-soft);border-radius:10px;overflow:hidden}
.trace-head{display:flex;align-items:center;justify-content:space-between;padding:7px 12px;cursor:pointer;font-size:12px;color:var(--text-2);user-select:none;gap:10px}
.trace-head:hover{color:var(--primary)}
.trace-title{font-weight:600;color:var(--text)}
.trace-head-sub{font-size:11px;color:var(--muted);font-family:ui-monospace,Menlo,monospace;flex:1;text-align:left;margin-left:8px}
.trace-toggle{font-size:11px;color:var(--muted);flex:none}
.t-dur{font-size:10px;color:var(--muted);font-family:ui-monospace,Menlo,monospace;background:var(--card);border:1px solid var(--border-soft);border-radius:6px;padding:0 6px;line-height:14px;flex:none}
.t-dur.live{color:var(--primary);background:#EAF1FF;border-color:#EAF1FF;animation:pulse 1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.6}50%{opacity:1}}
.spinner.sm{width:9px;height:9px;border-width:2px;display:inline-block;vertical-align:middle}
.trace-body{padding:2px 12px 10px;border-top:1px dashed var(--border-soft)}
.trace-node{display:flex;align-items:flex-start;gap:8px;padding:7px 0;border-bottom:1px dashed var(--border-soft);font-size:12px}
.trace-node:last-child{border-bottom:none}
.t-ico{flex:none;width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;margin-top:1px}
.trace-node.ok .t-ico{background:#E8F5EE;color:#00a325}
.trace-node.skip .t-ico{background:var(--border-soft);color:var(--muted)}
.trace-node.ask .t-ico{background:#EAF1FF;color:var(--primary)}
.trace-node.empty .t-ico,.trace-node.error .t-ico{background:#FEF1F1;color:#f53f3f}
.t-main{flex:1;min-width:0}
.t-label{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.t-name{font-weight:600;color:var(--text)}
.t-st{font-size:10px;border-radius:6px;padding:0 6px;line-height:16px}
.t-st.ok{color:#00a325;background:#E8F5EE}
.t-st.skip{color:var(--muted);background:var(--border-soft)}
.t-st.ask{color:var(--primary);background:#EAF1FF}
.t-st.empty,.t-st.error{color:#f53f3f;background:#FEF1F1}
.t-detail{color:var(--text-2);margin-top:3px;line-height:1.6;word-break:break-all}
.t-json{margin-top:6px;padding:8px 10px;background:var(--card);border:1px solid var(--border-soft);border-radius:8px;font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--text-2);line-height:1.6;max-height:220px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.t-json-btn{flex:none;font-size:10px;color:var(--muted);background:var(--card);border:1px solid var(--border);border-radius:6px;padding:1px 8px;cursor:pointer;line-height:18px}
.t-json-btn:hover{color:var(--primary);border-color:var(--primary)}
.trace-pending{display:flex;align-items:center;gap:8px;padding:8px 0 2px;font-size:11px;color:var(--muted)}
.spinner{width:10px;height:10px;border:2px solid var(--border);border-top-color:var(--primary);border-radius:50%;animation:trspin .8s linear infinite;display:inline-block}
@keyframes trspin{to{transform:rotate(360deg)}}

/* 本轮观测信息条 */
.obs-meta{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:6px;padding:5px 10px;background:var(--bg);border:1px solid var(--border-soft);border-radius:8px;font-size:11px;color:var(--muted)}
.om-dot{width:6px;height:6px;border-radius:50%;flex:none}
.om-dot.ok{background:#00b42a}
.om-dot.err{background:#f53f3f}
.om-item b{color:var(--text-2);font-weight:600}
.om-item i{font-style:normal;color:#a9b0bb;margin-left:3px}
.om-item.mono{font-family:ui-monospace,Menlo,monospace}
.om-btn{flex:none;font-size:10px;color:var(--muted);background:var(--card);border:1px solid var(--border);border-radius:6px;padding:1px 8px;cursor:pointer;line-height:16px}
.om-btn:hover{color:var(--primary);border-color:var(--primary)}
.obs-nodes{margin-top:4px;padding:7px 10px;background:var(--bg);border:1px solid var(--border-soft);border-radius:8px;display:flex;flex-direction:column;gap:5px}
.obs-node{display:flex;align-items:center;gap:8px;font-size:11px}
.on-name{width:120px;flex:none;color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.on-bar{flex:1;height:7px;background:var(--border-soft);border-radius:4px;overflow:hidden}
.on-bar i{display:block;height:100%;background:linear-gradient(90deg,#8bb4ff,#3370ff);border-radius:4px}
.on-bar i.err{background:linear-gradient(90deg,#ffb3b3,#f53f3f)}
.on-dur{width:60px;text-align:right;flex:none;color:var(--muted);font-family:ui-monospace,Menlo,monospace}

/* 点赞 / 点踩反馈条（拇指按钮，再点一次同类型 = 取消） */
.feedback-bar{display:flex;gap:8px;margin-top:8px}
.fb-btn{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--muted);background:var(--card);border:1px solid var(--border);border-radius:8px;padding:3px 12px;cursor:pointer;line-height:20px;transition:all .15s;user-select:none}
.fb-btn:hover{color:var(--primary);border-color:var(--primary)}
.fb-ico{width:15px;height:15px;flex:none}
.fb-ico.flip{transform:rotate(180deg)}
.fb-btn.active{color:#fff;background:var(--primary);border-color:var(--primary)}
.fb-btn.active:hover{opacity:.92}
/* 点踩激活态单独取红色系 */
.fb-btn.active.is-dislike{background:#f53f3f;border-color:#f53f3f}
/* 重新生成：功能性操作，与点赞/点踩（评价）稍作区隔 */
.fb-regen{margin-right:4px}

/* 消息底部操作条（编辑 / 复制）：图标形式，置于会话下方 */
.msg-bottom{display:flex;gap:6px;margin-top:8px}
.mb-btn{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:8px;color:var(--muted);background:var(--card);border:1px solid var(--border);cursor:pointer;transition:all .15s;user-select:none}
.mb-btn:hover{color:var(--primary);border-color:var(--primary)}
.mb-ico{width:16px;height:16px;flex:none}
/* 助手反馈条内的复制按钮靠右对齐 */
.fb-copy{margin-left:auto}
/* 编辑态：内联输入框替换气泡 */
.edit-box{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:10px;margin-top:4px}
.edit-area{width:100%;min-height:60px;max-height:240px;resize:vertical;font-size:14px;line-height:1.6;color:var(--text);background:var(--bg);border:1px solid var(--border-soft);border-radius:8px;padding:8px 10px;font-family:inherit;outline:none}
.edit-area:focus{border-color:var(--primary)}
.edit-actions{display:flex;justify-content:flex-end;gap:6px;margin-top:8px;flex-wrap:wrap}
/* 编辑重发按钮组：之前只写了类名没给样式，会退化成浏览器默认按钮，这里补齐 */
.ma-btn{display:inline-flex;align-items:center;justify-content:center;height:30px;padding:0 14px;border-radius:8px;font-size:13px;line-height:1;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text);transition:all .15s;user-select:none;font-family:inherit}
.ma-btn:hover{border-color:var(--primary);color:var(--primary)}
.ma-btn.primary{background:var(--primary);border-color:var(--primary);color:#fff}
.ma-btn.primary:hover{background:var(--primary-deep);border-color:var(--primary-deep);color:#fff}
.ma-btn.danger{background:transparent;border-color:var(--danger);color:var(--danger)}
.ma-btn.danger:hover{background:var(--danger);border-color:var(--danger);color:#fff}
.ma-btn.ghost{background:transparent;color:var(--text-2)}
.ma-btn.ghost:hover{color:var(--primary);border-color:var(--primary)}
.edit-hint{margin-top:6px;font-size:11px;line-height:1.5;color:var(--muted)}
.sql-box{margin:8px 0 4px;background:var(--bg);border:1px solid var(--border-soft);border-radius:10px;overflow:hidden}
.sql-head{display:flex;align-items:center;justify-content:space-between;padding:7px 12px;cursor:pointer;font-size:12px;color:var(--text-2);user-select:none;gap:10px}
.sql-head:hover{color:var(--primary)}
.sql-title{font-weight:600;color:var(--text)}
.sql-head-sub{font-size:11px;color:var(--muted);font-family:ui-monospace,Menlo,monospace;flex:1;text-align:left;margin-left:8px}
.sql-toggle{font-size:11px;color:var(--muted);flex:none}
.sql-body{padding:8px 12px 10px;border-top:1px dashed var(--border-soft)}
.sql-actions{display:flex;justify-content:flex-end;margin-bottom:6px}
.sql-btn{font-size:11px;color:var(--primary);background:#EAF1FF;border:1px solid #EAF1FF;border-radius:6px;padding:2px 10px;cursor:pointer}
.sql-btn:hover{filter:brightness(0.97)}
.sql-pre{margin:0;max-height:320px;overflow:auto;background:#0d1117;color:#c9d1d9;border-radius:8px;padding:10px 12px;font-size:11.5px;line-height:1.55;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
.sql-caliber{margin-top:6px;font-size:11.5px;line-height:1.6;color:#7d8590;background:#f6f8fa;border:1px solid var(--border-soft);border-left:3px solid var(--primary);border-radius:6px;padding:6px 10px}
.sql-caliber .sc-label{display:inline-block;font-weight:600;color:var(--primary);margin-right:6px}
.sql-caliber .sc-more{margin-top:5px}
.sql-caliber .sc-more>summary{cursor:pointer;color:var(--primary);font-weight:600;outline:none}
.sql-caliber .sc-ver{font-weight:400;color:#8a9099}
.sql-caliber .sc-detail{margin-top:4px;display:grid;gap:3px}
.sql-caliber .sc-row{display:flex;gap:6px;align-items:flex-start}
.sql-caliber .sc-k{flex:0 0 58px;color:#8a9099}
.sql-caliber .sc-v{flex:1;min-width:0;word-break:break-word;color:var(--text-2)}
.sql-err{margin-top:6px;font-size:11px;color:#f53f3f}
.sql-cached{display:inline-block;font-weight:600;color:#d97706;background:#fff7e6;border:1px solid #ffd591;border-radius:4px;padding:0 5px;font-size:11px;line-height:1.5}
</style>
