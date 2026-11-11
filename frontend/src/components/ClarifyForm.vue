<template>
  <div class="clarify-card" :class="{ done: submitted }">
    <!-- ============ 空数据时间窗重定向确认（FR5） ============ -->
    <template v-if="redirect">
      <div class="cf-head">
        <span class="cf-ico cf-ico-warn">!</span>
        <div class="cf-title-wrap">
          <div class="cf-title">{{ payload.title || '该时间段暂无数据' }}</div>
          <div class="cf-sub">{{ payload.problems && payload.problems[0] }}</div>
        </div>
      </div>

      <!-- 已提交：只读摘要 -->
      <div v-if="submitted" class="cf-summary">
        <span class="cf-tag"><b>改查</b>{{ payload.adjusted_label }}</span>
        <span class="cf-summary-note">已确认，正在按最新可得数据查询…</span>
      </div>
      <div v-else-if="dismissed" class="cf-summary">
        <span class="cf-summary-note cf-muted">已取消重查，可更换条件后重新提问</span>
      </div>

      <template v-else>
        <div class="rt-rows">
          <div class="rt-row">
            <span class="rt-k">查询窗口</span>
            <span class="rt-v">{{ fmtWindow(payload.original) }}</span>
          </div>
          <div class="rt-row">
            <span class="rt-k">数据截至</span>
            <span class="rt-v rt-asof">{{ payload.data_as_of }}</span>
          </div>
          <div class="rt-row rt-row-hl">
            <span class="rt-k">改查窗口</span>
            <span class="rt-v">{{ payload.adjusted_label }}</span>
          </div>
        </div>
        <div class="cf-actions">
          <button class="cf-submit" :disabled="store.sending" @click="submitRedirect">
            {{ payload.submit_label || '查询最新数据' }}
          </button>
          <button class="cf-cancel" :disabled="store.sending" @click="dismissed = true">
            {{ payload.cancel_label || '暂不重查' }}
          </button>
        </div>
      </template>
    </template>

    <!-- ============ 常规要素确认表单 ============ -->
    <template v-else>
    <div class="cf-head">
      <span class="cf-ico">?</span>
      <div class="cf-title-wrap">
        <div class="cf-title">{{ payload.title || '请确认查询要素' }}</div>
        <div v-if="problems.length" class="cf-sub">需要补充：{{ problems.join('；') }}</div>
      </div>
      <span v-if="payload.rank_by === 'confidence'" class="cf-rank" title="候选按与问题的匹配置信度从高到低排序">
        按置信度排序
      </span>
    </div>

    <!-- 已提交：只读确认摘要 -->
    <div v-if="submitted" class="cf-summary">
      <span v-for="(it, i) in confirmItems" :key="i" class="cf-tag">
        <b>{{ it.label }}</b>{{ it.value || '不限' }}
      </span>
      <span class="cf-summary-note">已确认，正在继续执行后续步骤…</span>
    </div>

    <template v-else>
      <div v-for="f in fields" :key="f.key" class="cf-field">
        <div class="cf-label-row">
          <span class="cf-label">{{ f.label }}</span>
          <span v-if="f.required" class="cf-req">必选</span>
          <span v-if="f.hint" class="cf-hint">{{ f.hint }}</span>
        </div>

        <div class="cf-options">
          <button
            v-for="opt in f.options"
            :key="opt.value"
            class="cf-opt"
            :class="{ on: isOn(f, opt.value) }"
            :title="opt.desc || opt.value"
            @click="pick(f, opt.value)"
          >
            <span class="cf-opt-text">{{ opt.label }}</span>
            <span v-if="opt.score != null" class="cf-opt-score" :class="scoreCls(opt.score)">
              {{ pct(opt.score) }}
            </span>
          </button>
        </div>

        <!-- 被剔除的不支持项：显式红字告知，避免用户以为已按原维度提交 -->
        <div v-if="f.invalid && f.invalid.length" class="cf-invalid">
          ⚠ 已忽略不被支持的{{ fieldName(f) }}：{{ f.invalid.join('、') }}，请从上方候选中改选
        </div>

        <!-- 自定义（选项外的值）：作为可移除标签展示 -->
        <div v-if="customValues(f).length" class="cf-custom-tags">
          <span v-for="v in customValues(f)" :key="v" class="cf-custom-tag">
            {{ v }}
            <i class="cf-x" title="移除" @click="removeValue(f, v)">×</i>
          </span>
        </div>

        <div v-if="f.allow_custom" class="cf-custom">
          <input
            v-model="custom[f.key]"
            class="cf-input"
            :placeholder="customPlaceholder(f)"
            @keydown.enter.prevent="addCustom(f)"
          />
          <button class="cf-add" @click="addCustom(f)">添加</button>
        </div>
      </div>

      <div class="cf-field cf-extra">
        <input v-model="extra" class="cf-input" :placeholder="extraPlaceholder" />
      </div>

      <div class="cf-actions">
        <button class="cf-submit" :disabled="!canSubmit" @click="submit">
          {{ payload.submit_label || '确认并继续' }}
        </button>
        <span v-if="!canSubmit" class="cf-tip">{{ tip }}</span>
        <span v-else class="cf-tip">提交后系统将按确认的要素直接取数，无需重复描述</span>
      </div>
    </template>
    </template>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { buildClarifySelection, store, submitClarification } from '../store'

