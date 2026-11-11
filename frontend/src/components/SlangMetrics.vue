<template>
  <div class="sl-page">
    <div class="sl-head">
      <div>
        <h2 class="sl-title">改写命中率 / 误改写率看板</h2>
        <div class="sl-sub">
          统计查询改写节点的实际表现：<b>命中率</b> = 有命中的改写次数 / 总改写次数；
          <b>误改写率</b> = 反馈「误改写」数 / 已反馈总数（对话中改写徽标 👍/👎 回写）。
          数据窗口可切换，近 {{ days }} 天。
        </div>
      </div>
      <div class="sl-head-actions">
        <el-select v-model="days" class="sl-select" @change="reload">
          <el-option :value="7" label="近 7 天" />
          <el-option :value="30" label="近 30 天" />
          <el-option :value="90" label="近 90 天" />
        </el-select>
        <el-button :loading="loading" @click="reload">刷新</el-button>
      </div>
    </div>

    <div v-if="!metrics" class="sl-empty">加载中或暂无数据…</div>
    <template v-else>
      <div class="sl-stats">
        <div class="sl-stat">
          <div class="sls-label">总改写次数</div>
          <div class="sls-value">{{ metrics.total_attempts }}</div>
          <div class="sls-hint">近 {{ metrics.window_days }} 天改写节点调用</div>
        </div>
        <div class="sl-stat">
          <div class="sls-label">命中率</div>
          <div class="sls-value" :style="{ color: hitRateColor }">{{ pct(metrics.hit_rate) }}%</div>
          <div class="sls-hint">有命中的改写占比</div>
        </div>
        <div class="sl-stat">
          <div class="sls-label">误改写率</div>
          <div class="sls-value" :style="{ color: misRateColor }">{{ pct(metrics.mis_rate) }}%</div>
          <div class="sls-hint">已反馈 {{ metrics.total_feedback }} 条</div>
        </div>
        <div class="sl-stat">
          <div class="sls-label">命中词次数</div>
          <div class="sls-value">{{ attemptsWithHit }}</div>
          <div class="sls-hint">👍 {{ metrics.good }} / 👎 {{ metrics.bad }}</div>
        </div>
      </div>

      <div class="sl-charts">
        <div class="sl-chart-card">
          <ChartBox v-if="byTypeViz" :viz="byTypeViz" />
          <div v-else class="sl-empty">暂无命中分布数据</div>
        </div>
        <div class="sl-chart-card">
          <ChartBox v-if="byScopeViz" :viz="byScopeViz" />
          <div v-else class="sl-empty">暂无命中分布数据</div>
        </div>
      </div>

      <h3 class="sl-section-title">命中 Top 词条</h3>
      <el-table :data="metrics.top_hits" class="sl-table" size="small" border stripe>
        <el-table-column prop="term" label="黑话 / 口语" min-width="140">
          <template #default="{ row }"><span class="sl-term">{{ row.term }}</span></template>
        </el-table-column>
        <el-table-column prop="standard" label="标准名" min-width="140" />
        <el-table-column label="类型" width="110">
          <template #default="{ row }">{{ slangTypeLabel(row.entry_type) }}</template>
        </el-table-column>
        <el-table-column label="作用域" width="100">
          <template #default="{ row }">{{ slangScopeLabel(row.scope) }}</template>
        </el-table-column>
        <el-table-column prop="hit_count" label="命中次数" width="100" align="right" />
      </el-table>
      <div v-if="!metrics.top_hits.length" class="sl-empty">近 {{ days }} 天暂无词条命中记录</div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import ChartBox from './ChartBox.vue'
import { store, slangTypeLabel, slangScopeLabel, loadSlangMetrics } from '../store'

const days = ref(30)
const metrics = computed(() => store.slangPage.metrics)
const loading = ref(false)
const attemptsWithHit = computed(() => metrics.value?.attempts_with_hit ?? 0)

function pct(v) {
  const n = Number(v || 0) * 100
  return n.toFixed(n % 1 === 0 ? 0 : 2)
}
const hitRateColor = computed(() => {
  const r = Number(metrics.value?.hit_rate || 0)
  return r >= 0.6 ? '#52C41A' : r >= 0.3 ? '#E6A23C' : '#EA6668'
})
const misRateColor = computed(() => {
  const r = Number(metrics.value?.mis_rate || 0)
  return r === 0 ? '#52C41A' : r <= 0.1 ? '#E6A23C' : '#EA6668'
})

const byTypeViz = computed(() => {
  const m = metrics.value
  if (!m || !m.by_type_hits) return null
  const entries = Object.entries(m.by_type_hits)
  if (!entries.length) return null
  return {
    title: '按类型命中分布',
    type: 'bar',
    x: entries.map(([k]) => slangTypeLabel(k)),
    series: [{ name: '命中数', data: entries.map(([, v]) => v) }]
  }
})
const byScopeViz = computed(() => {
  const m = metrics.value
  if (!m || !m.by_scope_hits) return null
  const entries = Object.entries(m.by_scope_hits)
  if (!entries.length) return null
  return {
    title: '按作用域命中分布',
    type: 'bar',
    x: entries.map(([k]) => slangScopeLabel(k)),
    series: [{ name: '命中数', data: entries.map(([, v]) => v) }]
  }
})

async function reload() {
  loading.value = true
  try {
    await loadSlangMetrics(days.value)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* 视图页根容器必须是滚动容器（body overflow:hidden + .main 不可滚） */
.sl-page {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  position: relative;
  padding: 18px 22px 28px;
  box-sizing: border-box;
}
.sl-page > * { flex: none; }
.sl-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.sl-title { margin: 0; font-size: 18px; font-weight: 600; color: #1f2329; }
.sl-sub { font-size: 12px; color: #8a9099; margin-top: 4px; max-width: 820px; line-height: 1.7; }
.sl-sub b { color: #5a6068; }
.sl-head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sl-select { width: 130px; }

.sl-stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0 4px; }
.sl-stat {
  flex: 1 1 150px; min-width: 130px; background: var(--card, #fff);
  border: 1px solid var(--border, #e8eaee); border-radius: 10px; padding: 12px 14px;
}
.sls-label { font-size: 12px; color: #8a9099; }
.sls-value { font-size: 22px; font-weight: 600; color: #1f2329; margin: 4px 0 2px; }
.sls-hint { font-size: 11px; color: #a8adb5; }

.sl-charts { display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0 8px; }
.sl-chart-card { flex: 1 1 360px; min-width: 320px; }
.sl-section-title { font-size: 15px; font-weight: 600; color: #1f2329; margin: 18px 0 8px; }
.sl-table { width: 100%; }
.sl-term { font-weight: 500; color: #1f2329; }
.sl-empty { color: #8a9099; font-size: 13px; padding: 16px 0; line-height: 1.6; }
</style>
