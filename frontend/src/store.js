/* 全局状态（Composition 风格轻量 store）
 * 对接 DmfDataAgent 后端：会话 / 知识库 / 插件 / 提示词 / 观测 / 流式对话
 */
import { reactive } from 'vue'
import { ElMessage } from 'element-plus'
import * as api from './utils/api'
import { normCaliber } from './utils/caliber'
import {
  clearAuth, getAuthUser, getRefreshToken, getToken, setAuthUser, setRefreshToken, setToken
} from './utils/storage'

/* 个人知识库归属标识：优先取当前登录用户 id，未登录回退（后端在鉴权开启时以令牌为准） */
export const USER_ID = 'frontend-user'
export function currentUserId() {
  return (store.auth && store.auth.user && store.auth.user.id) || USER_ID
}
export function isAdmin() {
  return !!(store.auth && store.auth.user && store.auth.user.role === 'admin')
}

/* ---- 权限点判定（后端 roles.role_permissions 下发到 user.permissions）----
 * admin 为通配符 '*'，对任意权限点放行。前端据此显隐入口/按钮，
 * 后端各接口另有 require_permission 守卫兜底（显隐 ≠ 安全边界）。 */
export function hasPerm(perm) {
  const perms = (store.auth.user && store.auth.user.permissions) || []
  return perms.includes('*') || perms.includes(perm)
}
export function hasAnyPerm(list) {
  return (list || []).some((p) => hasPerm(p))
}
/* 视图 → 所需权限点（前端导航显隐） */
const VIEW_PERM = {
  chat: 'chat',
  kb: 'knowledge',
  plugins: 'plugin',
  prompts: 'prompt',
  models: 'model',
  obs: 'obs',
  sem: 'semantic',
  /* 记忆管理（L1/L2/L3 平台视角）与 Few-Shot 样本维护 / 黑话词典 */
  memory: 'memory',
  fewshots: 'fewshot',
  slang: 'slang',
  slangMetrics: 'slang',
  users: 'admin'
}
export function canView(view) {
  const need = VIEW_PERM[view]
  if (!need) return true
  // 未登录 / 权限信息缺失时不拦截（后端接口会兜底 403）
  if (!store.auth.user) return true
  return hasPerm(need)
}

/* 四阶段进度（对应后端图的节点分组） */
export const STAGES = ['意图识别', '语义解析', '查询执行', '回答生成']
export const SRC_MAP = { rule: '规则', llm: '大模型', 'rule-fallback': '规则兜底' }

/* 图节点 → 中文标签 / 所属阶段 */
const NODE_LABEL = {
  rewrite_query: '黑话改写',
  classify_intent: '意图识别',
  extract_elements: '要素抽取',
  retrieve_knowledge: '知识检索',
  validate_elements: '要素校验',
  ask_clarification: '反问澄清',
  apply_clarification: '确认续跑',
  build_dsl: 'DSL 构建',
  execute_query: '查询执行',
  analyze_data: '数据分析',
  diagnose_data: '诊断归因',
  ask_time_redirect: '改查确认',
  synthesize_answer: '回答生成'
}
const NODE_STAGE = {
  rewrite_query: 0,
  classify_intent: 0,
  apply_clarification: 1,
  extract_elements: 1,
  retrieve_knowledge: 1,
  validate_elements: 1,
  build_dsl: 2,
  execute_query: 2,
  analyze_data: 3,
  diagnose_data: 3,
  ask_time_redirect: 3,
  synthesize_answer: 3
}

export const store = reactive({
  /* 视图：chat 对话 / kb 知识库 / plugins 插件 / prompts 提示词 / users 用户管理 */
  view: 'chat',
  /* 认证：user 为空表示未登录（App 渲染登录页） */
  auth: {
    user: getAuthUser(),
    loading: false,
    error: ''
  },
  /* 管理端：用户 / 租户 / 角色（仅 admin） */
  admin: {
    users: [], tenants: [], roles: [], loading: false, error: '', tenantFilter: ''
  },
  /* 会话 */
  sessionId: null,
  sessions: [],
  messages: [],
  /* 点赞 / 点踩反馈状态：{ [roundId]: 'like' | 'dislike' }，供消息气泡高亮回显 */
  feedbackMap: {},
  /* 黑话改写 👍/👎 反馈状态：{ [roundId]: 1 正确 / -1 误改写 }，与 feedbackMap 同等回显逻辑 */
  slangFeedbackMap: {},
  /* FR13 当前用户的有效权限上下文（T198f 前端权限标记）：
   * { mode, restricted, unrestricted_metrics, allowed_metrics[], denied_dimensions[],
   *   row_scope_dimensions[], row_scope_values{} }；null=尚未加载 */
  permCtx: null,
  sending: false,
  /* 当前流式对话的 AbortController（停止生成用；null=无进行中流） */
  activeAbort: null,
  /* 未落库错误（如 E5005 会话忙）时还原输入框草稿的回调，ChatInput 挂载时注册 */
  restoreDraft: null,
  /* 上下文占比圆环（轮次口径）：{memoryEnabled, uncovered_rounds, threshold, retain_rounds, ratio, has_summary, compressible} */
  context: emptyContext(),
  contextCompressing: false,
  /* 健康 */
  health: { ok: false, components: {}, raw: null },
  /* 观测 */
  obsVisible: false,
  /* 对话上方观测按钮的会话范围（null = 全局；由 openChatObs 设置） */
  obsSessionId: null,
  obsLoading: false,
  obs: null,
  obsError: '',
  /* 观测专页（汇总 + 明细 + 下钻） */
  obsPage: {
    loading: false,
    error: '',
    summary: null,
    rounds: [],
    total: 0,
    page: 1,
    pageSize: 20,
    filters: { session_id: '', intent: '', has_error: '', keyword: '' },
    spanStats: null,
    detail: null,
    detailLoading: false,
    /* FR11 成本 / 质量看板 */
    cost: null,
    quality: null,
    costGroupBy: 'model',
    costDays: 7
  },
  /* 知识库 */
  kb: {
    scope: 'personal', bases: [], loading: false, keyword: '',
    current: null, docs: [], docsLoading: false, docKeyword: '',
    /* FR7 来源分级：权威源 / 沉淀源 / 用户上传的文档数与精排可用性 */
    authority: null, seeding: false
  },
  /* 插件 */
  plugins: { skills: [], mcp: [], loading: false },
  /* 模型管理（对话 LLM 配置；default=库内默认标记名，effective=运行时生效名） */
  models: { items: [], default: null, effective: null, loading: false },
  /* 提示词 */
  prompts: { items: [], builtinVersion: '', changelog: [], loading: false },
  /* L2 用户偏好（个人记忆：用户手动设置 + 系统从点赞学习） */
  prefs: { items: [], loading: false, error: '', disabled: false, saving: false },
  /* 记忆管理页（平台视角：L1 会话记忆 / L2 用户记忆 / L3 长期记忆） */
  memoryPage: {
    tab: 'l1',
    stats: null,
    error: '',
    l1: { items: [], keyword: '', loading: false },
    l2: {
      items: [], loading: false,
      filters: { user_id: '', source: '', status: '' }
    },
    l3: {
      items: [], loading: false,
      filters: { type: '', status: '', keyword: '', include_few_shot: true }
    },
    saving: false
  },
  /* Few-Shot 样本管理页（点赞沉淀 + 手工录入；试召回 / 导入导出） */
  fewshotPage: {
    items: [], total: 0, page: 1, pageSize: 20,
    filters: { category: '', intent: '', source: '', status: '', keyword: '' },
    facets: { categories: [], intents: [], tags: [] },
    stats: null,
    loading: false, error: '', saving: false,
    recall: { visible: false, query: '', topK: 3, loading: false, result: null, error: '' }
  },
  /* 三级黑话词典管理页（行业级 / 公司级 / 用户级；在线试改写 / 导入导出 / 版本） */
  slangPage: {
    items: [], total: 0, page: 1, pageSize: 20,
    filters: { scope: '', entry_type: '', status: '', source: '', keyword: '' },
    facets: { scopes: [], entry_types: [], statuses: [], sources: [], metric_names: [], dimension_names: [], used_terms: [] },
    stats: null,
    metrics: null,           // 命中率 / 误改写率看板聚合
    loading: false, error: '', saving: false,
    /* 在线试改写（本页最有价值的按钮）：复用线上解析器 */
    preview: {
      visible: false, query: '', includeDraft: false,
      loading: false, result: null, error: ''
    },
    /* 版本抽屉：某词条的版本历史 + 回滚 */
    versions: { visible: false, entryId: null, term: '', loading: false, items: [], error: '' }
  },
  /* 语义查询（SQL 编译 / 结果回显） */
  semExp: {
    loadingMeta: false,
    // 区分当前是哪个动作在 loading：false | 'compile' | 'query'
    loadingRun: false,
    error: '',
    health: null,              // { reachable, mode, detail }
    metrics: [],               // [{ name, type, unit, aliases, granularities, dimensions, description }]
    dimensions: [],            // [{ name, type, expr, time_granularity, models }]
    manifest: null,            // { engine, manifest, starrocks, semantic_models, metrics }
    form: {
      metrics: [],             // 业务指标名（多选）
      groupBy: [],             // 分组维度（多选；含时间维度"统计日期"）
      start: '',               // YYYY-MM-DD
      end: '',                 // YYYY-MM-DD
      granularity: 'day',
      limit: 50,
      // ---- 高级选项（同环比 / 维度过滤 / 二次分析）----
      compareOn: false,        // 是否开启同环比
      // 同环比类型（可多选，FR1 支持同比 + 环比并存）：yoy | mom | wow | qoq | pop
      compareTypes: ['yoy'],
      // 维度过滤：[{ field, operator, values }]，values 为逗号分隔字符串
      filters: [],
      advAgg: '',              // 聚合覆盖：'' | 'avg' | 'max' | 'min'
      advProportion: false,    // 占比（__占比）
      advRank: false,          // 排名（__排名）
      rankScope: 'global'      // 排名范围：global 全局 | group 组内（按时间周期分别排名）
    },
    compile: null,             // { sql, columns, group_columns, metric_columns, models, engine }
    run: null                  // { columns, rows, sql?, row_count, elapsed_ms, error? }
  }
})

/* ---- 会话级流式生成状态（支持多会话并行）----
 * 需求：切到别的会话时，原会话**继续生成**，切回可见完整结果。
 * - sessionMessages：按会话缓存消息数组（含在途生成的消息）；切走只换指针、不销毁，
 *   在途流通过闭包引用写入缓存数组，切回即见最新内容；
 * - streaming：正在生成的会话 → AbortController（同一会话同时最多一个流，跨会话可并行）。
 * NEW_CHAT_KEY 是「新建对话」占位槽：首轮流拿到真实 session_id 后整体迁移到该 id。
 * store.sending / activeAbort 降级为「当前会话」的派生视图，经 syncStreamState() 维护。
 */
