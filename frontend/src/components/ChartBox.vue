<template>
  <div class="viz-box">
    <div class="viz-title">{{ viz.title || '数据图表' }}</div>
    <div ref="el" class="viz-canvas"></div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
// ECharts 按需引入（tree-shaking）：仅注册 line/bar 图表与所需组件
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({ viz: { type: Object, required: true } })
const el = ref(null)
let chart = null

function buildOption(spec){
  const unit = spec.unit || ''
  const fmt = v => {
    if(v == null) return '-'
    const n = Number(v)
    return isFinite(n) ? n.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) + (unit ? ' ' + unit : '') : String(v)
  }
  const isBar = spec.type === 'bar'
  const series = (spec.series || []).map(s => {
    const base = { name: s.name, type: isBar ? 'bar' : 'line', data: s.data, smooth: !isBar, connectNulls: true }
    if(isBar){
      base.barMaxWidth = 32
      base.itemStyle = { borderRadius: [4, 4, 0, 0] }
      if(spec.positive_negative){
        base.itemStyle.color = p => (p.value >= 0 ? '#52C41A' : '#EA6668')
      } else {
        base.itemStyle.color = '#8BC8EA'
      }
      base.label = { show: true, position: 'top', fontSize: 10, color: '#6B7280', formatter: p => fmt(p.value) }
    }
    return base
  })
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: isBar ? 'axis' : 'axis',
      confine: true,
      backgroundColor: 'rgba(255,255,255,0.96)',
      borderColor: '#E4E3DD',
      textStyle: { color: '#333', fontSize: 12 },
      formatter: params => {
        const arr = Array.isArray(params) ? params : [params]
        let r = arr[0].axisValue + '<br/>'
        arr.forEach(p => { r += p.marker + p.seriesName + ': <b>' + fmt(p.value) + '</b><br/>' })
        return r
      }
    },
    grid: { left: 16, right: 20, top: 32, bottom: 6, containLabel: true },
    xAxis: {
      type: 'category',
      data: spec.x || [],
      axisLabel: { color: '#6B7280', fontSize: 11, rotate: (spec.x || []).length > 6 ? 30 : 0 },
      axisLine: { lineStyle: { color: '#E4E3DD' } }
    },
    yAxis: {
      type: 'value',
      name: unit,
      nameTextStyle: { color: '#9CA3AF', fontSize: 11 },
      axisLabel: { color: '#6B7280', fontSize: 11 },
      splitLine: { lineStyle: { color: '#F0EFEA' } }
    },
    series
  }
}

function render(){
  if(!el.value) return
  if(!chart) chart = echarts.init(el.value)
  chart.setOption(buildOption(props.viz), true)
}

function onResize(){ if(chart) chart.resize() }

onMounted(() => {
  render()
  window.addEventListener('resize', onResize)
})
watch(() => props.viz, () => render(), { deep: true })
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if(chart){ chart.dispose(); chart = null }
})
</script>

<style scoped>
.viz-box{
  margin-top: 10px;
  border: 1px solid #E4E3DD;
  border-radius: 12px;
  padding: 10px 12px 4px;
  background: #FBFBF9;
}
.viz-title{
  font-size: 13px;
  font-weight: 600;
  color: #1A1B1C;
  margin-bottom: 4px;
}
.viz-canvas{
  width: 100%;
  height: 260px;
}
</style>