const props = defineProps({ msg: { type: Object, required: true } })

const payload = computed(() => props.msg.clarify || {})
const redirect = computed(() => payload.value.type === 'clarify_time_redirect')
const fields = computed(() => payload.value.fields || [])
const problems = computed(() => payload.value.problems || [])
const submitted = computed(() => !!props.msg.clarifySubmitted)
/* 「暂不重查」只收起卡片，不发任何请求（不重查是用户的自由，不必落库） */
const dismissed = ref(false)
const extraPlaceholder = computed(
  () => (payload.value.extra && payload.value.extra.placeholder) || '补充说明（可选）'
)

function fmtWindow(w) {
  if (!w || (!w.start && !w.end)) return '不限'
  return `${w.start || '不限'} ~ ${w.end || '不限'}`
}
function submitRedirect() {
  if (store.sending) return
  const p = payload.value
  const selection = {
    time_redirect: 'latest',
    time_range: p.adjusted,
    context: p.context || {},
    _labels: { time_range: p.adjusted_label }
  }
  submitClarification(props.msg, selection)
}
const confirmItems = computed(() => {
  const c = props.msg.clarifyConfirm || {}
  return [
    { label: '指标', value: (c.metrics || []).join('、') },
    { label: '维度', value: (c.dimensions || []).join('、') },
    { label: '时间', value: c.time_range || '' },
    { label: '补充', value: c.extra || '' }
  ].filter((x) => x.value)
})

/* 表单选择状态：以载荷里的 selected（系统已识别值）为初值，用户改选即覆盖 */
const answers = reactive({})
const custom = reactive({})
const extra = ref('')

function init() {
  const c = payload.value
  for (const key of Object.keys(answers)) delete answers[key]
  for (const key of Object.keys(custom)) delete custom[key]
  for (const f of c.fields || []) {
    answers[f.key] = Array.isArray(f.selected) ? [...f.selected] : []
    custom[f.key] = ''
  }
  extra.value = ''
}
watch(payload, init, { immediate: true })

function isOn(f, value) {
  return (answers[f.key] || []).includes(value)
}
function optionValues(f) {
  return (f.options || []).map((o) => o.value)
}
function customValues(f) {
  const known = optionValues(f)
  return (answers[f.key] || []).filter((v) => !known.includes(v))
}
function pick(f, value) {
  const cur = answers[f.key] || []
  if (f.multiple) {
    answers[f.key] = cur.includes(value) ? cur.filter((v) => v !== value) : [...cur, value]
  } else {
    answers[f.key] = cur.includes(value) ? [] : [value]
  }
}
function addCustom(f) {
  const v = (custom[f.key] || '').trim()
  if (!v) return
  const cur = answers[f.key] || []
  if (f.multiple) {
    if (!cur.includes(v)) answers[f.key] = [...cur, v]
  } else {
    answers[f.key] = [v]
  }
  custom[f.key] = ''
}
function removeValue(f, value) {
  answers[f.key] = (answers[f.key] || []).filter((v) => v !== value)
}
function customPlaceholder(f) {
  return `其它${fieldName(f)}，回车添加`
}
function fieldName(f) {
  return (f.label || '').replace(/（.*）/, '')
}

const missingRequired = computed(() =>
  fields.value.filter((f) => f.required && !(answers[f.key] || []).length)
)
const canSubmit = computed(() => !submitted.value && !store.sending && missingRequired.value.length === 0)
const tip = computed(() =>
  missingRequired.value.length
    ? `请先选择${missingRequired.value.map((f) => f.label).join('、')}`
    : '正在生成回答……'
)

function pct(score) {
  return `${Math.round(Number(score) * 100)}%`
}
function scoreCls(score) {
  const s = Number(score)
  if (s >= 0.99) return 'hi'
  if (s >= 0.5) return 'mid'
  return 'lo'
}
function submit() {
  if (!canSubmit.value) return
  const selection = buildClarifySelection(payload.value, answers, extra.value)
  submitClarification(props.msg, selection)
}
</script>