const NEW_CHAT_KEY = '::new-chat::'
const sessionMessages = {}
const streaming = {}

/* 当前视图对应的流式键：新建对话（sessionId 为空）用占位槽 */
function currentStreamKey() {
  return store.sessionId || NEW_CHAT_KEY
}
function isStreaming(key) {
  return !!streaming[key]
}
/* 让 store.sending / activeAbort 跟随「当前会话」的流状态（组件据此显隐停止按钮 / 禁发） */
function syncStreamState() {
  const key = currentStreamKey()
  store.sending = !!streaming[key]
  store.activeAbort = streaming[key] || null
}

/* ============ 视图切换 ============ */
export function setView(v) {
  const prev = store.view
  // 入口按权限点显隐（前端显隐 + 后端接口 403 双重）；无权限的视图直接忽略
  if (!canView(v)) return
  store.view = v
  if (v === 'kb') loadKbBases()
  if (v === 'plugins') loadPlugins()
  if (v === 'prompts') loadPromptList()
  if (v === 'models') loadModels()
  if (v === 'sem') loadSemanticMeta()
  if (v === 'users') loadAdminMeta()
  if (v === 'prefs') loadPreferences()
  if (v === 'memory') loadMemoryPage()
  if (v === 'fewshots') loadFewShotPage()
  if (v === 'slang') loadSlangPage()
  if (v === 'slangMetrics') loadSlangMetrics()
  if (v === 'obs') {
    // 侧栏「运行观测」= 全局观测（不注入会话过滤）；
    // 「跟踪当前对话」由专页内按钮显式触发（setObsFilter）
    loadObsPage()
  }
}

/* ============ 健康 ============ */
export async function refreshHealth() {
  store.health = await api.fetchHealth()
}

/* ============ 会话 ============ */

function emptyContext() {
  return {
    memory_enabled: false, total_rounds: 0, uncovered_rounds: 0, retain_rounds: 0,
    threshold: 0, ratio: 0, has_summary: false, compressible: false,
  }
}

/* 拉取当前会话的上下文占比状态（轮次口径）；无会话或失败时置空态，不阻断主流程 */
export async function refreshContext() {
  if (!store.sessionId) { store.context = emptyContext(); return }
  try {
    const st = await api.getContextStatus(store.sessionId)
    store.context = { ...emptyContext(), ...(st || {}) }
  } catch (e) { /* 圆环仅辅助，失败静默 */ }
}

/* 点击圆环：立即把更早轮次压缩为摘要，返回后端结果（null=未执行） */
export async function compressContext() {
  if (!store.sessionId || store.contextCompressing || !store.context.compressible) return null
  store.contextCompressing = true
  try {
    const res = await api.compressContext(store.sessionId)
    store.context = { ...emptyContext(), ...(res || {}) }
    return res
  } finally {
    store.contextCompressing = false
  }
}

export async function loadSessions() {
  try {
    store.sessions = await api.listSessions(USER_ID)
  } catch (e) {
    /* 静默降级 */
  }
}
export function newChat() {
  // 先把当前视图（含在途生成的消息）落缓存，避免切走即丢；再打开新会话槽
  sessionMessages[currentStreamKey()] = store.messages
  store.sessionId = null
  store.messages = sessionMessages[NEW_CHAT_KEY] || []
  sessionMessages[NEW_CHAT_KEY] = store.messages
  store.context = emptyContext()
  syncStreamState()
}
export async function switchSession(id) {
  // 先把当前会话视图（含在途生成的消息）存回缓存，再切过去 —— 切走不打断原会话的流
  sessionMessages[currentStreamKey()] = store.messages
  store.sessionId = id
  if (!sessionMessages[id] || !streaming[id]) {
    // 无缓存，或缓存不在途（可能已被其它标签页更新）→ 回源拉最新。
    // 正在本页生成（streaming）的会话**必须**用缓存：含尚未落库的在途消息，
    // 回源会用 DB 旧态覆盖丢帧
    if (!streaming[id]) delete sessionMessages[id]
    try {
      const msgs = await api.listMessages(id)
      store.messages = msgs.map(msgFromServer)
      sessionMessages[id] = store.messages
      // 批量回显历史消息的点赞 / 点踩状态
      const ids = store.messages
        .filter((m) => m.role === 'assistant' && m.roundId)
        .map((m) => m.roundId)
      await loadFeedbackStatus(ids)
      // 同样回显黑话改写的 👍/👎 状态（与点赞/点踩同口径，重开会话状态不变）
      await loadSlangRewriteFeedback(ids)
    } catch (e) {
      store.messages = []
      sessionMessages[id] = store.messages
    }
  } else {
    // 本页正在该会话生成：直接用缓存（含在途结果）
    store.messages = sessionMessages[id]
  }
  syncStreamState()
  await refreshContext()
}

/* 切换到对话视图并加载指定会话的完整历史 —— 在任一其它视图（obs/sem/kb/plugins/prompts）
 * 点侧栏会话项时统一调用，避免被当前 view 替换导致"点了没反应"。 */
export async function openSessionInChat(id) {
  store.view = 'chat'
  await switchSession(id)
  // 内容已切换，下一帧滚到底（用户希望看到最新一条）
  await Promise.resolve()
  scrollChatToBottom()
}

function scrollChatToBottom() {
  // 找带 .chat-scroll 的滚动容器，滚到底；若没找到（页面未挂载），尝试 document
  const el = document.querySelector('.chat-scroll, .messages, .chat')
  if (el) {
    requestAnimationFrame(() => { el.scrollTop = el.scrollHeight })
  }
}
export async function removeSession(id) {
  try {
    await api.deleteSession(id)
    delete sessionMessages[id]
    if (streaming[id]) { streaming[id].abort(); delete streaming[id] }
    if (store.sessionId === id) newChat()
    await loadSessions()
  } catch (e) { /* ignore */ }
}
export function sessionTitle() {
  const s = store.sessions.find((x) => x.id === store.sessionId)
  return s ? s.title : '新对话'
}

/* ============ 点赞 / 点踩反馈 ============ */
/* 提交反馈：点赞进 few-shot 库、点踩进 bad-case 库；再点一次同类型 = 取消（后端删样本）。
   roundId 即助手消息 id（= 后端 messages.id = trace_records.round_id）。
   乐观更新本地高亮，请求失败回滚。 */
export async function submitFeedback(roundId, type, reason) {
  if (!roundId || (type !== 'like' && type !== 'dislike')) return
  const prev = store.feedbackMap[roundId] || null
  // toggle：已处于同类型状态 -> 取消反馈
  const cancel = prev === type
  if (cancel) delete store.feedbackMap[roundId]
  else store.feedbackMap[roundId] = type
  try {
    await api.submitFeedback({
      message_id: roundId, type: cancel ? 'cancel' : type, reason,
      session_id: store.sessionId || undefined
    })
  } catch (e) {
    store.feedbackMap[roundId] = prev
    console.error('反馈提交失败：', e)
  }
}

/* 批量拉取消息反馈状态并合并进 feedbackMap（历史消息回显用）。 */
export async function loadFeedbackStatus(ids) {
  const list = (ids || []).filter(Boolean)
  if (!list.length) return
  try {
    const res = await api.fetchFeedbackStatus(list)
    if (res && res.status) Object.assign(store.feedbackMap, res.status)
  } catch (e) {
    /* 静默降级：状态缺失不影响对话 */
  }
}

/* ============ 黑话改写反馈（👍 正确 / 👎 误改写） ============ */
/* 与点赞/点踩同思路：点击显示、再点同类型取消、重新打开会话从后端回显（状态不变）。
   roundId 即助手消息 id（= 后端 messages.id = slang_rewrite_events.message_id）。
   optimistic 更新本地高亮，请求失败回滚。 */
export async function submitSlangRewriteFeedback(roundId, correct, opts = {}) {
  if (!roundId || (correct !== true && correct !== false)) return
  const prev = store.slangFeedbackMap[roundId] || 0
  const next = correct ? 1 : -1
  // toggle：已处于同状态 -> 取消反馈
  const cancel = prev === next
  if (cancel) delete store.slangFeedbackMap[roundId]
  else store.slangFeedbackMap[roundId] = next
  try {
    await api.feedbackSlangRewrite({
      original: opts.original || '',
      is_correct: correct,
      note: '',
      session_id: opts.sessionId || store.sessionId || undefined,
      message_id: roundId,
      cancel
    })
  } catch (e) {
    // 回滚到变更前（prev=0 即无反馈 -> 删掉）
    if (prev) store.slangFeedbackMap[roundId] = prev
    else delete store.slangFeedbackMap[roundId]
    console.error('黑话改写反馈提交失败：', e)
  }
}

/* 批量拉取黑话改写反馈状态并合并进 slangFeedbackMap（历史消息回显用）。 */
export async function loadSlangRewriteFeedback(ids) {
  const list = (ids || []).filter(Boolean)
  if (!list.length) return
  try {
    const res = await api.fetchSlangRewriteFeedbackStatus(list)
    if (res && res.status) {
      const map = {}
      for (const [mid, v] of Object.entries(res.status)) {
        map[mid] = v === 'good' ? 1 : (v === 'bad' ? -1 : 0)
      }
      Object.assign(store.slangFeedbackMap, map)
    }
  } catch (e) {
    /* 静默降级：状态缺失不影响对话 */
  }
}

