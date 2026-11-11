/* DmfDataAgent 后端 API 封装
 * 后端路由前缀统一 /api/v1（vite dev 代理到 uvicorn:8000；生产由 FastAPI 静态托管同源）
 * 覆盖：健康 / 会话 / 知识库 / 插件(Skill·MCP) / 系统提示词 / 运行观测 / 流式对话
 */
import { getRefreshToken, getToken, setAuthUser, setRefreshToken, setToken } from './storage'

const API = '/api/v1'
const H = { 'Content-Type': 'application/json' }

/* 401 统一处理：由 store 注册（清登录态 -> 跳登录页）。
 * 仅在「续期也失败」时才触发，避免 access 自然过期时把用户踢下线。 */
let _onUnauthorized = null
export function setUnauthorizedHandler(fn) { _onUnauthorized = fn }

/* 合并 Authorization（有令牌时自动带上） */
function withAuth(headers = {}) {
  const t = getToken()
  return t ? { ...headers, Authorization: `Bearer ${t}` } : { ...headers }
}

/* 登录 / 刷新入口自身的 401 不触发续期与登出（否则会自循环） */
function isAuthEntry(url) {
  return url.includes('/auth/login') || url.includes('/auth/refresh')
}

/* 静默续期：access 过期 -> 用 refresh 换新的 access + refresh（滚动轮换）。
 * 并发请求只发起一次续期（_refreshing 复用同一 Promise），避免把 refresh 令牌用废。 */
let _refreshing = null
export function refreshSession() {
  if (_refreshing) return _refreshing
  const rt = getRefreshToken()
  if (!rt) return Promise.resolve(false)
  _refreshing = (async () => {
    try {
      const r = await fetch(`${API}/auth/refresh`, {
        method: 'POST',
        headers: H,
        body: JSON.stringify({ refresh_token: rt })
      })
      if (!r.ok) return false
      const data = await r.json()
      setToken(data.access_token)
      setRefreshToken(data.refresh_token)
      if (data.user) setAuthUser(data.user)
      return true
    } catch (e) {
      return false
    } finally {
      _refreshing = null
    }
  })()
  return _refreshing
}

/* 带鉴权的 fetch：401 -> 续期一次并重放原请求；仍失败才交给 401 handler */
async function fetchAuthed(url, init = {}, retried = false) {
  const r = await fetch(url, { ...init, headers: withAuth(init.headers || {}) })
  if (r.status === 401 && !isAuthEntry(url)) {
    if (!retried && (await refreshSession())) {
      return fetchAuthed(url, init, true)
    }
    if (_onUnauthorized) _onUnauthorized()
  }
  return r
}

/* FastAPI 的 error.detail 有两种形态：
 * 1) 业务错误 → 字符串（HTTPException(status_code, detail="...")）
 * 2) 参数校验错误 → 对象数组 [{ loc, msg, type }, ...]（422 Unprocessable Entity）
 * 直接 String(detail) 会把后两者变成 "[object Object]"（数组含对象时亦然），
 * 必须先拍平成可读文本再抛，否则 UI 上只能看到一个毫无信息量的占位符。 */
export function formatDetail(detail) {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (typeof d === 'string') return d
        if (d && typeof d === 'object') {
          const loc = Array.isArray(d.loc)
            ? d.loc.filter((x) => x !== 'body' && x !== 'query' && x !== 'path').join('.')
            : ''
          const msg = d.msg || d.message || d.type || ''
          if (msg) return loc ? `${loc}: ${msg}` : String(msg)
          return JSON.stringify(d)
        }
        return String(d)
      })
      .join('；')
  }
  if (typeof detail === 'object') return detail.msg || detail.message || JSON.stringify(detail)
  return String(detail)
}

/* 把非 2xx 响应拍成一行可读错误文本（保留 HTTP 状态码作为兜底） */
export async function readError(r) {
  let detail = 'HTTP ' + r.status
  try {
    const b = await r.json()
    const text = formatDetail(b && b.detail)
    if (text) detail = text
  } catch (e) { /* 非 JSON 响应体：保留状态码 */ }
  return detail
}

