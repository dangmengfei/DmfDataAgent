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
  // 帕累托（2026-11-18 P1）：柱（数值）+ 累计线（右轴 %），附 80% 阈值标记线
  if(spec.type === 'pareto'){
    const [main, cum] = spec.series || []
    const thresholdPct = spec.threshold != null ? spec.threshold * 100 : 80
    return {
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', confine: true },
      legend: { data: (spec.series || []).map(s => s.name), textStyle: { fontSize: 10, color: '#6B7280' } },
      grid: { left: 16, right: 44, top: 32, bottom: 6, containLabel: true },
      xAxis: {
        type: 'category',
        data: spec.x || [],
        axisLabel: { color: '#6B7280', fontSize: 11, rotate: (spec.x || []).length > 6 ? 30 : 0 },
        axisLine: { lineStyle: { color: '#E4E3DD' } }
      },
      yAxis: [
        { type: 'value', name: unit, axisLabel: { color: '#6B7280', fontSize: 11 }, splitLine: { lineStyle: { color: '#F0EFEA' } } },
        { type: 'value', name: '%', min: 0, max: 100, axisLabel: { color: '#6B7280', fontSize: 11, formatter: '{value}%' }, splitLine: { show: false } }
      ],
      series: [
        {
          name: (main && main.name) || '值', type: 'bar', data: (main && main.data) || [],
          barMaxWidth: 28, itemStyle: { borderRadius: [4, 4, 0, 0], color: '#8BC8EA' }
        },
        {
          name: (cum && cum.name) || '累计占比', type: 'line', yAxisIndex: 1,
          data: (cum && cum.data) || [], smooth: false,
          itemStyle: { color: '#C07A2B' }, lineStyle: { color: '#C07A2B' },
          markLine: {
            symbol: 'none', silent: true,
            lineStyle: { color: '#C0392B', type: 'dashed' },
            label: { formatter: thresholdPct + '%', fontSize: 10 },
            data: [{ yAxis: thresholdPct }]
          }
        }
      ]
    }
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
    // 异动检测离群点标记（analysis_anomaly：mark_points 为离群期序号）
    if(Array.isArray(s.mark_points) && s.mark_points.length){
      base.markPoint = {
        symbol: 'circle', symbolSize: 8,
        itemStyle: { color: '#C0392B' },
        label: { show: false },
        data: s.mark_points.filter(i => i >= 0 && i < (s.data || []).length).map(i => ({ coord: [String((spec.x || [])[i]), s.data[i]] }))
      }
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