/* ============ 知识库 ============ */
export async function loadKbBases() {
  store.kb.loading = true
  try {
    const params = { type: store.kb.scope }
    // 个人库按真实登录用户过滤：后端创建个人库时 owner_id 被覆写为令牌 sub（current.id），
    // 必须与前端过滤口径一致，否则查不到自己新建的个人库。
    if (store.kb.scope === 'personal') params.owner_id = currentUserId()
    if (store.kb.keyword) params.keyword = store.kb.keyword
    store.kb.bases = await api.listKbBases(params)
  } catch (e) {
    store.kb.bases = []
  }
  store.kb.loading = false
}
export function switchKbScope(scope) {
  store.kb.scope = scope
  store.kb.current = null
  store.kb.docs = []
  loadKbBases()
}
export async function createKbBase(name, description) {
  const type = store.kb.scope
  const body = {
    name,
    description: description || '',
    type,
    // 个人库 owner_id 由后端按令牌 sub 覆写；前端传当前登录用户 id 与后端口径对齐（避免越权/错配）。
    owner_id: type === 'personal' ? currentUserId() : '',
    visibility: type === 'personal' ? 'private' : 'public'
  }
  const base = await api.createKbBase(body)
  await loadKbBases()
  return base
}
export async function removeKbBase(id) {
  const r = await api.deleteKbBase(id)
  if (store.kb.current && store.kb.current.id === id) closeKbBase()
  await loadKbBases()
  return r
}
export async function openKbBase(base) {
  store.kb.current = base
  store.kb.docKeyword = ''
  await loadKbDocs(base.id)
}
export function closeKbBase() {
  store.kb.current = null
  store.kb.docs = []
}
export async function loadKbDocs(kbId) {
  store.kb.docsLoading = true
  try {
    store.kb.docs = await api.listKbDocs({
      knowledge_base_id: kbId,
      keyword: store.kb.docKeyword || undefined
    })
  } catch (e) {
    store.kb.docs = []
  }
  store.kb.docsLoading = false
}
export async function addKbDocText(title, text) {
  const doc = await api.addKbDoc({
    title,
    text,
    knowledge_base_id: store.kb.current ? store.kb.current.id : undefined
  })
  if (store.kb.current) await loadKbDocs(store.kb.current.id)
  await loadKbBases()
  return doc
}
export async function uploadKbDoc(file) {
  const doc = await api.uploadKbDoc(file, store.kb.current ? store.kb.current.id : undefined)
  if (store.kb.current) await loadKbDocs(store.kb.current.id)
  await loadKbBases()
  return doc
}
export async function removeKbDoc(id) {
  const r = await api.deleteKbDoc(id)
  if (store.kb.current) await loadKbDocs(store.kb.current.id)
  await loadKbBases()
  return r
}
/* 文档分块列表（核验切分/入库质量，只读）；异常返回空数组，不打断面板 */
export async function fetchDocChunks(id) {
  try {
    return await api.listDocChunks(id)
  } catch (e) {
    return []
  }
}
/* FR7：来源分级统计（权威源 / 沉淀源 / 用户上传）+ 精排可用性 */
export async function loadAuthorityStats() {
  try {
    store.kb.authority = await api.getAuthorityStats()
  } catch (e) {
    store.kb.authority = null
  }
}
/* FR7：一键灌入指标口径字典（权威源，幂等覆盖；仅管理员） */
export async function seedMetricDictionary() {
  store.kb.seeding = true
  try {
    const r = await api.seedMetricDictionary(store.kb.current ? store.kb.current.id : undefined)
    if (store.kb.current) await loadKbDocs(store.kb.current.id)
    await Promise.all([loadKbBases(), loadAuthorityStats()])
    return r
  } finally {
    store.kb.seeding = false
  }
}

/* ============ 插件（Skill / MCP） ============ */
export async function loadPlugins() {
  store.plugins.loading = true
  try {
    const [s, m] = await Promise.all([api.fetchSkills(), api.fetchMcp()])
    store.plugins.skills = (s && s.items) || []
    store.plugins.mcp = (m && m.items) || []
  } catch (e) {
    /* 静默降级 */
  }
  store.plugins.loading = false
}
export async function addSkillPlugin(name, description) {
  const j = await api.addSkill({ name, description, config: '{}' })
  if (j.ok) await loadPlugins()
  return j
}
/* 上传 SKILL.md 声明式包（沙箱闸口在服务端：后缀/大小/编码/内容扫描/工具白名单） */
export async function uploadSkillPlugin(file, description = '') {
  try {
    const j = await api.uploadSkill(file, description)
    if (j.ok) await loadPlugins()
    return j
  } catch (e) {
    return { ok: false, message: e.message || '上传失败' }
  }
}
export async function fetchSkillPackage(name) {
  try {
    return await api.fetchSkillDetail(name)
  } catch (e) {
    return { ok: false, message: e.message || '加载详情失败' }
  }
}
/* 沙箱执行：服务端经 Harness 白名单/权限/预算/超时治理，返回步骤轨迹 */
export async function runSkillPlugin(name, inputs) {
  try {
    return await api.runSkill(name, inputs)
  } catch (e) {
    return { ok: false, message: e.message || '执行失败', steps: [] }
  }
}
export async function testMcpPlugin(name) {
  try {
    return await api.testMcp(name)
  } catch (e) {
    return { ok: false, message: e.message || '连接测试失败', tools: [] }
  }
}
/* 新增弹窗的预检试连（不落库；无副作用，故不触发 loadPlugins） */
export async function probeMcpPlugin(payload) {
  try {
    return await api.probeMcp(payload)
  } catch (e) {
    return { ok: false, message: e.message || '连接测试失败', tools: [] }
  }
}
export async function addMcpPlugin(payload) {
  const j = await api.addMcp(payload)
  if (j.ok) await loadPlugins()
  return j
}
export async function removePlugin(pluginType, name) {
  const j = await api.deletePlugin(pluginType, name)
  if (j.ok) await loadPlugins()
  return j
}

/* ============ 模型管理（对话 LLM 配置） ============ */
export async function loadModels() {
  store.models.loading = true
  try {
    const j = await api.listModels()
    store.models.items = (j && j.items) || []
    store.models.default = (j && j.default) || null
    store.models.effective = (j && j.effective) || null
  } catch (e) {
    /* 静默降级 */
  }
  store.models.loading = false
}
export async function addModel(payload) {
  const j = await api.createModel(payload)
  if (j.ok) await loadModels()
  return j
}
export async function updateModel(name, payload) {
  const j = await api.updateModel(name, payload)
  if (j.ok) await loadModels()
  return j
}
export async function removeModel(name) {
  const j = await api.deleteModel(name)
  if (j.ok) await loadModels()
  return j
}
export async function setModelDefault(name) {
  const j = await api.setModelDefault(name)
  if (j.ok) await loadModels()
  return j
}
/* 新增/编辑弹窗的预检试连（不落库；无副作用，故不触发 loadModels） */
export async function probeModelCfg(payload) {
  try {
    return await api.probeModel(payload)
  } catch (e) {
    return { ok: false, message: e.message || '连接测试失败' }
  }
}
export async function testModelCfg(name) {
  try {
    const j = await api.testModel(name)
    if (j.ok === false) await loadModels() // 失败也回写 last_test，刷新展示
    return j
  } catch (e) {
    return { ok: false, message: e.message || '连接测试失败' }
  }
}

/* ============ 系统提示词 ============ */
export async function loadPromptList() {
  store.prompts.loading = true
  try {
    const j = await api.fetchPromptsList()
    if (j.ok) {
      store.prompts.items = j.items || []
      store.prompts.builtinVersion = j.builtin_version || ''
      store.prompts.changelog = j.changelog || []
    }
  } catch (e) {
    /* 静默降级 */
  }
  store.prompts.loading = false
}
export async function loadPromptHistory(name) {
  const j = await api.fetchPromptHistory(name)
  return j.ok ? j.items || [] : []
}
export async function loadPromptDetail(name, version) {
  const j = await api.fetchPromptDetail(name, version)
  return j.ok ? j : null
}
export async function savePromptVersion(name, content, description) {
  const j = await api.updatePrompt(name, content, description)
  if (j.ok) await loadPromptList()
  return j
}
export async function rollbackPromptVersion(name, version) {
  const j = await api.rollbackPrompt(name, version)
  if (j.ok) await loadPromptList()
  return j
}

