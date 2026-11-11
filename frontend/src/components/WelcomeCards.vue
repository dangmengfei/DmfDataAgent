<template>
  <div class="welcome">
    <div class="w-logo">DA</div>
    <div class="w-title">你好，我是指标问答助手</div>
    <div class="w-sub">用自然语言查询经营指标，支持多维分析、同环比与归因解读。输入问题即可开始，也可以点击下方示例快速体验。</div>
    <div class="w-grid">
      <div v-for="c in cards" :key="c.q" class="w-card" @click="$emit('pick', c.q)">
        <div class="wc-t">{{ c.t }}</div>
        <div class="wc-d">{{ c.q }}</div>
      </div>
    </div>
  </div>
</template>

<script setup>
const cards = [
  { t: '📊 指标排名', q: '各分行上季度零售存款余额排名' },
  { t: '📈 同环比分析', q: '近半年零售贷款余额月度趋势与环比' },
  { t: '🔍 多维下钻', q: '按客群看存款利息支出构成' },
  { t: '🧭 归因诊断', q: '上月不良贷款率环比上升的主要原因' }
]
defineEmits(['pick'])
</script>
