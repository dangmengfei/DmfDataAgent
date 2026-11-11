<template>
  <footer class="inputbar">
    <div class="input-inner">
      <div class="input-box">
        <textarea
          id="chatInput"
          ref="taRef"
          rows="1"
          :placeholder="placeholder"
          v-model="text"
          @input="autoGrow"
          @keydown="onKeydown"
        ></textarea>
        <div class="input-tools">
          <button class="tool-chip" :style="chipStyle" title="后端组件状态（点击刷新）" @click="refreshHealth">
            <el-icon><TrendCharts /></el-icon>
            <span>{{ backendLabel }}</span>
          </button>
          <span class="input-hint">Enter 发送 · Shift + Enter 换行</span>
          <ContextRing />
          <el-button
            v-if="store.sending"
            class="send-btn stop"
            type="danger"
            circle
            title="停止生成（保留已生成内容）"
            @click="onStop"
          >
            <el-icon class="stop-ico"><VideoPause /></el-icon>
          </el-button>
          <el-button v-else class="send-btn" type="primary" circle :disabled="!canSend" @click="doSend">
            <el-icon><Promotion /></el-icon>
          </el-button>
        </div>
      </div>
      <div class="disclaimer">内容由 AI 生成，仅供参考 · 指标口径以数据平台为准</div>
    </div>
  </footer>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Promotion, TrendCharts, VideoPause } from '@element-plus/icons-vue'
import { store, send, stopStreaming, refreshHealth } from '../store'
import ContextRing from './ContextRing.vue'

const text = ref('')
const taRef = ref(null)

function autoGrow(){
  const ta = taRef.value
  if(!ta) return
  ta.style.height = 'auto'
  ta.style.height = Math.min(ta.scrollHeight, 160) + 'px'
}
function onKeydown(e){
  // 中文输入法组合期间（候选词确认的 Enter）不发送：
  // isComposing 为标准判定；keyCode 229 兜底覆盖部分浏览器/旧输入法不置标记的情况
  if(e.isComposing || e.keyCode === 229) return
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); doSend() }
}
function doSend(){
  if(!text.value.trim() || store.sending) return
  const q = text.value
  text.value = ''
  autoGrow()
  send(q)
}
function onStop(){
  if(store.sending) stopStreaming()
}
function setAndSend(q){
  text.value = q
  autoGrow()
  doSend()
}
/* 未落库错误（如 E5005 会话忙）时还原输入框草稿的回调，供 store 撤回占位消息后调用 */
function restoreDraft(q){
  text.value = q
  autoGrow()
  taRef.value && taRef.value.focus()
}
defineExpose({ setAndSend, restoreDraft })

onMounted(() => {
  autoGrow()
  store.restoreDraft = restoreDraft
})
onUnmounted(() => {
  if (store.restoreDraft === restoreDraft) store.restoreDraft = null
})

const backendLabel = computed(() => {
  if(!store.health.ok) return '服务未连接'
  const c = store.health.components || {}
  return `DB ${c.database || '-'} · RAG ${c.rag || '-'}`
})
const chipStyle = computed(() => (store.health.ok ? { color: '#00a325' } : { color: '#d93026' }))
const canSend = computed(() => text.value.trim().length > 0 && !store.sending)
const placeholder = '输入指标问题，例如：各分行一季度存款余额排名'
</script>

<style scoped>
.send-btn{width:34px;height:34px;margin-left:6px}
.send-btn .el-icon{font-size:16px}
.send-btn.stop .stop-ico{font-size:14px;color:#fff}
/* 提示文字吸收剩余空间，把圆环+发送键整组推到右端（圆环紧挨发送键左侧） */
.input-hint{margin-right:auto}
</style>
