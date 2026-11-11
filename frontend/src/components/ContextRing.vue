<template>
  <el-tooltip v-if="ctx.memory_enabled" :content="tip" placement="top">
    <button
      class="ctx-ring"
      type="button"
      :class="{ 'is-disabled': !ctx.compressible || store.contextCompressing }"
      :aria-label="tip"
      @click="onClick"
    >
      <svg :width="size" :height="size" viewBox="0 0 36 36">
        <circle class="ring-track" cx="18" cy="18" r="15.5" />
        <circle
          class="ring-value"
          cx="18" cy="18" r="15.5"
          :stroke="stroke"
          :stroke-dasharray="circumference"
          :stroke-dashoffset="dashOffset"
        />
      </svg>
      <span v-if="store.contextCompressing" class="ring-center ring-spin">…</span>
      <span v-else class="ring-center">{{ percentLabel }}</span>
    </button>
  </el-tooltip>
</template>

<script setup>
import { computed } from 'vue'
import { ElMessage } from 'element-plus'
import { compressContext, store } from '../store'

const size = 26
const circumference = 2 * Math.PI * 15.5

const ctx = computed(() => store.context || {})
const ratio = computed(() => Math.max(0, Math.min(1, Number(ctx.value.ratio) || 0)))
const dashOffset = computed(() => circumference * (1 - ratio.value))
const percentLabel = computed(() => Math.round(ratio.value * 100) + '%')
function fmtTokens(n) {
  const v = Number(n) || 0
  return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'k' : String(v)
}
const stroke = computed(() => {
  if (ratio.value >= 1) return '#f56c6c' // 超窗口：红
  if (ratio.value >= 0.8) return '#e6a23c' // 接近：橙
  if (ratio.value >= 0.5) return '#409eff' // 过半：蓝
  return '#67c23a' // 充裕：绿
})
const tip = computed(() => {
  const c = ctx.value
  const size = `上下文 ${fmtTokens(c.used_tokens)} / ${fmtTokens(c.max_tokens)} tokens（${Math.round(
    (Number(c.ratio) || 0) * 100
  )}%，未压缩 ${c.uncovered_rounds}/${c.threshold} 轮，估算值）`
  if (c.compressible) return `${size} · 点击压缩为摘要（保留近 ${c.retain_rounds} 轮）`
  if (c.has_summary) return `${size}（已有摘要，暂无可压缩的更早轮次）`
  return `${size}（暂无可压缩的更早轮次）`
})

async function onClick() {
  if (!ctx.value.compressible || store.contextCompressing) return
  try {
    const res = await compressContext()
    if (!res) return
    if (res.compressed) {
      ElMessage.success(`已把前 ${res.rounds_compressed} 轮压缩为摘要，保留最近 ${res.retain_rounds} 轮`)
    } else if (res.reason === 'nothing_to_compress') {
      ElMessage.info('暂无可压缩的更早轮次')
    }
  } catch (e) {
    ElMessage.error('上下文压缩失败：' + (e && e.message ? e.message : '稍后重试'))
  }
}
</script>

<style scoped>
.ctx-ring{
  position:relative;width:26px;height:26px;padding:0;border:none;background:transparent;
  border-radius:50%;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;
}
.ctx-ring.is-disabled{cursor:default;opacity:.55}
.ctx-ring:not(.is-disabled):hover{background:var(--el-fill-color-light,#f5f7fa)}
.ring-track{fill:none;stroke:var(--el-fill-color,#e5e7eb);stroke-width:3}
.ring-value{
  fill:none;stroke-width:3;stroke-linecap:round;
  transform:rotate(-90deg);transform-origin:50% 50%;transition:stroke-dashoffset .3s ease,stroke .3s ease;
}
.ring-center{
  position:absolute;font-size:10px;line-height:1;color:var(--el-text-color-primary,#303133);
  font-variant-numeric:tabular-nums;pointer-events:none;
}
.ring-spin{animation:ctx-blink 1s infinite}
@keyframes ctx-blink{50%{opacity:.3}}
</style>