/* ============ 对话（SSE） ============ */
export async function send(question, opts = {}) {
  const q = question.trim()
  if (!q) return
  // 按会话串行：同一会话同时只允许一个流（跨会话可并行）；新建对话走占位槽
  // 注意用 let：首轮拿到真实 session_id 后要把键从占位槽迁移过去
  let streamKey = currentStreamKey()
  if (isStreaming(streamKey)) return
  // 用 reactive() 包装后再入列：terminal 事件回填 userMsg.id 时需要触发响应式更新
  const buf = store.messages
  const userMsg = reactive({ role: 'user', content: q, ts: Date.now(), id: null })
  buf.push(userMsg)

  const msg = reactive({
    role: 'assistant',
    content: '',
    pending: true,
    failed: false,
    stages: [0, 0, 0, 0],
    intentMeta: null,
    viz: [],
    trace: [],
    rewriteMeta: null,
    timeExtract: null,
    interpret: null,
    otel: null,
    elapsedMs: null,
    tokens: null
  })
  buf.push(msg)
  let finished = false

  /* 会话中断（SSE AbortController）：用户点「停止」时 abort 当前会话的流 */
  const controller = new AbortController()
  streaming[streamKey] = controller
  syncStreamState()

  /* 结构化澄清提交：携带用户点选结果，后端确定性合并要素后自动续跑后续步骤 */
  const payload = { message: q, session_id: store.sessionId, user_id: USER_ID }
  if (opts.clarifySelection) payload.clarify_selection = opts.clarifySelection

  try {
    await api.streamChat(
      payload,
      (ev) => {
      if (ev.type === 'session') {
        const newSid = ev.session_id
        if (!store.sessionId) store.sessionId = newSid
        if (newSid && newSid !== streamKey) {
          // 新建对话在首轮拿到真实 id：把消息缓存与流控制器从占位槽迁移到该 id，
          // 保证「切走再切回」仍能看到这轮在途消息
          sessionMessages[newSid] = buf
          delete sessionMessages[streamKey]
          streaming[newSid] = controller
          delete streaming[streamKey]
          streamKey = newSid
        }
        // 新会话即时出现在侧栏（在途期间即可切回，原会话继续生成）
        loadSessions()
        syncStreamState()
      } else if (ev.type === 'node_start') {
        // 节点开始：新增（或复用）"运行中"步骤，前端自增秒表
        const st = NODE_STAGE[ev.node]
        if (st != null) msg.stages[st] = 1
        const existing = msg.trace.find(t => t.stage === ev.node)
        const startedAt = (typeof ev.started_at === 'number' ? ev.started_at : Date.now() / 1000)
        if (!existing) {
          msg.trace.push({
            stage: ev.node,
            label: NODE_LABEL[ev.node] || ev.node,
            status: 'pending',
            detail: '执行中…',
            started_at: startedAt,
            elapsed_ms: null,
            live: true
          })
        } else if (existing.status === 'pending' && !existing.started_at) {
          existing.started_at = startedAt
          existing.live = true
        }
      } else if (ev.type === 'node_done') {
        // 节点完成：把状态/耗时写入对应的步骤
        const nodeName = ev.node
        const step = msg.trace.find(t => t.stage === nodeName)
        if (step) {
          step.status = ev.status || 'ok'
          step.elapsed_ms = typeof ev.elapsed_ms === 'number' ? ev.elapsed_ms : null
          step.detail = (step.elapsed_ms != null ? `耗时 ${step.elapsed_ms}ms` : '已完成')
          step.live = false
        } else {
          // 没追到开始时间就来了完成 → 直接追加完成态
          msg.trace.push({
            stage: nodeName,
            label: NODE_LABEL[nodeName] || nodeName,
            status: ev.status || 'ok',
            detail: typeof ev.elapsed_ms === 'number' ? `耗时 ${ev.elapsed_ms}ms` : '已完成',
            elapsed_ms: typeof ev.elapsed_ms === 'number' ? ev.elapsed_ms : null,
            live: false
          })
        }
        const st = NODE_STAGE[nodeName]
        if (st != null) {
          msg.stages[st] = (ev.status === 'error') ? 2 : 1
        }
      } else if (ev.type === 'token') {
        msg.content += ev.content || ''
      } else if (ev.type === 'final') {
        finished = true
        const t = ev.node_trace || {}
        msg.content = ev.content || msg.content
        msg.pending = false
        msg.wasMock = !!ev.mock_mode
        msg.intentMeta = ev.intent
          ? { intent: ev.intent, confidence: null, source: t.intent_source || '' }
          : null
        // 把流式期间已写入的 trace 与 final 一并合并（保留历史节点的 step、最终补 elapsed_ms）
        const finalTrace = traceFromNodeTrace(t)
        const merged = mergeTrace(msg.trace || [], finalTrace)
        msg.trace = merged
        // 把仍在 pending 的节点标记为 ok + 用 otel 时长填 elapsed_ms
        for (const step of msg.trace) {
          if (step.status === 'pending' && step.elapsed_ms == null) {
            step.status = 'ok'
          }
          step.live = false
        }
        msg.viz = vizFromNodeTrace(t)
        msg.sqlInfo = sqlInfoFromNodeTrace(t)
        msg.stages = stagesFromNodeTrace(t)
        msg.otel = t.otel || null
        msg.elapsedMs = t.elapsed_ms != null ? t.elapsed_ms : null
        msg.tokens = t.total_tokens != null
          ? { prompt: t.prompt_tokens || 0, completion: t.completion_tokens || 0, total: t.total_tokens }
          : null
        msg.roundId = ev.message_id || null
        msg.sessionId = ev.session_id || null
        // 回填本轮用户提问的落库 id（「替换该轮」的裁剪锚点）；历史消息本就有 id
        if (ev.user_message_id) userMsg.id = ev.user_message_id
        /* 结构化确认：要素表单（clarify，按字段渲染）或空数据时间窗重定向卡片
         * （clarify_time_redirect，无 fields）；用户点选提交后确定性续跑 */
        const clarify = ev.clarification
          || (t.confirmation && t.confirmation.clarification)
          || null
        if (clarify && ((clarify.fields && clarify.fields.length)
          || clarify.type === 'clarify_time_redirect')) msg.clarify = clarify
        if (ev.rewritten_query && ev.rewritten_query !== q) {
          msg.rewriteMeta = { original: q, rewritten: ev.rewritten_query }
        }
        /* 黑话改写（优先于指代消解展示：它作用在链路最前端） */
        const rwMeta = rewriteMetaFromTrace(t, q)
        if (rwMeta) msg.rewriteMeta = rwMeta
      } else if (ev.type === 'error') {
        finished = true
        msg.pending = false
        /* 结构化错误（FR6）：error_info 含 code/category/message/retryable */
        const info = ev.error_info || {}
        const errMsg = ev.content || info.message || ev.message || '未知错误'
        if (info.code === 'E5005') {
          /* 会话忙（同会话并发被串行化闸拒绝）：后端刻意不落库，
           * 把两条占位消息撤回、提问还原回输入框，用户稍后重发即可 */
          const idx = buf.indexOf(msg)
          if (idx >= 0) buf.splice(idx, 1)
          const uidx = buf.indexOf(userMsg)
          if (uidx >= 0) buf.splice(uidx, 1)
          if (store.restoreDraft) store.restoreDraft(q)
          ElMessage.warning(errMsg)
        } else {
          msg.failed = true
          msg.content = '⚠ 处理失败：' + errMsg
          msg.trace.push({ stage: 'error', label: '处理失败', status: 'error', detail: errMsg })
        }
      }
      },
      controller.signal
    )
  } catch (err) {
    if (controller.signal.aborted) {
      // 用户主动中断：保留已生成的部分内容，不标失败
      finished = true
      msg.pending = false
      msg.stopped = true
      if (msg.content) msg.content += '\n\n_（已手动停止生成）_'
      else msg.content = '_已手动停止生成，本轮未完成。_'
    } else {
      finished = true
      msg.failed = true
      msg.pending = false
      msg.content = '⚠ 请求失败：' + err.message
    }
  } finally {
    // 释放本会话的流；发送态只影响「当前正看的就是这个会话」时的按钮/禁发
    delete streaming[streamKey]
    syncStreamState()
    if (!finished && msg.pending) {
      msg.pending = false
      msg.failed = true
      msg.content = msg.content || '⚠ 流式响应中断'
    }
    await loadSessions()
    // 上下文占比是当前会话的：切回本会话才刷
    if (currentStreamKey() === streamKey) await refreshContext()
  }
}

/* 中断当前对话的流式生成（保留已生成的部分内容） */
export function stopStreaming() {
  if (store.activeAbort) store.activeAbort.abort()
}

/* 编辑重发「替换该轮」：裁掉被改写的这条消息**及其之后的全部轮次**，再以新内容重发。
 *
 * 与「追加式编辑」（saveEdit 直接 send）相对：
 * - 追加式：原问答保留，等价"改了这条再问一遍"——历史更完整，但旧问答仍在上下文里；
 * - 替换式：旧问答从 DB 与会话中消失，等价"我当初就该这么问"——上下文干净，但不可回退。
 *
 * 后端会顺带重置该会话的结构化对话状态（裁剪后旧状态已与历史不符，留着会成为下一轮的脏上下文），
 * 因此这里本地也把消息列表同步裁掉，随后 `send()` 追加新的用户消息与答复。
 */
export async function replaceRound(messageId, text) {
  const t = (text || '').trim()
  if (!t) return
  if (store.sending) { ElMessage.warning('正在生成回答，请稍后再发送'); return }
  if (!store.sessionId || !messageId) {
    ElMessage.error('替换失败：会话或消息标识缺失')
    return
  }
  try {
    await api.deleteMessagesAfter(store.sessionId, messageId, true)
  } catch (e) {
    ElMessage.error('替换失败：' + (e && e.message ? e.message : e))
    return
  }
  // 本地同步裁剪（含被改写的那条及其之后）：后端 DB 已是真源，这里只为界面一致
  const idx = store.messages.findIndex((m) => m.id === messageId)
  if (idx >= 0) {
    // 先收集将被裁掉的消息对应的反馈高亮，再 splice —— 只清这些，不动保留消息的状态
    const dropped = store.messages.slice(idx).filter((m) => m.roundId)
    store.messages.splice(idx)
    for (const m of dropped) {
      delete store.feedbackMap[m.roundId]
      delete store.slangFeedbackMap[m.roundId]
    }
  }
  await send(t)
  await refreshContext()
}

/* 重新生成：删掉本轮提问与其回答（后端顺带重置该会话对话状态），以同一提问重发。
 * 复用「替换该轮」的裁剪逻辑（inclusive=true，提问一并删除后重发，界面观感一致）。
 * 仅支持最后一轮：中间轮重新生成会连后续对话一起丢，入口在 UI 上已限制。 */
export async function regenerateRound(msg) {
  if (store.sending) { ElMessage.warning('正在生成回答，请稍后再发送'); return }
  const list = store.messages
  const last = list[list.length - 1]
  const isSame = last === msg || (!!msg.roundId && last && last.roundId === msg.roundId)
  if (!isSame || msg.role !== 'assistant') {
    ElMessage.warning('只能重新生成最后一轮回答')
    return
  }
  let userMsg = null
  for (let i = list.length - 2; i >= 0; i--) {
    if (list[i].role === 'user') { userMsg = list[i]; break }
  }
  if (!userMsg || !userMsg.id) {
    ElMessage.error('重新生成失败：未找到对应的提问')
    return
  }
  await replaceRound(userMsg.id, userMsg.content)
}

/* ============ 结构化澄清确认（WorkBuddy 式表单提交） ============
 * 用户在澄清卡片上确认指标/维度/时间后提交：
 * - 纯选择：把选择与回传上下文交给后端，走确定性合并后自动续跑（不重新抽取）；
 * - 带"补充说明"自由文本：合成为一句自然语言走常规链路，由 LLM 理解补充语义
 *   （避免自由文本被确定性路径忽略）。
 */

/* 选项值 -> 展示文案（优先用候选 label；自定义输入原样展示） */
function clarifyOptionLabel(field, value) {
  const hit = ((field && field.options) || []).find((o) => o.value === value)
  return (hit && hit.label) || value
}

/* 表单选择 -> 提交给后端的结构（时间项统一取第一个值） */
export function buildClarifySelection(clarify, answers, extra) {
  const fields = (clarify && clarify.fields) || []
  const pick = (key) => {
    const v = answers[key]
    return Array.isArray(v) ? v.filter((x) => x != null && String(x).trim()) : (v ? [v] : [])
  }
  const metrics = pick('metrics')
  const dimensions = pick('dimensions')
  const time = pick('time_range')
  return {
    metrics,
    dimensions,
    time_range: time[0] || '',
    extra: (extra || '').trim(),
    context: (clarify && clarify.context) || {},
    /* 供前端本地展示"用户确认了什么" */
    _labels: {
      metrics: metrics.map((v) => clarifyOptionLabel(fields[0], v)),
      dimensions: dimensions.map((v) => clarifyOptionLabel(fields[1], v)),
      time_range: time[0] ? clarifyOptionLabel(fields[2], time[0]) : ''
    }
  }
}

/* 用户确认内容 -> 会话展示文案（也是提交给后端的 message） */
export function clarifySummaryText(selection) {
  const labels = selection._labels || {}
  const parts = []
  if (labels.metrics && labels.metrics.length) parts.push(`指标=${labels.metrics.join('、')}`)
  if (labels.dimensions && labels.dimensions.length) parts.push(`维度=${labels.dimensions.join('、')}`)
  if (labels.time_range) parts.push(`时间=${labels.time_range}`)
  if (parts.length) return `已确认：${parts.join('；')}`
  return '已确认查询要素'
}

/* 提交某条助手消息上的澄清卡片（msg 为该助手消息对象） */
export async function submitClarification(msg, selection) {
  if (!msg || !msg.clarify || store.sending) return

  // FR5 空数据时间窗重定向：二选一确认，直接采用后端算好的 adjusted 窗口续跑
  if (selection.time_redirect) {
    const label = (selection._labels && selection._labels.time_range) || '数据截至日'
    const text = `改查最新数据（${label}）`
    msg.clarifySubmitted = true
    msg.clarifyConfirm = { time_range: label }
    const payload = { ...selection }
    delete payload._labels
    await send(text, { clarifySelection: payload })
    return
  }

  const text = clarifySummaryText(selection)
  const extra = (selection.extra || '').trim()
  // 卡片转为只读已确认态（记录用户提交内容，便于回看）
  msg.clarifySubmitted = true
  msg.clarifyConfirm = {
    metrics: (selection._labels && selection._labels.metrics) || [],
    dimensions: (selection._labels && selection._labels.dimensions) || [],
    time_range: selection._labels ? selection._labels.time_range : '',
    extra
  }
  if (extra) {
    // 含自由补充说明：合成一句话走常规链路（由 LLM 抽取），保证补充语义被理解
    await send(`${text}；补充说明：${extra}`)
  } else {
    const payload = { ...selection }
    delete payload._labels
    await send(text, { clarifySelection: payload })
  }
}

