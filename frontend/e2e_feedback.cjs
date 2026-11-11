/* 反馈功能端到端冒烟测试（playwright，headless chromium）
 * 覆盖：前端点赞/点踩按钮渲染、点击后乐观高亮、后端落库（few-shot / bad-case）、
 *       以及 /feedback/status 接口的历史回显数据正确性。
 */
const { chromium } = require('playwright')

const FRONTEND = 'http://127.0.0.1:5173'
const BACKEND = 'http://127.0.0.1:8000'

const captured = []          // 拦截到的 POST /feedback 请求体
const consoleErrors = []

async function waitAssistantCount(page, n) {
  // 先快速确认助手气泡已出现（证明发送成功），再等反馈条渲染（证明本轮完成）
  await page.waitForSelector('.msg.assistant', { timeout: 40000 })
  await page.waitForFunction(
    (k) => document.querySelectorAll('.msg.assistant .feedback-bar').length >= k,
    n,
    { timeout: 120000 }
  )
}

async function sendQuestion(page, q) {
  const ta = page.locator('textarea#chatInput')
  await ta.click()
  await ta.fill(q)
  await ta.press('Enter')
}

async function httpGetStatus(ids) {
  const url = `${BACKEND}/api/v1/feedback/status?message_ids=${encodeURIComponent(ids.join(','))}`
  const r = await fetch(url, { headers: { 'Content-Type': 'application/json' } })
  if (!r.ok) throw new Error('status API HTTP ' + r.status)
  return (await r.json()).status
}

;(async () => {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage()
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
  page.on('pageerror', (e) => consoleErrors.push('PAGEERROR: ' + e.message))
  // 拦截反馈提交请求，提取 message_id / type
  page.on('request', async (req) => {
    const u = req.url()
    if (u.endsWith('/api/v1/feedback') && req.method() === 'POST') {
      try { captured.push(JSON.parse(req.postData() || '{}')) } catch (_) {}
    }
  })

  console.log('→ 打开前端', FRONTEND)
  await page.goto(FRONTEND, { waitUntil: 'domcontentloaded', timeout: 30000 })
  await page.waitForSelector('textarea#chatInput', { timeout: 30000 })
  console.log('→ 输入框就绪')

  // —— 第 1 条消息：点赞（应进 few-shot）——
  await sendQuestion(page, '查一下近7天各分行的存款余额排名')
  await waitAssistantCount(page, 1)
  const m1 = page.locator('.msg.assistant').last()
  await m1.locator('.fb-btn', { hasText: '点赞' }).click()
  await page.waitForTimeout(800)
  const m1Active = await m1.locator('.fb-btn.active').count()
  console.log('→ 第1条点赞后 .active 按钮数 =', m1Active, m1Active === 1 ? 'OK' : 'FAIL')

  // —— 第 2 条消息：点踩（应进 bad-case）——
  await sendQuestion(page, '分析上月新客获取成本同比变化并给出归因')
  await waitAssistantCount(page, 2)
  const m2 = page.locator('.msg.assistant').last()
  await m2.locator('.fb-btn', { hasText: '点踩' }).click()
  await page.waitForTimeout(800)
  const m2Active = await m2.locator('.fb-btn.active').count()
  console.log('→ 第2条点踩后 .active 按钮数 =', m2Active, m2Active === 1 ? 'OK' : 'FAIL')

  // 等待反馈请求真正发出（乐观更新 + await）
  await page.waitForTimeout(1500)

  // —— 校验后端落库 & status 接口 ——
  const liked = captured.filter((c) => c.type === 'like')
  const disliked = captured.filter((c) => c.type === 'dislike')
  console.log('→ 拦截到反馈请求：like =', liked.length, 'dislike =', disliked.length)
  if (!liked.length || !disliked.length) {
    console.error('FAIL: 未捕获到预期的 like/dislike 提交'); await browser.close(); process.exit(1)
  }

  const likeId = liked[liked.length - 1].message_id
  const dislikeId = disliked[disliked.length - 1].message_id
  console.log('→ 校验 like 消息', likeId, '应回显 like')
  const st1 = await httpGetStatus([likeId])
  console.log('   status =', JSON.stringify(st1), st1[likeId] === 'like' ? 'OK' : 'FAIL')

  console.log('→ 校验 dislike 消息', dislikeId, '应回显 dislike')
  const st2 = await httpGetStatus([dislikeId])
  console.log('   status =', JSON.stringify(st2), st2[dislikeId] === 'dislike' ? 'OK' : 'FAIL')

  const ok = m1Active === 1 && m2Active === 1 && st1[likeId] === 'like' && st2[dislikeId] === 'dislike'
  console.log('→ 控制台错误数 =', consoleErrors.length)
  if (consoleErrors.length) console.log(consoleErrors.slice(0, 10).join('\n'))

  await browser.close()
  console.log(ok ? '\n✅ E2E 反馈冒烟全部通过' : '\n❌ E2E 反馈冒烟存在失败项')
  process.exit(ok ? 0 : 1)
})().catch((e) => { console.error('E2E 运行异常：', e); process.exit(2) })
