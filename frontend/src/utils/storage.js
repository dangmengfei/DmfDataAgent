/* localStorage 持久化：会话消息 / 会话列表 / 当前会话 */
const MAX_MSGS = 200
const MSG_KEY = id => 'mc_msgs_' + id
const CUR_KEY = 'mc_current_session'

export function loadMessages(id){
  try{ return JSON.parse(localStorage.getItem(MSG_KEY(id)) || '[]') }catch(e){ return [] }
}
export function saveMessage(sessionId, role, content, extra = {}){
  const list = loadMessages(sessionId)
  list.push({role, content, ts: Date.now(), ...extra})
  if(list.length > MAX_MSGS) list.splice(0, list.length - MAX_MSGS)
  localStorage.setItem(MSG_KEY(sessionId), JSON.stringify(list))
}
export function loadSessions(){
  try{ return JSON.parse(localStorage.getItem('mc_sessions') || '[]') }catch(e){ return [] }
}
export function saveSession(sessionId, title){
  const sessions = loadSessions()
  const idx = sessions.findIndex(s => s.id === sessionId)
  const rec = {id: sessionId, title: title || '新对话', ts: Date.now()}
  if(idx >= 0) sessions[idx] = rec; else sessions.push(rec)
  localStorage.setItem('mc_sessions', JSON.stringify(sessions.slice(-20)))
  return sessions
}
export function getCurrentSession(){
  return localStorage.getItem(CUR_KEY) || ('sess_' + Date.now())
}
export function setCurrentSession(id){ localStorage.setItem(CUR_KEY, id) }

/* ---- 登录令牌 / 当前用户持久化 ---- */
const TOKEN_KEY = 'dmf_token'
const REFRESH_KEY = 'dmf_refresh_token'
const USER_KEY = 'dmf_user'
export function getToken(){ return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t){ t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY) }
/* 刷新令牌：access 过期时用它静默换新（/auth/refresh），失败才要求重新登录 */
export function getRefreshToken(){ return localStorage.getItem(REFRESH_KEY) || '' }
export function setRefreshToken(t){ t ? localStorage.setItem(REFRESH_KEY, t) : localStorage.removeItem(REFRESH_KEY) }
export function getAuthUser(){
  try{ return JSON.parse(localStorage.getItem(USER_KEY) || 'null') }catch(e){ return null }
}
export function setAuthUser(u){ u ? localStorage.setItem(USER_KEY, JSON.stringify(u)) : localStorage.removeItem(USER_KEY) }
/* 一次性清空全部登录态（登出 / 续期失败） */
export function clearAuth(){ setToken(''); setRefreshToken(''); setAuthUser(null) }