/* ---- 后端 node_trace → 前端展示结构 ---- */
function mergeTrace(prev, finalTrace) {
  // 按 stage 合并：流式期间的实时 step + final 带来完整信息
  const byStage = {}
  // 先按 prev 顺序保留
  for (const step of prev || []) {
    byStage[step.stage] = step
    // 清理旧字段，标记不再 live
    step.live = false
  }
  const out = []
  for (const f of finalTrace) {
    const exist = byStage[f.stage]
    if (exist) {
      out.push({
        ...exist,
        status: f.status || exist.status,
        elapsed_ms: f.elapsed_ms != null ? f.elapsed_ms : exist.elapsed_ms,
        detail: f.detail || exist.detail,
        json: f.json || exist.json,
        label: f.label || exist.label,
        live: false
      })
    } else {
      out.push({ ...f, live: false })
    }
  }
  // 流式里有但 final 没有的 step（罕见，但保留）
  const finalStages = new Set(finalTrace.map(x => x.stage))
  for (const step of prev || []) {
    if (!finalStages.has(step.stage)) {
      step.live = false
      if (step.status === 'pending') step.status = 'ok'
      out.push(step)
    }
  }
  return out
}
function stagesFromNodeTrace(t) {
  return [
    t.intent ? 1 : 0,
    t.query_elements || t.retrieved_docs || t.validation ? 1 : 0,
    t.dsl || t.query_result ? 1 : 0,
    t.analysis || t.final_answer ? 1 : 0
  ]
}

function fmtElements(el) {
  if (!el || typeof el !== 'object') return ''
  const parts = []
  const metrics = el.metrics || []
  if (metrics.length) parts.push('指标 ' + metrics.map((m) => m.alias || m.metric_id || m.id || m).join('、'))
  const dims = el.dimensions || []
  if (dims.length) parts.push('维度 ' + dims.map((d) => d.dim_id || d.alias || d).join('、'))
  if (el.time && (el.time.start || el.time.end)) {
    parts.push(`时间 ${el.time.start || ''}~${el.time.end || ''}${el.time.grain ? ' ' + el.time.grain : ''}`)
  }
  const filters = el.filters || []
  if (filters.length) parts.push('过滤 ' + filters.map((f) => `${f.dim || f.dim_id}=${f.value || f.values}`).join('、'))
  const ratios = el.ratios || []
  if (ratios.length) parts.push('同环比 ' + ratios.join('/'))
  return parts.join(' · ')
}

/* 黑话改写徽标数据：本轮是否发生黑话替换/替换了哪些（node_trace.query_rewrite） */
function rewriteMetaFromTrace(t, original) {
  const rw = (t && t.query_rewrite) || null
  if (!rw) return null
  const hits = Array.isArray(rw.hits) ? rw.hits : []
  const orig = rw.original || original || ''
  const rewritten = rw.rewritten || ''
  if (!hits.length && (!rewritten || rewritten === orig)) return null
  return { original: orig, rewritten, hits, degraded: !!rw.degraded }
}

function traceFromNodeTrace(t) {
  // node_span[name]=node.<langgraph_node>，剥掉前缀得到 stage
  const spans = (t.otel && Array.isArray(t.otel.node_spans)) ? t.otel.node_spans : []
  const durByNode = {}
  const errByNode = {}
  for (const s of spans) {
    const n = (s.name || '').replace(/^node\./, '')
    if (n) {
      durByNode[n] = s.duration_ms
      errByNode[n] = (s.status_code && String(s.status_code).toUpperCase() === 'ERROR')
    }
  }
  // 解析逻辑 → LangGraph 节点名（来自 lib.graph 的节点定义）
  // 为老逻辑 final 也能构建一份每节点的 step 名/标签/细节
  const out = []
  const mkStep = (stage, label, ok, detail, json) => {
    if (!ok) return
    out.push({
      stage, label,
      status: errByNode[stage] ? 'error' : 'ok',
      elapsed_ms: durByNode[stage] != null ? durByNode[stage] : null,
      detail: typeof detail === 'string' ? detail : '',
      json
    })
  }
  // 黑话改写（链路最前端：优先翻译黑话 / 口语归一，命中明细一并展示）
  if (t.query_rewrite) {
    const rw = t.query_rewrite
    const hits = Array.isArray(rw.hits) ? rw.hits : []
    const desc = hits.length
      ? hits.slice(0, 6).map(h => `${h.term}→${h.standard || (h.expand || []).join('/') || '删除'}`).join('、')
      : '未命中黑话'
    mkStep('rewrite_query', NODE_LABEL.rewrite_query, true, desc, rw)
  }
  if (t.intent) mkStep('classify_intent', NODE_LABEL.classify_intent, true, `意图：${t.intent}`)
  if (t.query_elements) mkStep('extract_elements', NODE_LABEL.extract_elements, true, fmtElements(t.query_elements) || '要素解析完成', t.query_elements)
  if (t.retrieved_docs) mkStep('retrieve_knowledge', NODE_LABEL.retrieve_knowledge, true, String(t.retrieved_docs))
  if (t.validation) {
    const ok = t.validation.valid !== false && t.validation.ok !== false
    mkStep('validate_elements', NODE_LABEL.validate_elements, true, t.validation.message || (ok ? '校验通过' : '校验未通过'), t.validation)
  }
  if (t.confirmation) mkStep('ask_clarification', NODE_LABEL.ask_clarification, true, t.confirmation.question || '需要补充信息', t.confirmation)
  if (t.dsl) mkStep('build_dsl', NODE_LABEL.build_dsl, true, '指标 DSL 生成完成', t.dsl)
  if (t.query_result) {
    const qr = t.query_result
    const rows = (qr.rows || []).length
    const status = qr.status || (rows ? 'ok' : 'empty')
    out.push({
      stage: 'execute_query', label: NODE_LABEL.execute_query,
      status: status === 'ok' ? 'ok' : status === 'empty' ? 'empty' : 'error',
      elapsed_ms: durByNode['execute_query'] != null ? durByNode['execute_query'] : null,
      detail: qr.message || `返回 ${rows} 行`,
      json: { status: qr.status, columns: qr.columns, rows: rows, message: qr.message }
    })
  }
  if (t.analysis) mkStep('analyze_data', NODE_LABEL.analyze_data, true, typeof t.analysis === 'string' ? t.analysis : '分析完成', t.analysis)
  if (t.skills && t.skills.length) {
    out.push({ stage: 'skills', label: '追加技能', status: 'ok', detail: t.skills.join('、'), json: t.skills })
  }
  if (t.error) out.push({ stage: 'error', label: '异常', status: 'error', detail: String(t.error) })
  return out
}

function vizFromNodeTrace(t) {
  const qr = t.query_result
  if (!qr || !qr.columns || !qr.columns.length || !qr.rows || !qr.rows.length) return []
  const cols = qr.columns
  const numeric = cols
    .map((c, i) => ({ name: c, idx: i }))
    .filter(({ idx }) => idx > 0 && qr.rows.every((r) => typeof r[idx] === 'number' && isFinite(r[idx])))
  if (!numeric.length) return []
  const x = qr.rows.map((r) => String(r[0] == null ? '' : r[0]))
  const series = numeric.map((c) => ({ name: c.name, data: qr.rows.map((r) => r[c.idx]) }))
  const isTime = /日期|时间|date|\d{4}-\d{2}/.test(x[0] || '') || x.length > 6
  const viz = [{ type: isTime ? 'line' : 'bar', title: series[0].name, x, series }]
  // 分布直方图（FR2）：trace.analysis.distribution 由后端 Python 分桶产出，前端只画
  const dist = t.analysis && t.analysis.distribution
  if (dist && Array.isArray(dist.bins) && dist.bins.length) {
    viz.push({
      type: 'bar',
      title: `${dist.metric} 分布`,
      x: dist.bins.map((b) => b.label),
      series: [{ name: '数量', data: dist.bins.map((b) => b.count) }]
    })
  }
  return viz
}

/* 从 node_trace 提取「本轮执行的 SQL + 结果元信息」（查 SQL 面板用）。
 * execute_query 节点的产出 query_result 已含 sql/engine/models/row_count/elapsed_ms/error，
 * 后端在 final 事件与历史消息 node_trace 中均透传，这里只做确定性抽取。 */
function sqlInfoFromNodeTrace(t) {
  const qr = t && t.query_result
  if (!qr || !qr.sql) return null
  const rows = qr.rows || []
  const rowCount = qr.row_count != null ? qr.row_count : rows.length
  return {
    sql: String(qr.sql),
    engine: qr.engine || '',
    models: Array.isArray(qr.models) ? qr.models : [],
    rowCount: rowCount != null ? rowCount : null,
    elapsedMs: qr.elapsed_ms != null ? qr.elapsed_ms : null,
    error: qr.error ? String(qr.error) : '',
    // P2-5 结果元信息：数据截至时间 + 口径说明（这次结果用了什么定义/数据源/时间窗）
    dataAsOf: qr.data_as_of != null ? String(qr.data_as_of) : '',
    // P2-1 查询结果缓存：本次结果是否来自缓存命中
    cached: qr.cached === true,
    // FR12：口径说明（公式 / 时间窗 / 过滤 / 同环比 / 版本号）
    caliber: normCaliber(qr.caliber)
  }
}

/* ---- 服务端历史消息 → 前端消息结构 ---- */
function msgFromServer(m) {
  const t = m.node_trace || {}
  return {
    /* 服务端消息 id：用户消息靠它做「替换该轮」的裁剪锚点；助手消息同时映射为 roundId */
    id: m.id || null,
    role: m.role,
    content: m.content,
    ts: m.created_at,
    pending: false,
    failed: !!t.error,
    stages: stagesFromNodeTrace(t),
    intentMeta: t.intent ? { intent: t.intent, confidence: null, source: '' } : null,
    viz: vizFromNodeTrace(t),
    sqlInfo: sqlInfoFromNodeTrace(t),
    trace: traceFromNodeTrace(t),
    rewriteMeta: rewriteMetaFromTrace(t, ''),
    timeExtract: null,
    interpret: null,
    /* 观测信息（历史消息同样可展示：耗时 / token / span 数 / trace_id） */
    otel: t.otel || null,
    elapsedMs: t.elapsed_ms != null ? t.elapsed_ms : null,
    tokens: t.total_tokens != null
      ? { prompt: t.prompt_tokens || 0, completion: t.completion_tokens || 0, total: t.total_tokens }
      : null,
    /* 历史消息同样还原澄清卡片：回看旧会话时可继续在卡片上确认并续跑 */
    clarify: (t.confirmation && t.confirmation.clarification) || null,
    roundId: m.role === 'assistant' ? m.id : null,
    sessionId: m.session_id || null
  }
}