async function req(url, init = {}) {
  const r = await fetchAuthed(url, init)
  if (!r.ok) throw new Error(await readError(r))
  return r.json()
}

/* ---------- 认证 ---------- */
export const login = (username, password) =>
  req(`${API}/auth/login`, { method: 'POST', headers: H, body: JSON.stringify({ username, password }) })
export const fetchMe = () => req(`${API}/auth/me`)
export const changePassword = (old_password, new_password) =>
  req(`${API}/auth/change-password`, { method: 'POST', headers: H, body: JSON.stringify({ old_password, new_password }) })

/* ---------- 角色 / 租户 / 用户（管理） ---------- */
export const fetchRoles = () => req(`${API}/roles`)
export const fetchPermissionCatalog = () => req(`${API}/roles/permissions`)

/* FR13 数据权限：当前登录用户的有效权限上下文（指标白名单 / 行权限 / 列 deny / 模式）。
 * 无需 admin —— 每个用户都该知道"哪些指标/维度我看不到"，否则界面上"查不出数"会被当成 bug。 */
export const myPermissionContext = () => req(`${API}/permission/my-context`)
export const createRole = (body) =>
  req(`${API}/roles`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const updateRole = (name, body) =>
  req(`${API}/roles/${encodeURIComponent(name)}`, { method: 'PUT', headers: H, body: JSON.stringify(body) })
export const deleteRole = (name) =>
  req(`${API}/roles/${encodeURIComponent(name)}`, { method: 'DELETE' })
export const listTenants = () => req(`${API}/tenants`)
export const createTenant = (body) =>
  req(`${API}/tenants`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const updateTenant = (id, body) =>
  req(`${API}/tenants/${id}`, { method: 'PATCH', headers: H, body: JSON.stringify(body) })
export const deleteTenant = (id) => req(`${API}/tenants/${id}`, { method: 'DELETE' })

export function listUsers(tenantId) {
  const q = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
  return req(`${API}/users${q}`)
}
export const createUser = (body) =>
  req(`${API}/users`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const updateUser = (id, body) =>
  req(`${API}/users/${id}`, { method: 'PATCH', headers: H, body: JSON.stringify(body) })
export const resetUserPassword = (id, new_password) =>
  req(`${API}/users/${id}/password`, { method: 'POST', headers: H, body: JSON.stringify({ new_password }) })
export const deleteUser = (id) => req(`${API}/users/${id}`, { method: 'DELETE' })

/* ---------- 健康 ---------- */
export async function fetchHealth() {
  try {
    const r = await fetch(`${API}/health`)
    const data = await r.json()
    return { ok: data.status === 'ok', components: data.components || {}, raw: data }
  } catch (e) {
    return { ok: false, components: {}, raw: null }
  }
}

/* ---------- 会话 ---------- */
export const listSessions = (userId = 'anonymous') =>
  req(`${API}/sessions?user_id=${encodeURIComponent(userId)}`)

export const createSession = (userId = 'anonymous', title = null) =>
  req(`${API}/sessions`, { method: 'POST', headers: H, body: JSON.stringify({ user_id: userId, title }) })

export const renameSession = (id, title) =>
  req(`${API}/sessions/${id}`, { method: 'PATCH', headers: H, body: JSON.stringify({ title }) })

export const deleteSession = (id) =>
  req(`${API}/sessions/${id}`, { method: 'DELETE' })

export const listMessages = (id) => req(`${API}/sessions/${id}/messages`)

/* 编辑重发「替换该轮」：删除该条消息及其之后的全部消息（inclusive=false 则保留锚点、只删其后）。
 * 返回 { deleted, state_reset }；后端同时会重置该会话的结构化对话状态。 */
export const deleteMessagesAfter = (sessionId, messageId, inclusive = true) =>
  req(
    `${API}/sessions/${encodeURIComponent(sessionId)}/messages/after/${encodeURIComponent(messageId)}` +
      `?inclusive=${inclusive ? 'true' : 'false'}`,
    { method: 'DELETE' }
  )

/* ---------- 知识库（个人 / 企业） ---------- */
export function listKbBases({ type, keyword, owner_id } = {}) {
  const q = new URLSearchParams()
  if (type) q.set('type', type)
  if (keyword) q.set('keyword', keyword)
  if (owner_id) q.set('owner_id', owner_id)
  const qs = q.toString()
  return req(`${API}/knowledge/bases${qs ? '?' + qs : ''}`)
}

export const createKbBase = (body) =>
  req(`${API}/knowledge/bases`, { method: 'POST', headers: H, body: JSON.stringify(body) })

export const deleteKbBase = (id) =>
  req(`${API}/knowledge/bases/${id}`, { method: 'DELETE' })

export function listKbDocs({ keyword, knowledge_base_id } = {}) {
  const q = new URLSearchParams()
  if (keyword) q.set('keyword', keyword)
  if (knowledge_base_id) q.set('knowledge_base_id', knowledge_base_id)
  const qs = q.toString()
  return req(`${API}/knowledge/documents${qs ? '?' + qs : ''}`)
}

export const addKbDoc = (body) =>
  req(`${API}/knowledge/documents`, { method: 'POST', headers: H, body: JSON.stringify(body) })

export async function uploadKbDoc(file, knowledge_base_id) {
  const form = new FormData()
  form.append('file', file)
  if (knowledge_base_id) form.append('knowledge_base_id', knowledge_base_id)
  const r = await fetchAuthed(`${API}/knowledge/documents/upload`, { method: 'POST', body: form })
  if (!r.ok) throw new Error(await readError(r))
  return r.json()
}

export const deleteKbDoc = (id) =>
  req(`${API}/knowledge/documents/${id}`, { method: 'DELETE' })

/* 文档分块列表（核验切分/入库质量，只读） */
export const listDocChunks = (id) => req(`${API}/knowledge/documents/${encodeURIComponent(id)}/chunks`)

export const queryKb = (query, top_k) =>
  req(`${API}/knowledge/query`, { method: 'POST', headers: H, body: JSON.stringify({ query, top_k }) })

/* ---------- 知识来源分级 + 权威源灌入（FR7） ---------- */

/* 来源分级统计（权威源 / 沉淀源 / 用户上传）+ 精排可用性 */
export const getAuthorityStats = () => req(`${API}/knowledge/authority/stats`)

/* 灌入指标口径字典（权威源，幂等覆盖；仅管理员） */
export const seedMetricDictionary = (knowledge_base_id) =>
  req(`${API}/knowledge/seed/dictionary`, {
    method: 'POST',
    headers: H,
    body: JSON.stringify({ knowledge_base_id: knowledge_base_id || null })
  })

/* ---------- L2 用户偏好（个人记忆） / 记忆开关 ---------- */
export function listPreferences(status) {
  const q = status ? `?status=${encodeURIComponent(status)}` : ''
  return req(`${API}/memory/preferences${q}`)
}
export const upsertPreference = (body) =>
  req(`${API}/memory/preferences`, { method: 'PUT', headers: H, body: JSON.stringify(body) })
export const deletePreference = (key) =>
  req(`${API}/memory/preferences/${encodeURIComponent(key)}`, { method: 'DELETE' })
export const clearPreferences = () =>
  req(`${API}/memory/preferences`, { method: 'DELETE' })
export const getMemoryDisabled = () => req(`${API}/memory/disable`)
export const setMemoryDisabled = (disabled) =>
  req(`${API}/memory/disable`, { method: 'POST', headers: H, body: JSON.stringify({ disabled }) })

/* ---------- 记忆管理（L1 会话记忆 / L2 用户记忆 / L3 长期记忆） ---------- */
/* L1：会话摘要列表（跨会话，管理页用；原本只能按 session_id 单条查） */
export const listMemorySummaries = (keyword = '', limit = 100) =>
  req(`${API}/memory/l1${qs({ keyword, limit })}`)
export const clearSessionMemory = (sessionId) =>
  req(`${API}/memory/session/${encodeURIComponent(sessionId)}/clear`, { method: 'POST' })

/* 上下文占比圆环：轮次口径状态 + 点击手动压缩为摘要 */
export const getContextStatus = (sessionId) =>
  req(`${API}/memory/session/${encodeURIComponent(sessionId)}/context`)
export const compressContext = (sessionId) =>
  req(`${API}/memory/session/${encodeURIComponent(sessionId)}/compress`, { method: 'POST' })

/* L2：全平台偏好（平台视角，需 memory 权限点）+ 按主键删除（需 memory:write） */
export const listAllPreferences = (params = {}) => req(`${API}/memory/l2${qs(params)}`)
export const deletePreferenceById = (id) =>
  req(`${API}/memory/l2/${id}`, { method: 'DELETE' })

/* L3：领域知识 CRUD + 审核 */
export const listDomainMemories = (params = {}) => req(`${API}/memory/l3${qs(params)}`)
export const createDomainMemory = (body) =>
  req(`${API}/memory/l3`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const updateDomainMemory = (id, body) =>
  req(`${API}/memory/l3/${id}`, { method: 'PUT', headers: H, body: JSON.stringify(body) })
export const deleteDomainMemory = (id) =>
  req(`${API}/memory/l3/${id}`, { method: 'DELETE' })

/* 各层条数统计（管理页统计卡） */
export const memoryStats = () => req(`${API}/memory/stats`)

/* ---------- Few-Shot 样本管理 ---------- */
export const listFewShots = (params = {}) => req(`${API}/fewshots${qs(params)}`)
export const fewShotStats = () => req(`${API}/fewshots/stats`)
export const fewShotFacets = () => req(`${API}/fewshots/facets`)
export const createFewShot = (body) =>
  req(`${API}/fewshots`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const updateFewShot = (id, body) =>
  req(`${API}/fewshots/${id}`, { method: 'PUT', headers: H, body: JSON.stringify(body) })
export const deleteFewShot = (id) => req(`${API}/fewshots/${id}`, { method: 'DELETE' })
/* 试召回：验证哪些样本会被命中、最终注入文本长什么样（与线上意图识别同源） */
export const recallFewShots = (query, top_k = 3) =>
  req(`${API}/fewshots/recall`, { method: 'POST', headers: H, body: JSON.stringify({ query, top_k }) })
export const importFewShots = (items, category = 'intention', overwrite = false) =>
  req(`${API}/fewshots/import`, {
    method: 'POST', headers: H, body: JSON.stringify({ items, category, overwrite })
  })
/* 导出返回 JSON（非文件流）：由前端落盘，避免新窗口下载丢失 Authorization 头 */
export const exportFewShots = (status) => req(`${API}/fewshots/export${qs({ status })}`)

/* ---------- 三级黑话词典管理 ---------- */
export const listSlang = (params = {}) => req(`${API}/slang${qs(params)}`)
export const slangStats = () => req(`${API}/slang/stats`)
export const slangFacets = () => req(`${API}/slang/facets`)
/* 在线试改写：复用线上解析器（Redis 优先 + 最长优先 + 三模式），所见即线上所得 */
export const previewSlang = (body) =>
  req(`${API}/slang/preview`, { method: 'POST', headers: H, body: JSON.stringify(body || {}) })
export const createSlang = (body) =>
  req(`${API}/slang`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const updateSlang = (id, body) =>
  req(`${API}/slang/${id}`, { method: 'PUT', headers: H, body: JSON.stringify(body) })
export const deleteSlang = (id) => req(`${API}/slang/${id}`, { method: 'DELETE' })
export const publishSlang = (id) => req(`${API}/slang/${id}/publish`, { method: 'POST' })
export const disableSlang = (id) => req(`${API}/slang/${id}/disable`, { method: 'POST' })
export const slangVersions = (id) => req(`${API}/slang/${id}/versions`)
export const rollbackSlang = (id, version, remark = '') =>
  req(`${API}/slang/${id}/rollback`, { method: 'POST', headers: H, body: JSON.stringify({ version, remark }) })
/* 导入：支持 dry_run 预检（返回冲突/非法词条，不落库） */
export const importSlang = (body) =>
  req(`${API}/slang/import`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const exportSlang = (params = {}) => req(`${API}/slang/export${qs(params)}`)
/* 命中率 / 误改写率看板聚合（近 days 天） */
export const slangMetrics = (days = 30) => req(`${API}/slang/metrics?days=${days}`)
/* 改写结果反馈（误改写率数据源）：original 由后端归一化取哈希定位事件 */
export const feedbackSlangRewrite = (body) =>
  req(`${API}/slang/rewrite/feedback`, { method: 'POST', headers: H, body: JSON.stringify(body || {}) })
/* 批量查询黑话改写反馈状态：{ [message_id]: 'good' | 'bad' }，供历史消息回显高亮 */
export async function fetchSlangRewriteFeedbackStatus(ids) {
  const list = (ids || []).filter(Boolean)
  if (!list.length) return { status: {} }
  return req(`${API}/slang/rewrite/feedback/status?message_ids=${encodeURIComponent(list.join(','))}`)
}

/* ---------- 插件管理（Skill / MCP） ---------- */
export const fetchSkills = () => req(`${API}/plugins/skills`)
export const fetchMcp = () => req(`${API}/plugins/mcp`)
export const addSkill = (payload) =>
  req(`${API}/plugins/skills`, { method: 'POST', headers: H, body: JSON.stringify(payload) })
export const addMcp = (payload) =>
  req(`${API}/plugins/mcp`, { method: 'POST', headers: H, body: JSON.stringify(payload) })
/* 新增弹窗预检：用表单当前配置试连，不落库 */
export const probeMcp = (payload) =>
  req(`${API}/plugins/mcp/test`, { method: 'POST', headers: H, body: JSON.stringify(payload) })
/* 上传 SKILL.md：FormData 不设 Content-Type，交给浏览器自动带 multipart boundary */
export async function uploadSkill(file, description = '') {
  const fd = new FormData()
  fd.append('file', file)
  if (description) fd.append('description', description)
  const r = await fetchAuthed(`${API}/plugins/skills/upload`, { method: 'POST', body: fd })
  if (!r.ok) throw new Error(await readError(r))
  return r.json()
}
export const fetchSkillDetail = (name) => req(`${API}/plugins/skills/${encodeURIComponent(name)}`)
export const runSkill = (name, inputs) =>
  req(`${API}/plugins/skills/${encodeURIComponent(name)}/run`, {
    method: 'POST', headers: H, body: JSON.stringify({ inputs: inputs || {} })
  })
export const testMcp = (name) =>
  req(`${API}/plugins/mcp/${encodeURIComponent(name)}/test`, { method: 'POST' })
export const deletePlugin = (plugin_type, name) =>
  req(`${API}/plugins/delete?plugin_type=${plugin_type}&name=${encodeURIComponent(name)}`, { method: 'DELETE' })

/* ---------- 模型管理（对话 LLM 配置 CRUD + 连接测试 + 设为默认） ---------- */
export const listModels = () => req(`${API}/models`)
export const getModel = (name) => req(`${API}/models/${encodeURIComponent(name)}`)
export const createModel = (payload) =>
  req(`${API}/models`, { method: 'POST', headers: H, body: JSON.stringify(payload) })
export const updateModel = (name, payload) =>
  req(`${API}/models/${encodeURIComponent(name)}`, { method: 'PUT', headers: H, body: JSON.stringify(payload) })
export const deleteModel = (name) =>
  req(`${API}/models/${encodeURIComponent(name)}`, { method: 'DELETE' })
export const setModelDefault = (name) =>
  req(`${API}/models/${encodeURIComponent(name)}/default`, { method: 'POST' })
/* 新增/编辑弹窗预检：用表单当前配置试连，不落库 */
export const probeModel = (payload) =>
  req(`${API}/models/probe`, { method: 'POST', headers: H, body: JSON.stringify(payload) })
export const testModel = (name) =>
  req(`${API}/models/${encodeURIComponent(name)}/test`, { method: 'POST' })

/* ---------- 系统提示词管理 ---------- */
export const fetchPromptsList = () => req(`${API}/prompts/list`)
export const fetchPromptHistory = (name) => req(`${API}/prompts/history?name=${encodeURIComponent(name)}`)
export function fetchPromptDetail(name, version) {
  const q = new URLSearchParams({ name })
  if (version) q.set('version', version)
  return req(`${API}/prompts/detail?${q}`)
}
export const updatePrompt = (name, content, description) =>
  req(`${API}/prompts/update`, { method: 'POST', headers: H, body: JSON.stringify({ name, content, description }) })
export const rollbackPrompt = (name, version) =>
  req(`${API}/prompts/rollback`, { method: 'POST', headers: H, body: JSON.stringify({ name, version }) })

/* ---------- 运行观测 ---------- */
function qs(params = {}) {
  const q = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') q.set(k, v)
  })
  const s = q.toString()
  return s ? '?' + s : ''
}

export const fetchObsSummary = (params = {}) => req(`${API}/obs/summary${qs(params)}`)
export const fetchObsTraces = (limit = 50) => req(`${API}/obs/traces?limit=${limit}`)
export const fetchObsTraceDetail = (sessionId, roundId) =>
  req(`${API}/obs/traces/${sessionId}/${roundId}`)

/* 专页：轮次明细（过滤 + 分页）/ 节点 span 统计 / 单会话观测汇总 */
export const fetchObsRounds = (params = {}) => req(`${API}/obs/rounds${qs(params)}`)
export const fetchObsSpans = (params = {}) => req(`${API}/obs/spans${qs(params)}`)
export const fetchObsSession = (sessionId) =>
  req(`${API}/obs/sessions/${encodeURIComponent(sessionId)}`)

/* FR11 成本 / 质量看板 */
export const fetchObsCost = (params = {}) => req(`${API}/obs/cost${qs(params)}`)
export const fetchObsQuality = (params = {}) => req(`${API}/obs/quality${qs(params)}`)

/* ---------- 合规审计看板（FR13，仅 admin） ---------- */
export const fetchAuditLogs = (params = {}) =>
  req(`${API}/audit/logs${qs(params)}`)
export const fetchAuditStats = () => req(`${API}/audit/stats`)

/* 导出（FR13）：走 req 带 Authorization 取 JSON 回前端落盘。
 * 不再用「返回 URL 给 window.open」——新窗口不带 Authorization 头，鉴权开启时必然 401
 * （与 /slang/export、/fewshots/export 保持同一条通路）。 */
export const obsExport = (params = {}) => req(`${API}/obs/export${qs(params)}`)

/* ---------- 语义层（compile/query 联调 + 清单浏览） ---------- */
export const listMetrics = (keyword = '') => req(`${API}/metrics?keyword=${encodeURIComponent(keyword)}`)
export const semanticMetrics = () => req(`${API}/semantic/metrics`)
export const semanticDimensions = () => req(`${API}/semantic/dimensions`)
export const semanticManifest = () => req(`${API}/semantic/manifest`)
export const semanticHealth = () => req(`${API}/semantic/health`)
export const semanticCompile = (body) =>
  req(`${API}/semantic/compile`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const semanticQuery = (body) =>
  req(`${API}/semantic/query`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const semanticKnowledgeGraph = (params = {}) =>
  req(`${API}/semantic/knowledge-graph${qs(params)}`)
export const semanticKnowledgeLineage = (query) =>
  req(`${API}/semantic/knowledge-graph/lineage?query=${encodeURIComponent(query || '')}`)

/* ---------- 语义层来源 / 发布快照 / 审批流（混合方案） ----------
 * 快照状态机与指标级同构：draft → review → published → deprecated；
 * 运行时只消费 published，审批通过后后端会自动热更新（无需重启）。 */
export const semanticRuntime = () => req(`${API}/semantic/registry/runtime`)
/* 当前生效基座的**完整**清单（semantic_models / metrics 全字段）。
 * overlay 是按 name 整条替换、不做字段级 merge，所以编辑面必须以完整条目为起点提交。 */
export const semanticBaseManifest = () => req(`${API}/semantic/registry/base-manifest`)
export const reloadSemantic = () =>
  req(`${API}/semantic/registry/reload`, { method: 'POST' })

export function listSnapshots(tenantId = '', state = '', limit = 50) {
  const q = new URLSearchParams()
  if (tenantId) q.set('tenant_id', tenantId)
  if (state) q.set('state', state)
  q.set('limit', String(limit))
  return req(`${API}/semantic/registry/snapshots?${q.toString()}`)
}
export const snapshotDetail = (id, includePayload = false) =>
  req(`${API}/semantic/registry/snapshots/${id}?include_payload=${includePayload}`)

/* 发布快照：mode=full 取当前 YAML 基座；mode=overlay 必须带 payload（差异条目）。
 * 默认落 draft —— 直接 published 需 admin 显式指定。 */
export const publishSnapshot = (body) =>
  req(`${API}/semantic/registry/publish`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const submitSnapshot = (id) =>
  req(`${API}/semantic/registry/snapshots/${id}/submit`, { method: 'POST' })
export const approveSnapshot = (id) =>
  req(`${API}/semantic/registry/snapshots/${id}/approve`, { method: 'POST' })
export const deprecateSnapshot = (id) =>
  req(`${API}/semantic/registry/snapshots/${id}/deprecate`, { method: 'POST' })
export const reviveSnapshot = (id) =>
  req(`${API}/semantic/registry/snapshots/${id}/revive`, { method: 'POST' })
export const rollbackSnapshot = (id, state = 'published') =>
  req(`${API}/semantic/registry/snapshots/${id}/rollback`, {
    method: 'POST', headers: H, body: JSON.stringify({ state })
  })
/* 驳回快照（review → draft）：reason 必填，后端会记录驳回人/原因。 */
export const rejectSnapshot = (id, reason) =>
  req(`${API}/semantic/registry/snapshots/${id}/reject`, {
    method: 'POST', headers: H, body: JSON.stringify({ reason })
  })
/* 批量审批/驳回：action=approve|reject，reject 时必须带 reason。
 * 返回 { approved|rejected: [...], failed: [{id, error}] }，逐条容错。 */
export const batchSnapshots = (action, ids, reason = null) =>
  req(`${API}/semantic/registry/snapshots/batch`, {
    method: 'POST', headers: H, body: JSON.stringify({ action, ids, reason })
  })

/* ---------- 语义注册中心：指标 / 维度 CRUD + 审批流 ---------- */
export const registryMetrics = (params = {}) =>
  req(`${API}/semantic/registry/metrics${qs(params)}`)
export const registryMetricDetail = (key) =>
  req(`${API}/semantic/registry/metrics/${encodeURIComponent(key)}`)
export const createMetricDraft = (body) =>
  req(`${API}/semantic/registry/metrics/draft`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const updateMetric = (key, body) =>
  req(`${API}/semantic/registry/metrics/${encodeURIComponent(key)}`, { method: 'PUT', headers: H, body: JSON.stringify(body) })
export const validateMetric = (key, deep = false) =>
  req(`${API}/semantic/registry/metrics/${encodeURIComponent(key)}/validate?deep=${deep ? 'true' : 'false'}`, { method: 'POST' })
export const submitMetric = (key) =>
  req(`${API}/semantic/registry/metrics/${encodeURIComponent(key)}/submit`, { method: 'POST' })
export const publishMetric = (key, deep = false) =>
  req(`${API}/semantic/registry/metrics/${encodeURIComponent(key)}/publish?deep=${deep ? 'true' : 'false'}`, { method: 'POST' })
export const deprecateMetric = (key) =>
  req(`${API}/semantic/registry/metrics/${encodeURIComponent(key)}/deprecate`, { method: 'POST' })
export const metricVersions = (key) =>
  req(`${API}/semantic/registry/metrics/${encodeURIComponent(key)}/versions`)

export const registryDimensions = (params = {}) =>
  req(`${API}/semantic/registry/dimensions${qs(params)}`)
export const registryDimensionDetail = (key) =>
  req(`${API}/semantic/registry/dimensions/${encodeURIComponent(key)}`)
export const createDimensionDraft = (body) =>
  req(`${API}/semantic/registry/dimensions/draft`, { method: 'POST', headers: H, body: JSON.stringify(body) })
export const updateDimension = (key, body) =>
  req(`${API}/semantic/registry/dimensions/${encodeURIComponent(key)}`, { method: 'PUT', headers: H, body: JSON.stringify(body) })
export const validateDimension = (key) =>
  req(`${API}/semantic/registry/dimensions/${encodeURIComponent(key)}/validate`, { method: 'POST' })
export const submitDimension = (key) =>
  req(`${API}/semantic/registry/dimensions/${encodeURIComponent(key)}/submit`, { method: 'POST' })
export const publishDimension = (key) =>
  req(`${API}/semantic/registry/dimensions/${encodeURIComponent(key)}/publish`, { method: 'POST' })
export const deprecateDimension = (key) =>
  req(`${API}/semantic/registry/dimensions/${encodeURIComponent(key)}/deprecate`, { method: 'POST' })
export const dimensionVersions = (key) =>
  req(`${API}/semantic/registry/dimensions/${encodeURIComponent(key)}/versions`)
export const registryMeasures = (params = {}) =>
  req(`${API}/semantic/registry/measures${qs(params)}`)
export const rollbackMetric = (key, version) =>
  req(`${API}/semantic/registry/metrics/${encodeURIComponent(key)}/rollback`, {
    method: 'POST', headers: H, body: JSON.stringify({ version })
  })
export const rollbackDimension = (key, version) =>
  req(`${API}/semantic/registry/dimensions/${encodeURIComponent(key)}/rollback`, {
    method: 'POST', headers: H, body: JSON.stringify({ version })
  })

/* ---------- 点赞 / 点踩反馈（few-shot & bad case） ---------- */
/* 点赞 -> 写入 few_shot_examples；点踩 -> 写入 bad_cases。
   message_id 即助手消息 id（= trace_records.round_id）。 */
export async function submitFeedback(body) {
  return req(`${API}/feedback`, { method: 'POST', headers: H, body: JSON.stringify(body) })
}

/* 批量查询消息反馈状态：{ [message_id]: 'like' | 'dislike' }，供历史消息回显高亮。 */
export async function fetchFeedbackStatus(ids) {
  const list = (ids || []).filter(Boolean)
  if (!list.length) return { status: {} }
  return req(`${API}/feedback/status?message_ids=${encodeURIComponent(list.join(','))}`)
}

/**
 * 流式对话（SSE）：POST /api/v1/chat/stream
 * 后端帧格式：event: <type>\ndata: <json>\n\n
 * 事件类型：session | node_start | node_done | token | final | error
 * onEvent({ type, ...data }) 逐事件回调
 */
export async function streamChat(payload, onEvent, signal) {
  const resp = await fetchAuthed(`${API}/chat/stream`, {
    method: 'POST',
    headers: H,
    body: JSON.stringify(payload),
    signal
  })
  if (!resp.ok) throw new Error(await readError(resp))
  if (!resp.body) throw new Error(`HTTP ${resp.status}：响应无 body，无法读取流`)
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const frames = buf.split('\n\n')
    buf = frames.pop() || ''
    for (const frame of frames) {
      const ev = parseFrame(frame)
      if (ev) onEvent(ev)
    }
  }
}

function parseFrame(frame) {
  const lines = frame.split('\n')
  const eventLine = lines.find(l => l.startsWith('event:'))
  const dataLine = lines.find(l => l.startsWith('data:'))
  if (!eventLine || !dataLine) return null
  const type = eventLine.slice('event:'.length).trim()
  const raw = dataLine.slice('data:'.length).trim()
  let data = {}
  try {
    data = JSON.parse(raw)
  } catch (e) {
    return null
  }
  return { type, ...data }
}
