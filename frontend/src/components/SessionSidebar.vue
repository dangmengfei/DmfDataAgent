<template>
  <aside class="sidebar">
    <div class="side-logo">
      <div class="logo-box">DA</div>
      <div>
        <div class="logo-name">DmfDataAgent</div>
        <div class="logo-sub">指标问答 Agent</div>
      </div>
    </div>
    <el-button class="new-chat" type="primary" @click="onNewChat">
      <el-icon><Plus /></el-icon>新建对话
    </el-button>
    <div class="side-nav">
      <!-- 主入口 -->
      <div class="nav-item" :class="{ on: store.view === 'chat' }" @click="setView('chat')">
        <el-icon><ChatDotRound /></el-icon><span>对话</span>
      </div>
      <div v-if="can('kb')" class="nav-item" :class="{ on: store.view === 'kb' }" @click="setView('kb')">
        <el-icon><Collection /></el-icon><span>知识库</span>
      </div>
      <div v-if="can('models')" class="nav-item" :class="{ on: store.view === 'models' }" @click="setView('models')">
        <el-icon><Cpu /></el-icon><span>模型管理</span>
      </div>

      <!-- 提示词与插件：提示词管理 + Skill / MCP -->
      <div v-if="configVisible" class="nav-group" :class="{ open: configOpen }">
        <div class="nav-item nav-group-head" @click="configOpen = !configOpen">
          <el-icon><Setting /></el-icon><span>提示词与插件</span>
          <el-icon class="nav-arrow"><ArrowDown /></el-icon>
        </div>
        <div v-show="configOpen" class="nav-sub">
          <div v-if="can('prompts')" class="nav-item" :class="{ on: store.view === 'prompts' }" @click="setView('prompts')">
            <el-icon><Document /></el-icon><span>提示词管理</span>
          </div>
          <div v-if="can('plugins')" class="nav-item" :class="{ on: store.view === 'plugins' }" @click="setView('plugins')">
            <el-icon><Grid /></el-icon><span>Skill / MCP</span>
          </div>
        </div>
      </div>

      <!-- 观测与审计：运行观测 + 审计看板 -->
      <div v-if="obsVisible" class="nav-group" :class="{ open: obsOpen }">
        <div class="nav-item nav-group-head" @click="obsOpen = !obsOpen">
          <el-icon><DataLine /></el-icon><span>观测与审计</span>
          <el-icon class="nav-arrow"><ArrowDown /></el-icon>
        </div>
        <div v-show="obsOpen" class="nav-sub">
          <div v-if="can('obs')" class="nav-item" :class="{ on: store.view === 'obs' }" @click="onOpenObservability">
            <el-icon><Monitor /></el-icon><span>运行观测</span>
          </div>
          <div v-if="admin" class="nav-item" :class="{ on: store.view === 'audit' }" @click="setView('audit')">
            <el-icon><Document /></el-icon><span>审计看板</span>
          </div>
        </div>
      </div>

      <!-- 语义与指标：语义查询 + 指标管理 + 知识图谱 -->
      <div v-if="semVisible" class="nav-group" :class="{ open: semOpen }">
        <div class="nav-item nav-group-head" @click="semOpen = !semOpen">
          <el-icon><Histogram /></el-icon><span>语义与指标</span>
          <el-icon class="nav-arrow"><ArrowDown /></el-icon>
        </div>
        <div v-show="semOpen" class="nav-sub">
          <div v-if="can('sem')" class="nav-item" :class="{ on: store.view === 'sem' }" @click="setView('sem')">
            <el-icon><DataAnalysis /></el-icon><span>语义查询</span>
          </div>
          <div v-if="can('sem')" class="nav-item" :class="{ on: store.view === 'registry' }" @click="setView('registry')">
            <el-icon><Tickets /></el-icon><span>指标管理</span>
          </div>
          <div v-if="can('sem')" class="nav-item" :class="{ on: store.view === 'kbGraph' }" @click="setView('kbGraph')">
            <el-icon><Share /></el-icon><span>知识图谱</span>
          </div>
        </div>
      </div>

      <!-- 智能调优：Agent 个性化 / 记忆 / 样本 / 词典等治理面板归口 -->
      <div v-if="tuningVisible" class="nav-group" :class="{ open: tuningOpen }">
        <div class="nav-item nav-group-head" @click="tuningOpen = !tuningOpen">
          <el-icon><MagicStick /></el-icon><span>智能调优</span>
          <el-icon class="nav-arrow"><ArrowDown /></el-icon>
        </div>
        <div v-show="tuningOpen" class="nav-sub">
          <div class="nav-item" :class="{ on: store.view === 'prefs' }" @click="setView('prefs')">
            <el-icon><Star /></el-icon><span>我的偏好</span>
          </div>
          <div v-if="can('memory')" class="nav-item" :class="{ on: store.view === 'memory' }" @click="setView('memory')">
            <el-icon><Notebook /></el-icon><span>记忆管理</span>
          </div>
          <div v-if="can('fewshots')" class="nav-item" :class="{ on: store.view === 'fewshots' }" @click="setView('fewshots')">
            <el-icon><Tickets /></el-icon><span>Few-Shot 管理</span>
          </div>
          <div v-if="can('slang')" class="nav-item" :class="{ on: store.view === 'slang' }" @click="setView('slang')">
            <el-icon><Reading /></el-icon><span>黑话词典</span>
          </div>
          <div v-if="can('slang')" class="nav-item" :class="{ on: store.view === 'slangMetrics' }" @click="setView('slangMetrics')">
            <el-icon><DataLine /></el-icon><span>改写看板</span>
          </div>
        </div>
      </div>

      <div v-if="admin" class="nav-item" :class="{ on: store.view === 'users' }" @click="setView('users')">
        <el-icon><UserFilled /></el-icon><span>用户管理</span>
      </div>
    </div>
    <div class="side-section">
      <span>最近会话</span>
      <el-icon class="side-refresh" title="刷新会话列表" @click="loadSessions"><Refresh /></el-icon>
    </div>
    <div class="session-tools">
      <el-input
        v-model="sessionKeyword"
        class="session-search"
        size="small"
        placeholder="搜索会话标题"
        clearable
        :prefix-icon="Search"
      />
      <el-select v-model="sessionRange" size="small" class="session-range">
        <el-option label="全部" value="all" />
        <el-option label="今天" value="today" />
        <el-option label="近7天" value="7d" />
        <el-option label="近30天" value="30d" />
      </el-select>
    </div>
    <div class="session-list">
      <div v-if="!store.sessions.length" class="session-empty">暂无会话，开始提问吧</div>
      <div v-else-if="!filteredSessions.length" class="session-empty">没有匹配的会话</div>
      <div
        v-for="s in filteredSessions"
        :key="s.id"
        class="session-item"
        :class="{ active: s.id === store.sessionId }"
        @click="onPickSession(s.id)"
      >
        <el-icon class="s-ico"><ChatDotRound /></el-icon>
        <span class="s-txt" :title="s.title">{{ s.title }}</span>
        <span class="s-time">{{ timeLabel(s) }}</span>
        <el-icon class="s-del" title="删除会话" @click.stop="onRemove(s.id)"><Delete /></el-icon>
      </div>
    </div>
    <div class="side-foot">
      <div class="avatar-sm">{{ userInitial }}</div>
      <div class="u-meta">
        <div class="u-name">{{ user?.display_name || user?.username }}</div>
        <div class="u-sub">{{ tenantLabel }} · {{ roleLabel }}</div>
      </div>
      <el-icon class="u-logout" title="退出登录" @click="onLogout"><SwitchButton /></el-icon>
    </div>
  </aside>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import {
  ArrowDown, ChatDotRound, Collection, Cpu, DataAnalysis, DataLine, Delete, Document, Grid, Histogram,
  MagicStick, Monitor, Notebook, Plus, Refresh, Reading, Search, Setting, Share, Star, SwitchButton, Tickets, UserFilled
} from '@element-plus/icons-vue'
import {
  store, newChat, openSessionInChat, removeSession, loadSessions, setView,
  setObsFilter, loadObsPage, isAdmin, canView, logout
} from '../store'