/* ============ 运行观测 ============
 * 两级入口语义：
 * - 对话上方「观测」按钮（ObsPanel 抽屉）：当前对话的观测内容（传 sessionId；无会话时全局）
 * - 侧栏「运行观测」菜单（Observability 专页）：全局观测结果，支持一键「跟踪当前对话」过滤 */
export async function refreshObs(sessionId) {
  store.obsLoading = true
  store.obsError = ''
  const params = sessionId ? { session_id: sessionId } : {}
  try {
    const [summary, traces] = await Promise.all([
      api.fetchObsSummary(params),
      api.fetchObsTraces(50)
    ])
    // traces 为全局最近记录；会话模式下按 session_id 过滤展示（后端 /traces 不带过滤参数）
    const sessionTraces = sessionId
      ? traces.filter((t) => t.session_id === sessionId)
      : traces
    store.obs = { summary, traces: sessionTraces, sessionId: sessionId || null }
  } catch (e) {
    store.obsError = e.message
    store.obs = null
  } finally {
    store.obsLoading = false
  }
}

/* ---- 观测专页：汇总 + 明细（过滤/分页）+ 单轮下钻 ---- */
function _obsFilterParams() {
  const f = store.obsPage.filters
  const p = {}
  if (f.session_id) p.session_id = f.session_id
  if (f.intent) p.intent = f.intent
  if (f.has_error !== '' && f.has_error != null) p.has_error = f.has_error
  if (f.keyword) p.keyword = f.keyword
  return p
}

export async function loadObsPage() {
  const p = store.obsPage
  p.loading = true
  p.error = ''
  const params = _obsFilterParams()
  try {
    const [summary, rounds, spans] = await Promise.all([
      api.fetchObsSummary(params),
      api.fetchObsRounds({ ...params, page: p.page, page_size: p.pageSize }),
      api.fetchObsSpans({ session_id: p.filters.session_id || undefined, limit: 500 })
    ])
    p.summary = summary
    p.rounds = rounds.items || []
    p.total = rounds.total || 0
    p.spanStats = spans
    // FR11 成本 / 质量看板（自有错误处理，失败不影响上方主面板）
    loadObsCostQuality()
  } catch (e) {
    p.error = e.message
    p.summary = null
    p.rounds = []
    p.total = 0
    p.spanStats = null
  } finally {
    p.loading = false
  }
}

export function setObsFilter(patch) {
  Object.assign(store.obsPage.filters, patch)
  store.obsPage.page = 1
  loadObsPage()
}
export function setObsPage(page) {
  store.obsPage.page = page
  loadObsPage()
}
export function resetObsFilters() {
  store.obsPage.filters = { session_id: '', intent: '', has_error: '', keyword: '' }
  store.obsPage.page = 1
  loadObsPage()
}
export async function loadObsRoundDetail(sessionId, roundId) {
  const p = store.obsPage
  p.detailLoading = true
  p.detail = null
  try {
    p.detail = await api.fetchObsTraceDetail(sessionId, roundId)
  } catch (e) {
    p.detail = { error: e.message, session_id: sessionId, round_id: roundId, spans: [] }
  } finally {
    p.detailLoading = false
  }
}
export function closeObsDetail() {
  store.obsPage.detail = null
}

/* ---- 观测专页：FR11 成本 / 质量看板 ---- */
export async function loadObsCostQuality() {
  const p = store.obsPage
  try {
    const [cost, quality] = await Promise.all([
      api.fetchObsCost({ group_by: p.costGroupBy, days: p.costDays }),
      api.fetchObsQuality({ days: p.costDays })
    ])
    p.cost = cost
    p.quality = quality
  } catch (e) {
    p.cost = null
    p.quality = null
  }
}

export function setCostGroupBy(groupBy) {
  store.obsPage.costGroupBy = groupBy
  loadObsCostQuality()
}

export function setCostDays(days) {
  store.obsPage.costDays = days
  loadObsCostQuality()
}

/* ============ 语义查询（SQL 编译 / 真实执行） ============ */
export async function loadSemanticMeta(force = false) {
  const s = store.semExp
  if (s.loadingMeta) return
  if (!force && s.metrics.length && s.dimensions.length && s.manifest) return
  s.loadingMeta = true
  s.error = ''
  try {
    const [metrics, dimensions, manifest, health] = await Promise.all([
      api.semanticMetrics(),
      api.semanticDimensions(),
      api.semanticManifest(),
      api.semanticHealth()
    ])
    s.metrics = metrics || []
    s.dimensions = dimensions || []
    s.manifest = manifest || null
    s.health = health || null
  } catch (e) {
    s.error = e.message || String(e)
  } finally {
    s.loadingMeta = false
  }
}

function _semanticPayload(s = store.semExp) {
  const f = s.form
  const payload = {
    metrics: [...f.metrics],
    group_by: [...f.groupBy],
    limit: Number(f.limit) || undefined
  }
  if (f.start || f.end || f.granularity) {
    payload.time_range = {
      start: f.start || null,
      end: f.end || null,
      granularity: f.granularity || 'day'
    }
  }
  // ---- 维度过滤：丢掉空行/空值，避免后端 422 ----
  const filters = (f.filters || [])
    .map(x => ({
      field: String(x.field || '').trim(),
      operator: x.operator || 'in',
      values: String(x.values ?? '')
        .split(',')
        .map(v => v.trim())
        .filter(Boolean)
    }))
    .filter(x => x.field && x.values.length)
  if (filters.length) payload.filters = filters
  // ---- 同环比：period_comparison + 类型（FR1 可并存，如 yoy + mom）----
  if (f.compareOn && f.compareTypes.length) {
    payload.period_comparison = true
    payload.period_comparison_type = f.compareTypes[0]
    payload.period_comparison_types = [...f.compareTypes]
  }
  // ---- 排名范围（FR1）：组内排名按时间周期分别排名 ----
  if (f.advRank && f.rankScope === 'group') payload.rank_scope = 'group'
  // ---- 二次分析：聚合覆盖（日均/峰值）/ 占比 / 排名，作用于已选全部指标 ----
  const advanced = []
  for (const name of f.metrics) {
    const item = { metric_name: name }
    let on = false
    if (f.advAgg) { item.agg = f.advAgg; on = true }
    if (f.advProportion) { item.proportion = true; on = true }
    if (f.advRank) { item.orderby = 'desc'; on = true }
    if (on) advanced.push(item)
  }
  if (advanced.length) payload.advanced = { metric_result: advanced }
  return payload
}

export async function runSemanticCompile() {
  const s = store.semExp
  if (!s.form.metrics.length) {
    s.error = '请至少选择一个指标'
    return
  }
  s.loadingRun = 'compile'
  s.error = ''
  s.run = null
  try {
    s.compile = await api.semanticCompile(_semanticPayload(s))
  } catch (e) {
    s.error = e.message || String(e)
    s.compile = null
  } finally {
    s.loadingRun = false
  }
}

export async function runSemanticQuery() {
  const s = store.semExp
  if (!s.form.metrics.length) {
    s.error = '请至少选择一个指标'
    return
  }
  s.loadingRun = 'query'
  s.error = ''
  s.run = null
  const t0 = performance.now()
  try {
    const res = await api.semanticQuery(_semanticPayload(s))
    const elapsed = Math.round(performance.now() - t0)
    const cols = res.columns || []
    const rawRows = res.rows || res.data || []
    // 后端返回 list-of-lists（兼容 schema 序列化），el-table 期望 list-of-dicts
    // → 标准化成 [{ col1: v1, col2: v2, ... }]，兼容 el-table 的 :prop + :formatter
    const rows = Array.isArray(rawRows) && rawRows.length > 0 && !Array.isArray(rawRows[0])
      ? rawRows
      : rawRows.map(arr => {
          const obj = {}
          cols.forEach((c, i) => { obj[c] = arr[i] })
          return obj
        })
    s.run = {
      columns: cols,
      rows,
      row_count: rows.length,
      sql: res.sql || (s.compile && s.compile.sql) || '',
      elapsed_ms: typeof res.elapsed_ms === 'number' ? res.elapsed_ms : elapsed,
      error: null,
      mock: !!res.mock,
      engine: res.engine || 'metricflow',
      // P2-1 缓存命中标记 + P2-5/FR12 结果元信息（口径说明 / 数据截至）
      cached: !!res.cached,
      caliber: normCaliber(res.caliber),
      data_as_of: res.data_as_of || ''
    }
  } catch (e) {
    s.run = { columns: [], rows: [], row_count: 0, sql: '', elapsed_ms: 0, error: e.message || String(e), mock: false, engine: '' }
    s.error = e.message || String(e)
  } finally {
    s.loadingRun = false
  }
}

export function resetSemanticForm() {
  const f = store.semExp.form
  f.metrics = []
  f.groupBy = []
  f.start = ''
  f.end = ''
  f.granularity = 'day'
  f.limit = 50
  f.compareOn = false
  f.compareTypes = ['yoy']
  f.filters = []
  f.advAgg = ''
  f.advProportion = false
  f.advRank = false
  f.rankScope = 'global'
  store.semExp.compile = null
  store.semExp.run = null
  store.semExp.error = ''
}

/* ============ L2 用户偏好（个人记忆） ============ */
/* 偏好键的中文名（常见键映射；未知键原样展示） */
const PREF_KEY_LABEL = {
  granularity: '默认时间粒度',
  dimensions: '常用维度',
  metrics: '常用指标',
  time_range: '默认时间范围',
  chart_type: '偏好图表类型',
  unit: '偏好单位'
}
export function prefKeyLabel(k) { return PREF_KEY_LABEL[k] || k }

export async function loadPreferences(status) {
  const p = store.prefs
  /* 只接受 'active' | 'archived'。兜住误把 DOM 事件当参数传进来的情况
     （`@click="loadPreferences"` 会把 PointerEvent 当第一个实参 → ?status=[object PointerEvent] → 422）。 */
  const st = (status === 'active' || status === 'archived') ? status : undefined
  p.loading = true
  p.error = ''
  try {
    const [items, flag] = await Promise.all([
      api.listPreferences(st),
      api.getMemoryDisabled().catch(() => null)
    ])
    p.items = items || []
    if (flag) p.disabled = !!flag.disabled
  } catch (e) {
    p.error = e.message || String(e)
    p.items = []
  } finally {
    p.loading = false
  }
}

export async function upsertPreference(key, value, source = 'user') {
  store.prefs.saving = true
  try {
    const r = await api.upsertPreference({
      key, value, source, confidence: source === 'user' ? 1.0 : 0.6, status: 'active'
    })
    await loadPreferences()
    return r
  } finally {
    store.prefs.saving = false
  }
}

export async function deletePreference(key) {
  const r = await api.deletePreference(key)
  await loadPreferences()
  return r
}

export async function clearPreferences() {
  const r = await api.clearPreferences()
  await loadPreferences()
  return r
}