<style scoped>
.clarify-card {
  margin: 8px 0 4px;
  background: var(--card);
  border: 1px solid var(--border);
  border-left: 3px solid var(--primary);
  border-radius: 12px;
  padding: 12px 14px;
  box-shadow: var(--shadow);
}
.clarify-card.done { border-left-color: #00b42a; }

/* 头部 */
.cf-head { display: flex; align-items: flex-start; gap: 8px; }
.cf-ico {
  flex: none; width: 20px; height: 20px; border-radius: 50%;
  background: var(--primary-soft); color: var(--primary);
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; margin-top: 1px;
}
.cf-title-wrap { flex: 1; min-width: 0; }
.cf-title { font-size: 13px; font-weight: 600; color: var(--text); }
.cf-sub { font-size: 12px; color: var(--text-2); margin-top: 2px; line-height: 1.6; }
.cf-rank {
  flex: none; font-size: 10px; color: var(--primary);
  background: var(--primary-soft); border-radius: 6px; padding: 1px 7px; line-height: 16px;
}

/* 字段 */
.cf-field { margin-top: 10px; }
.cf-label-row { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; margin-bottom: 5px; }
.cf-label { font-size: 12px; font-weight: 600; color: var(--text-2); }
.cf-req { font-size: 10px; color: #d93026; background: #fef1f1; border-radius: 5px; padding: 0 5px; line-height: 15px; }
.cf-hint { font-size: 11px; color: var(--muted); }

.cf-options { display: flex; flex-wrap: wrap; gap: 6px; }
.cf-opt {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 12px; color: var(--text-2);
  background: var(--bg); border: 1px solid var(--border);
  border-radius: 8px; padding: 4px 10px; cursor: pointer;
  transition: all .14s; line-height: 18px; font-family: inherit;
}
.cf-opt:hover { border-color: var(--primary); color: var(--primary); }
.cf-opt.on { background: var(--primary-soft); border-color: var(--primary); color: var(--primary); font-weight: 500; }
.cf-opt-text { white-space: nowrap; }
.cf-opt-score {
  font-size: 10px; font-family: ui-monospace, Menlo, monospace;
  border-radius: 5px; padding: 0 4px; line-height: 14px;
  background: var(--border-soft); color: var(--muted);
}
.cf-opt.on .cf-opt-score.hi { background: #e6f6ec; color: #0f7a3d; }
.cf-opt-score.hi { background: #e6f6ec; color: #0f7a3d; }
.cf-opt-score.mid { background: #fff5e6; color: #ad6800; }
.cf-opt-score.lo { background: var(--border-soft); color: var(--muted); }

.cf-invalid { margin-top: 6px; font-size: 11px; color: #d93026; line-height: 1.6; }

.cf-custom-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.cf-custom-tag {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 12px; color: var(--primary); background: var(--primary-soft);
  border: 1px dashed var(--primary); border-radius: 8px; padding: 3px 8px;
}
.cf-x { font-style: normal; cursor: pointer; color: var(--muted); font-size: 13px; line-height: 1; }
.cf-x:hover { color: #d93026; }

.cf-custom { display: flex; gap: 6px; margin-top: 6px; }
.cf-input {
  flex: 1; min-width: 0; height: 30px; padding: 0 10px;
  font-size: 12px; color: var(--text); font-family: inherit;
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px; outline: none;
}
.cf-input:focus { border-color: var(--primary); background: var(--card); }
.cf-input::placeholder { color: var(--muted); }
.cf-add {
  flex: none; font-size: 12px; color: var(--text-2);
  background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; padding: 0 12px; cursor: pointer; font-family: inherit;
}
.cf-add:hover { border-color: var(--primary); color: var(--primary); }
.cf-extra { margin-top: 10px; }

/* 操作区 */
.cf-actions { display: flex; align-items: center; gap: 10px; margin-top: 12px; }
.cf-submit {
  font-size: 13px; font-weight: 500; color: #fff; background: var(--primary);
  border: none; border-radius: 8px; padding: 7px 18px; cursor: pointer; font-family: inherit;
  transition: background .14s;
}
.cf-submit:hover:not(:disabled) { background: var(--primary-deep); }
.cf-submit:disabled { background: #c9d4e4; cursor: not-allowed; }
.cf-tip { font-size: 11px; color: var(--muted); }

/* 已提交态 */
.cf-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 10px; }
.cf-tag {
  font-size: 12px; color: var(--text-2); background: var(--bg);
  border: 1px solid var(--border); border-radius: 8px; padding: 3px 9px;
}
.cf-tag b { color: var(--muted); font-weight: 500; margin-right: 4px; }
.cf-summary-note { font-size: 11px; color: #00a325; }
.cf-summary-note.cf-muted { color: var(--muted); }

/* 空数据时间窗重定向卡片 */
.cf-ico-warn { background: #fff5e6; color: #ad6800; }
.rt-rows { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
.rt-row { display: flex; align-items: baseline; gap: 10px; font-size: 12px; }
.rt-k { flex: none; width: 56px; color: var(--muted); }
.rt-v { color: var(--text-2); }
.rt-asof { color: #ad6800; font-weight: 600; }
.rt-row-hl .rt-v { color: var(--primary); font-weight: 600; }
.rt-row-hl {
  margin-top: 2px; padding: 6px 10px; border-radius: 8px;
  background: var(--primary-soft);
}
.cf-cancel {
  font-size: 13px; color: var(--text-2); background: var(--card);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 7px 14px; cursor: pointer; font-family: inherit; transition: all .14s;
}
.cf-cancel:hover:not(:disabled) { border-color: var(--muted); color: var(--text); }
.cf-cancel:disabled { cursor: not-allowed; opacity: .6; }
</style>