/* ===== 最近会话：搜索 + 时间筛选 + 活动时间倒序 ===== */
const sessionKeyword = ref('')
const sessionRange = ref('all')

/* 会话活跃时间：优先 updated_at（最近一次有消息/改名），回退 created_at */
function activeTime(s) {
  const u = s.updated_at ? new Date(s.updated_at).getTime() : 0
  const c = s.created_at ? new Date(s.created_at).getTime() : 0
  return u || c || 0
}

const filteredSessions = computed(() => {
  const kw = sessionKeyword.value.trim().toLowerCase()
  const now = Date.now()
  const DAY = 86400000
  const rangeStart = { today: now - DAY, '7d': now - 7 * DAY, '30d': now - 30 * DAY }[sessionRange.value] || 0
  return store.sessions
    .filter((s) => {
      if (kw && !String(s.title || '').toLowerCase().includes(kw)) return false
      if (rangeStart && activeTime(s) < rangeStart) return false
      return true
    })
    .sort((a, b) => activeTime(b) - activeTime(a)) // 时间倒序：最近活动在前
})

/* 列表项右侧的相对时间标签（今天显示时:分，更早显示 月/日） */
function timeLabel(s) {
  const t = activeTime(s)
  if (!t) return ''
  const d = new Date(t)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const pad = (n) => String(n).padStart(2, '0')
  return sameDay ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : `${d.getMonth() + 1}/${d.getDate()}`
}

const admin = computed(() => isAdmin())
/* 导航项按权限点显隐（viewer 只看得到对话 / 知识库 / 语义元信息） */
const can = (view) => canView(view)
const user = computed(() => store.auth.user)
const userInitial = computed(() => {
  const n = user.value?.display_name || user.value?.username || '用'
  return n.slice(0, 1).toUpperCase()
})
const ROLE_LABEL = { admin: '管理员', analyst: '分析师', viewer: '只读用户' }
const roleLabel = computed(() => ROLE_LABEL[user.value?.role] || user.value?.role || '')
const tenantLabel = computed(() => user.value?.tenant_code || '默认租户')