/* 记忆注入开关：关闭后 L1 历史装配 + L2 偏好注入同步失效（后端 runtime.memory_disabled） */
export async function toggleMemoryDisabled(disabled) {
  store.prefs.saving = true
  try {
    const r = await api.setMemoryDisabled(disabled)
    store.prefs.disabled = !!(r && r.disabled)
    return r
  } finally {
    store.prefs.saving = false
  }
}

/* ============ 记忆管理（L1 会话 / L2 用户 / L3 长期记忆） ============ */
/* L3 领域知识的类型与状态中文名（对齐后端 domain_memories.type / status） */
export const L3_TYPE_LABEL = {
  rule: '规则',
  glossary: '术语',
  correction: '纠错',
  few_shot: '点赞样本镜像'
}
export const L3_STATUS_LABEL = { pending: '待审核', approved: '已通过', rejected: '已驳回' }
export function l3TypeLabel(t) { return L3_TYPE_LABEL[t] || t || '-' }
export function l3StatusLabel(s) { return L3_STATUS_LABEL[s] || s || '-' }

/* 当前用户是否有记忆写权限（L3 增删改 / 审核、跨用户删偏好） */
export function canWriteMemory() { return hasPerm('memory:write') }
/* 当前用户是否有 Few-Shot 写权限 */
export function canWriteFewShot() { return hasPerm('fewshot:write') }

/* 一次把统计 + 当前标签页的数据拉起来（切页 / 刷新共用） */
export async function loadMemoryPage() {
  await Promise.all([loadMemoryStats(), loadMemoryTab(store.memoryPage.tab)])
}

export async function loadMemoryStats() {
  try {
    store.memoryPage.stats = await api.memoryStats()
  } catch (e) {
    /* 统计失败不影响列表：错误由各标签页自己提示 */
    store.memoryPage.stats = null
  }
}

export function setMemoryTab(tab) {
  store.memoryPage.tab = tab
  return loadMemoryTab(tab)
}

export async function loadMemoryTab(tab) {
  if (tab === 'l1') return loadL1Summaries()
  if (tab === 'l2') return loadL2Preferences()
  if (tab === 'l3') return loadL3Memories()
}

/* ---- L1 会话记忆 ---- */
export async function loadL1Summaries(keyword) {
  const s = store.memoryPage.l1
  /* 只接受字符串：兜住把 DOM 事件裸绑进 @click 的情况（会产生 ?keyword=[object PointerEvent]） */
  if (typeof keyword === 'string') s.keyword = keyword
  s.loading = true
  store.memoryPage.error = ''
  try {
    s.items = await api.listMemorySummaries(s.keyword || '', 200)
  } catch (e) {
    store.memoryPage.error = e.message || String(e)
    s.items = []
  } finally {
    s.loading = false
  }
}

export async function clearL1Memory(sessionId) {
  const r = await api.clearSessionMemory(sessionId)
  await Promise.all([loadL1Summaries(), loadMemoryStats()])
  return r
}

/* ---- L2 用户记忆（全平台） ---- */
export async function loadL2Preferences() {
  const s = store.memoryPage.l2
  s.loading = true
  store.memoryPage.error = ''
  try {
    s.items = await api.listAllPreferences({ ...s.filters, limit: 300 })
  } catch (e) {
    store.memoryPage.error = e.message || String(e)
    s.items = []
  } finally {
    s.loading = false
  }
}

export function setL2Filter(patch) {
  Object.assign(store.memoryPage.l2.filters, patch || {})
  return loadL2Preferences()
}

export async function removeL2Preference(id) {
  const r = await api.deletePreferenceById(id)
  await Promise.all([loadL2Preferences(), loadMemoryStats()])
  return r
}

/* ---- L3 长期记忆（领域知识） ---- */
export async function loadL3Memories() {
  const s = store.memoryPage.l3
  s.loading = true
  store.memoryPage.error = ''
  try {
    const f = s.filters
    s.items = await api.listDomainMemories({
      type: f.type || undefined,
      status: f.status || undefined,
      keyword: f.keyword || undefined,
      include_few_shot: f.include_few_shot ? undefined : 'false',
      limit: 200
    })
  } catch (e) {
    store.memoryPage.error = e.message || String(e)
    s.items = []
  } finally {
    s.loading = false
  }
}

export function setL3Filter(patch) {
  Object.assign(store.memoryPage.l3.filters, patch || {})
  return loadL3Memories()
}

export async function createL3Memory(body) {
  store.memoryPage.saving = true
  try {
    const r = await api.createDomainMemory(body)
    await Promise.all([loadL3Memories(), loadMemoryStats()])
    return r
  } finally {
    store.memoryPage.saving = false
  }
}

export async function updateL3Memory(id, body) {
  store.memoryPage.saving = true
  try {
    const r = await api.updateDomainMemory(id, body)
    await Promise.all([loadL3Memories(), loadMemoryStats()])
    return r
  } finally {
    store.memoryPage.saving = false
  }
}

export async function removeL3Memory(id) {
  const r = await api.deleteDomainMemory(id)
  await Promise.all([loadL3Memories(), loadMemoryStats()])
  return r
}

/* ============ Few-Shot 样本管理 ============ */
export const FEWSHOT_SOURCE_LABEL = { like: '点赞沉淀', manual: '手工录入' }
export function fewShotSourceLabel(s) { return FEWSHOT_SOURCE_LABEL[s] || s || '-' }

export async function loadFewShotPage() {
  await Promise.all([loadFewShotStats(), loadFewShotFacets(), loadFewShots()])
}

export async function loadFewShotStats() {
  try {
    store.fewshotPage.stats = await api.fewShotStats()
  } catch (e) {
    store.fewshotPage.stats = null
  }
}

export async function loadFewShotFacets() {
  try {
    store.fewshotPage.facets = await api.fewShotFacets()
  } catch (e) {
    /* 下拉选项拉取失败不阻断列表 */
  }
}

export async function loadFewShots() {
  const p = store.fewshotPage
  p.loading = true
  p.error = ''
  try {
    const f = p.filters
    const res = await api.listFewShots({
      category: f.category || undefined,
      intent: f.intent || undefined,
      source: f.source || undefined,
      status: f.status || undefined,
      keyword: f.keyword || undefined,
      limit: p.pageSize,
      offset: (p.page - 1) * p.pageSize
    })
    p.items = res.items || []
    p.total = res.total || 0
  } catch (e) {
    p.error = e.message || String(e)
    p.items = []
    p.total = 0
  } finally {
    p.loading = false
  }
}

/* 过滤条件变更后回到第 1 页（否则第 3 页过滤后可能落在空页上） */
export function setFewShotFilter(patch) {
  Object.assign(store.fewshotPage.filters, patch || {})
  store.fewshotPage.page = 1
  return loadFewShots()
}

export function setFewShotPage(page) {
  store.fewshotPage.page = page
  return loadFewShots()
}

export function resetFewShotFilters() {
  Object.assign(store.fewshotPage.filters, {
    category: '', intent: '', source: '', status: '', keyword: ''
  })
  store.fewshotPage.page = 1
  return loadFewShots()
}

/* 列表刷新统一用这个：统计 / 下拉选项 / 列表一起刷（改完样本后三处都会变） */
async function _refreshFewShotAll() {
  await Promise.all([loadFewShots(), loadFewShotStats(), loadFewShotFacets()])
}

export async function createFewShot(body) {
  store.fewshotPage.saving = true
  try {
    const r = await api.createFewShot(body)
    await _refreshFewShotAll()
    return r
  } finally {
    store.fewshotPage.saving = false
  }
}

export async function updateFewShot(id, body) {
  store.fewshotPage.saving = true
  try {
    const r = await api.updateFewShot(id, body)
    await _refreshFewShotAll()
    return r
  } finally {
    store.fewshotPage.saving = false
  }
}

export async function removeFewShot(id) {
  const r = await api.deleteFewShot(id)
  await _refreshFewShotAll()
  return r
}

/* 试召回：验证样本是否会被命中（与线上意图识别注入同一套召回实现） */
export function openFewShotRecall(query = '') {
  const r = store.fewshotPage.recall
  r.visible = true
  if (query) r.query = query
  r.result = null
  r.error = ''
}

export function closeFewShotRecall() {
  store.fewshotPage.recall.visible = false
}

export async function runFewShotRecall() {
  const r = store.fewshotPage.recall
  const q = (r.query || '').trim()
  if (!q) return null
  r.loading = true
  r.error = ''
  try {
    r.result = await api.recallFewShots(q, r.topK || 3)
    return r.result
  } catch (e) {
    r.error = e.message || String(e)
    r.result = null
    return null
  } finally {
    r.loading = false
  }
}

export async function importFewShots(items, category = 'intention', overwrite = false) {
  store.fewshotPage.saving = true
  try {
    const r = await api.importFewShots(items, category, overwrite)
    await _refreshFewShotAll()
    return r
  } finally {
    store.fewshotPage.saving = false
  }
}

/* 导出：后端返回 JSON（含 FR13 watermark 块），这里只取回 payload，落盘交给预览弹窗
 * （先预览 + 屏幕水印，再点「下载」落盘；避免新窗口下载丢 Authorization 头）。 */
export async function exportFewShots() {
  return api.exportFewShots(store.fewshotPage.filters.status || undefined)
}

/* 统一的 JSON 落盘（预览弹窗复用；导出件已由后端写入 watermark 块） */
export function downloadJson(payload, filename) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/* ============ 三级黑话词典管理（行业级 / 公司级 / 用户级） ============ */
export const SLANG_SCOPE_LABEL = { industry: '行业级', tenant: '公司级', user: '用户级' }
export const SLANG_TYPE_LABEL = {
  metric: '指标', dimension: '维度', dim_value: '枚举值', term: '术语', stop: '停用词'
}
export const SLANG_STATUS_LABEL = { draft: '草稿', published: '已发布', disabled: '已停用' }
export const SLANG_MODE_LABEL = { expand: '扩展', replace: '替换', stop: '删除' }
export const SLANG_SOURCE_LABEL = { builtin: '平台内置', manual: '手工录入', imported: '批量导入' }
export const slangScopeLabel = (s) => SLANG_SCOPE_LABEL[s] || s || '-'
export const slangTypeLabel = (t) => SLANG_TYPE_LABEL[t] || t || '-'
export const slangStatusLabel = (s) => SLANG_STATUS_LABEL[s] || s || '-'
export const slangModeLabel = (m) => SLANG_MODE_LABEL[m] || m || '-'
export const slangSourceLabel = (s) => SLANG_SOURCE_LABEL[s] || s || '-'

/* 权限：读/写/审分离；非 admin 只能写本人用户级（后端另有 scope 守卫兜底） */
export function canWriteSlang() { return hasPerm('slang:write') }
export function canReviewSlang() { return hasPerm('slang:review') }

export async function loadSlangPage() {
  await Promise.all([loadSlangStats(), loadSlangFacets(), loadSlang()])
}

