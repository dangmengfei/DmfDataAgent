<template>
  <div id="messages">
    <MessageItem
      v-for="(m, i) in store.messages"
      :key="i"
      :msg="m"
      :is-last="i === store.messages.length - 1"
    />
  </div>
</template>

<script setup>
import { watch, nextTick } from 'vue'
import MessageItem from './MessageItem.vue'
import { store } from '../store'

const chat = () => document.querySelector('.chat')

async function scrollToBottom(smooth = false) {
  await nextTick()
  const el = chat()
  if (!el) return
  if (smooth) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  else el.scrollTop = el.scrollHeight
}

watch(() => store.messages.length, () => scrollToBottom())

/* 反问澄清卡片是在流结束后才挂到消息上的，此时消息条数未变，
 * 若不额外滚动，用户看不到需要确认的表单（曾被真实截图暴露）。 */
watch(
  () => {
    const m = store.messages[store.messages.length - 1]
    return m && m.clarify ? `${m.clarify.original_query || ''}#${(m.clarify.fields || []).length}` : ''
  },
  (v) => { if (v) scrollToBottom(true) }
)
</script>