function onNewChat() {
  setView('chat')
  newChat()
}

/* ===== 「智能调优」分组：我的偏好 / 记忆 / Few-Shot / 黑话词典 / 改写看板 ===== */
const TUNING_VIEWS = ['prefs', 'memory', 'fewshots', 'slang', 'slangMetrics']
/* 任一子项有权限（或当前正处子视图）时显示分组 */
const tuningVisible = computed(() =>
  TUNING_VIEWS.some((v) => canView(v)) || TUNING_VIEWS.includes(store.view)
)
/* 折叠态；默认按当前视图命中自动展开，之后交由用户控制 */
const tuningOpen = ref(TUNING_VIEWS.includes(store.view))
watch(
  () => store.view,
  (v) => { if (TUNING_VIEWS.includes(v)) tuningOpen.value = true }
)

/* ===== 「提示词与插件」分组：提示词管理 + Skill / MCP ===== */
const CONFIG_VIEWS = ['prompts', 'plugins']
const configVisible = computed(() => CONFIG_VIEWS.some((v) => canView(v)))
const configOpen = ref(CONFIG_VIEWS.includes(store.view))
watch(() => store.view, (v) => { if (CONFIG_VIEWS.includes(v)) configOpen.value = true })

/* ===== 「观测与审计」分组：运行观测 + 审计看板 ===== */
const OBS_VIEWS = ['obs', 'audit']
const obsVisible = computed(() => canView('obs') || isAdmin())
const obsOpen = ref(OBS_VIEWS.includes(store.view))
watch(() => store.view, (v) => { if (OBS_VIEWS.includes(v)) obsOpen.value = true })

/* ===== 「语义与指标」分组：语义查询 + 指标管理 + 知识图谱 ===== */
const SEM_VIEWS = ['sem', 'registry', 'kbGraph']
const semVisible = computed(() => canView('sem'))
const semOpen = ref(SEM_VIEWS.includes(store.view))
watch(() => store.view, (v) => { if (SEM_VIEWS.includes(v)) semOpen.value = true })

/* 各视图（chat / obs / sem / kb / plugins / prompts）通用的会话项点击：先切回 chat 视图，再异步加载历史，滚到底 */
async function onPickSession(id) {
  if (store.sessionId === id && store.view === 'chat') return
  await openSessionInChat(id)
}

/* 「运行观测」导航项：从其它视图（含 chat）跳转时自动注入当前会话到过滤器，
 * 从 obs → obs（用户已在 obs）只刷新，不覆盖手动设置的过滤器 */
/* 侧栏「运行观测」：全局观测（不注入会话过滤）。
 * 「跟踪当前对话」由专页内按钮显式触发。 */
function onOpenObservability() {
  if (store.view === 'obs') loadObsPage()
  else setView('obs')
}

async function onRemove(id) {
  const cf = await ElMessageBox.confirm('删除该会话及其全部消息？', '删除确认', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消'
  }).catch(() => null)
  if (!cf) return
  await removeSession(id)
  ElMessage({ message: '会话已删除', type: 'success' })
}

async function onLogout() {
  const cf = await ElMessageBox.confirm('确定退出登录？', '退出登录', {
    type: 'warning', confirmButtonText: '退出', cancelButtonText: '取消'
  }).catch(() => null)
  if (!cf) return
  logout()
}
</script>

<style scoped>
.side-section { display: flex; align-items: center; justify-content: space-between; }
.side-refresh { font-size: 13px; cursor: pointer; }
.side-refresh:hover { color: var(--primary); }
.session-empty { font-size: 12px; color: var(--muted); padding: 10px 10px; }
/* ===== 最近会话：搜索 + 筛选工具行 ===== */
.session-tools { display: flex; gap: 6px; padding: 4px 12px 6px; align-items: center; }
.session-search { flex: 1; min-width: 0; }
.session-search :deep(.el-input__wrapper) { border-radius: 8px; box-shadow: none; background: var(--bg); }
.session-range { width: 88px; flex: none; }
.session-range :deep(.el-select__wrapper) { border-radius: 8px; box-shadow: none; background: var(--bg); }
.s-time { flex: none; font-size: 10px; color: var(--muted); font-variant-numeric: tabular-nums; }
.session-item .s-del { flex: none; font-size: 13px; opacity: 0; transition: opacity .15s; }
.session-item:hover .s-del { opacity: .6; }
.session-item .s-del:hover { opacity: 1; color: #f53f3f; }
.u-meta { flex: 1; min-width: 0; }
.u-name { font-size: 13px; color: #1f2329; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.u-logout { font-size: 15px; color: var(--muted); cursor: pointer; flex: none; }
.u-logout:hover { color: #f53f3f; }
/* ===== 「智能调优」分组 ===== */
.nav-group-head .nav-arrow { margin-left: auto; font-size: 12px; color: var(--muted); transition: transform .2s; }
.nav-group.open .nav-arrow { transform: rotate(180deg); }
.nav-sub { padding-left: 14px; }
.nav-sub .nav-item { padding: 6px 10px; font-size: 12.5px; }
</style>