export async function loadSlangStats() {
  try {
    store.slangPage.stats = await api.slangStats()
  } catch (e) {
    store.slangPage.stats = null
  }
}

export async function loadSlangFacets() {
  try {
    store.slangPage.facets = await api.slangFacets()
  } catch (e) {
    /* 下拉选项拉取失败不阻断列表 */
  }
}

export async function loadSlang() {
  const p = store.slangPage
  p.loading = true
  p.error = ''
  try {
    const f = p.filters
    const res = await api.listSlang({
      scope: f.scope || undefined,
      entry_type: f.entry_type || undefined,
      status: f.status || undefined,
      source: f.source || undefined,
      keyword: f.keyword || undefined,
      limit: p.pageSize,
      offset: (p.page - 1) * p.pageSize
    })
    p.items = res.items || []
    p.total = res.total || 0
  } catch (e) {
    p.error = e.message || String(e)
    p.items = []
    p.total = 0
  } finally {
    p.loading = false
  }
}

export function setSlangFilter(patch) {
  Object.assign(store.slangPage.filters, patch || {})
  store.slangPage.page = 1
  return loadSlang()
}

export function setSlangPage(page) {
  store.slangPage.page = page
  return loadSlang()
}

export function resetSlangFilters() {
  Object.assign(store.slangPage.filters, {
    scope: '', entry_type: '', status: '', source: '', keyword: ''
  })
  store.slangPage.page = 1
  return loadSlang()
}

/* 改完词条后统计 / 下拉选项 / 列表一起刷（三处都会变） */
async function _refreshSlangAll() {
  await Promise.all([loadSlang(), loadSlangStats(), loadSlangFacets()])
}

export async function createSlang(body) {
  store.slangPage.saving = true
  try {
    const r = await api.createSlang(body)
    await _refreshSlangAll()
    return r
  } finally {
    store.slangPage.saving = false
  }
}

export async function updateSlang(id, body) {
  store.slangPage.saving = true
  try {
    const r = await api.updateSlang(id, body)
    await _refreshSlangAll()
    return r
  } finally {
    store.slangPage.saving = false
  }
}

export async function removeSlang(id) {
  const r = await api.deleteSlang(id)
  await _refreshSlangAll()
  return r
}

export async function publishSlang(id) {
  const r = await api.publishSlang(id)
  await _refreshSlangAll()
  return r
}

export async function disableSlang(id) {
  const r = await api.disableSlang(id)
  await _refreshSlangAll()
  return r
}

/* ---- 在线试改写（复用线上解析器：Redis 优先 + 最长优先 + 三模式） ---- */
export function openSlangPreview(query = '') {
  const v = store.slangPage.preview
  v.visible = true
  if (query) v.query = query
  v.result = null
  v.error = ''
}

export function closeSlangPreview() {
  store.slangPage.preview.visible = false
}

export async function runSlangPreview() {
  const v = store.slangPage.preview
  const q = (v.query || '').trim()
  if (!q) return null
  v.loading = true
  v.error = ''
  try {
    v.result = await api.previewSlang({ query: q, include_draft: !!v.includeDraft })
    return v.result
  } catch (e) {
    v.error = e.message || String(e)
    v.result = null
    return null
  } finally {
    v.loading = false
  }
}

/* ---- 版本历史 / 回滚 ---- */
export function openSlangVersions(item) {
  const v = store.slangPage.versions
  v.visible = true
  v.entryId = item.id
  v.term = item.term
  v.items = []
  v.error = ''
  return loadSlangVersions(item.id)
}

export function closeSlangVersions() {
  store.slangPage.versions.visible = false
}

export async function loadSlangVersions(id) {
  const v = store.slangPage.versions
  v.loading = true
  v.error = ''
  try {
    const res = await api.slangVersions(id)
    v.items = res.items || []
    return v.items
  } catch (e) {
    v.error = e.message || String(e)
    v.items = []
    return []
  } finally {
    v.loading = false
  }
}

export async function rollbackSlangVersion(id, version, remark = '') {
  const r = await api.rollbackSlang(id, version, remark)
  await _refreshSlangAll()
  await loadSlangVersions(id)
  return r
}

/* ---- 导入（可先 dry_run 预检）/ 导出 ---- */
export async function importSlang(entries, opts = {}) {
  store.slangPage.saving = true
  try {
    const payload = {
      entries,
      scope: opts.scope || 'user',
      status: opts.status || 'draft',
      overwrite: !!opts.overwrite,
      dry_run: !!opts.dryRun,
      source: 'imported'
    }
    const r = await api.importSlang(payload)
    if (!payload.dry_run) await _refreshSlangAll()
    return r
  } finally {
    store.slangPage.saving = false
  }
}

/* 导出：取回 JSON payload（含 FR13 watermark），落盘由预览弹窗统一处理 */
export async function exportSlang() {
  const f = store.slangPage.filters
  return api.exportSlang({
    scope: f.scope || undefined,
    entry_type: f.entry_type || undefined,
    status: f.status || undefined,
    source: f.source || undefined,
    keyword: f.keyword || undefined
  })
}

/* 命中率 / 误改写率看板聚合（近 days 天） */
export async function loadSlangMetrics(days = 30) {
  store.slangPage.metrics = null
  try {
    store.slangPage.metrics = await api.slangMetrics(days)
  } catch (e) {
    console.error('加载改写看板失败', e)
    store.slangPage.metrics = null
  }
  return store.slangPage.metrics
}

/* ============ 认证（登录 / 登出 / 令牌） ============ */
export async function login(username, password) {
  store.auth.loading = true
  store.auth.error = ''
  try {
    const res = await api.login(username, password)
    setToken(res.access_token)
    if (res.refresh_token) setRefreshToken(res.refresh_token)
    setAuthUser(res.user)
    store.auth.user = res.user
    // 登录后若当前视图对新角色不可见（如 viewer 停留在观测页），回落到对话页
    if (!canView(store.view)) store.view = 'chat'
    await loadSessions()
    return true
  } catch (e) {
    store.auth.error = e.message || '登录失败'
    return false
  } finally {
    store.auth.loading = false
  }
}

export async function ensureAuth() {
  // 无访问令牌但存有刷新令牌：先静默续期（页面久置后重开，access 已过期）
  if (!getToken() && getRefreshToken()) {
    await api.refreshSession()
  }
  if (!getToken()) { store.auth.user = null; return false }
  try {
    const me = await api.fetchMe()
    store.auth.user = me
    setAuthUser(me)
    // 恢复后的视图若对当前角色不可见，回落对话页
    if (!canView(store.view)) store.view = 'chat'
    return true
  } catch (e) {
    logout()
    return false
  }
}

export function logout() {
  clearAuth()
  store.auth.user = null
  store.view = 'chat'
  store.sessionId = null
  store.sessions = []
  store.messages = []
}

/* ============ FR13 数据权限标记（T198f） ============
 * 拉当前用户的有效权限上下文，供「指标管理 / 语义查询」把受限的指标与维度标记出来。
 * 失败静默为 null —— 权限标记只是提示，绝不该阻断任何页面的正常渲染。 */

export async function loadPermissionContext(force = false) {
  if (store.permCtx && !force) return store.permCtx
  try {
    store.permCtx = await api.myPermissionContext()
  } catch (e) {
    store.permCtx = null
  }
  return store.permCtx
}

/* 某**指标 key** 是否对当前用户受限。
 * 语义与后端 policy_loader / filter_metrics 完全一致：无白名单记录（unrestricted_metrics）= 不限制；
 * 有白名单且 key 不在其中 = 受限（enforce 下取数会被剔除）。mode=off 一律不标记。 */
export function metricKeyRestricted(metricKey) {
  const c = store.permCtx
  if (!c || c.mode === 'off' || c.unrestricted_metrics) return false
  const key = String(metricKey || '')
  if (!key) return false
  return !(c.allowed_metrics || []).includes(key)
}

/* 某维度/列是否被 deny（enforce 下会从结果集**硬剔除**，与脱敏不同） */
export function dimensionDenied(name) {
  const c = store.permCtx
  if (!c || c.mode === 'off') return false
  return (c.denied_dimensions || []).includes(String(name || ''))
}

/* 某维度的行权限允许值；无行权限返回 null（= 不限） */
export function rowScopeValues(name) {
  const c = store.permCtx
  if (!c) return null
  const vals = (c.row_scope_values || {})[String(name || '')]
  return vals && vals.length ? vals : null
}

/* ============ 管理端（用户 / 租户 / 角色；仅 admin） ============ */
export async function loadAdminMeta() {
  const a = store.admin
  a.loading = true
  a.error = ''
  try {
    const [users, tenants, roles] = await Promise.all([
      api.listUsers(a.tenantFilter || undefined),
      api.listTenants(),
      api.fetchRoles()
    ])
    a.users = users || []
    a.tenants = tenants || []
    a.roles = roles || []
  } catch (e) {
    a.error = e.message || String(e)
  } finally {
    a.loading = false
  }
}
export function setUserTenantFilter(tid) {
  store.admin.tenantFilter = tid || ''
  loadAdminMeta()
}
export async function createAdminUser(body) {
  const r = await api.createUser(body)
  await loadAdminMeta()
  return r
}
export async function updateAdminUser(id, body) {
  const r = await api.updateUser(id, body)
  await loadAdminMeta()
  return r
}
export async function removeAdminUser(id) {
  const r = await api.deleteUser(id)
  await loadAdminMeta()
  return r
}
export async function resetAdminUserPassword(id, pw) {
  return api.resetUserPassword(id, pw)
}
export async function createAdminTenant(body) {
  const r = await api.createTenant(body)
  await loadAdminMeta()
  return r
}
export async function updateAdminTenant(id, body) {
  const r = await api.updateTenant(id, body)
  await loadAdminMeta()
  return r
}
export async function removeAdminTenant(id) {
  const r = await api.deleteTenant(id)
  await loadAdminMeta()
  return r
}
/* ---- 角色管理（自定义角色增删改 + 权限点勾选） ---- */
export async function fetchAdminPermissionCatalog() {
  return api.fetchPermissionCatalog()
}
export async function createAdminRole(body) {
  const r = await api.createRole(body)
  await loadAdminMeta()
  return r
}
export async function updateAdminRole(name, body) {
  const r = await api.updateRole(name, body)
  await loadAdminMeta()
  return r
}
export async function removeAdminRole(name) {
  const r = await api.deleteRole(name)
  await loadAdminMeta()
  return r
}
export async function changeOwnPassword(oldPw, newPw) {
  return api.changePassword(oldPw, newPw)
}

/* ============ 初始化 ============ */
/* 401 统一处理：续期也失败 -> 清登录态 -> App 自动切到登录页 */
api.setUnauthorizedHandler(() => {
  clearAuth()
  store.auth.user = null
})
refreshHealth()
if (getToken()) {
  ensureAuth().then((ok) => { if (ok) loadSessions() })
}
