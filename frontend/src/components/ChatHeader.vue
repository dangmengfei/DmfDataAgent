<template>
  <header class="topbar">
    <h1>{{ title }}</h1>
    <span class="crumb">DmfDataAgent · 指标问答</span>
    <button class="status-pill-obs" @click="$emit('open-obs')">
      <el-icon><DataAnalysis /></el-icon>观测
    </button>
    <div class="status-pill" :class="{ ok: store.health.ok, err: !store.health.ok }">
      <span class="dot"></span>
      <span>{{ store.health.ok ? '服务就绪' : '服务未连接' }}</span>
    </div>
  </header>
</template>

<script setup>
import { computed } from 'vue'
import { DataAnalysis } from '@element-plus/icons-vue'
import { store, sessionTitle } from '../store'

const title = computed(() => sessionTitle())
defineEmits(['open-obs'])
</script>

<style scoped>
.status-pill-obs{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-2);background:var(--bg);border:1px solid var(--border);padding:5px 12px;border-radius:20px;cursor:pointer;transition:all .15s}
.status-pill-obs:hover{color:var(--primary);border-color:var(--primary);background:var(--primary-soft)}
.status-pill-obs .el-icon{font-size:13px}
</style>
