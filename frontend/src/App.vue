<template>
  <el-config-provider :locale="zhCn">
    <!-- 未登录：整个界面替换为登录页 -->
    <LoginView v-if="!store.auth.user" />

    <!-- 已登录：主应用 -->
    <div v-else class="app">
      <!-- 左侧边栏 -->
      <SessionSidebar />

      <!-- 主区域 -->
      <main class="main">
        <template v-if="store.view === 'chat'">
          <ChatHeader @open-obs="openChatObs" />
          <section class="chat">
            <div class="chat-inner">
              <WelcomeCards v-if="store.messages.length === 0" @pick="onPick" />
              <MessageList />
            </div>
          </section>
          <ChatInput ref="inputRef" />
        </template>
        <KnowledgeBase v-else-if="store.view === 'kb'" />
        <PluginsPanel v-else-if="store.view === 'plugins'" />
        <ModelsPanel v-else-if="store.view === 'models'" />
        <PromptManager v-else-if="store.view === 'prompts'" />
        <Observability v-else-if="store.view === 'obs'" />
        <SemanticExplorer v-else-if="store.view === 'sem'" />
        <SemanticRegistry v-else-if="store.view === 'registry'" />
        <AuditDashboard v-else-if="store.view === 'audit'" />
        <KnowledgeGraph v-else-if="store.view === 'kbGraph'" />
        <UserPreferences v-else-if="store.view === 'prefs'" />
        <MemoryManager v-else-if="store.view === 'memory'" />
        <FewShotManager v-else-if="store.view === 'fewshots'" />
        <SlangManager v-else-if="store.view === 'slang'" />
        <SlangMetrics v-else-if="store.view === 'slangMetrics'" />
        <UserManagement v-else-if="store.view === 'users'" />
      </main>

      <!-- 可观测面板 -->
      <ObsPanel v-model="store.obsVisible" />
    </div>
  </el-config-provider>
</template>

<script setup>
import { ref } from 'vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import SessionSidebar from './components/SessionSidebar.vue'
import ChatHeader from './components/ChatHeader.vue'
import WelcomeCards from './components/WelcomeCards.vue'
import MessageList from './components/MessageList.vue'
import ChatInput from './components/ChatInput.vue'
import ObsPanel from './components/ObsPanel.vue'
import Observability from './components/Observability.vue'
import KnowledgeBase from './components/KnowledgeBase.vue'
import PluginsPanel from './components/PluginsPanel.vue'
import ModelsPanel from './components/ModelsPanel.vue'
import PromptManager from './components/PromptManager.vue'
import SemanticExplorer from './components/SemanticExplorer.vue'
import SemanticRegistry from './components/SemanticRegistry.vue'
import AuditDashboard from './components/AuditDashboard.vue'
import KnowledgeGraph from './components/KnowledgeGraph.vue'
import UserPreferences from './components/UserPreferences.vue'
import MemoryManager from './components/MemoryManager.vue'
import FewShotManager from './components/FewShotManager.vue'
import SlangManager from './components/SlangManager.vue'
import SlangMetrics from './components/SlangMetrics.vue'
import UserManagement from './components/UserManagement.vue'
import LoginView from './components/LoginView.vue'
import { store } from './store'

const inputRef = ref(null)
function onPick(q){
  inputRef.value && inputRef.value.setAndSend(q)
}
/* 对话上方「观测」按钮：展示当前对话的观测内容（无选中会话时退化为全局） */
function openChatObs(){
  store.obsSessionId = store.sessionId || null
  store.obsVisible = true
}
</script>
