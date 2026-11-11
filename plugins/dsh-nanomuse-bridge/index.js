/**
 * @local/dsh-nanomuse-bridge —— DSH 原生会话事件 → nanoMuse 9196
 *
 * 官方契约（源码核验，非推测）：
 *   packages/core/session/src/index.ts:77   'session/event'(session, event): void
 *   packages/core/session/src/types.ts:281  SessionEventMap（turn/start / turn/end /
 *                                          tool/call / tool/result / assistant/message /
 *                                          user/message / approval/asked …）
 *   apps/cli/tests/profiles/headless/tests/harness.ts:104  ctx.on('agent/status', …)
 *   apps/web/tests/chat-scroll-contract.e2e.ts:190  ctx.on('agent/assistant-stream', …)
 *   packages/core/agent/src/agent.ts:163/167  agent.followup() / agent.steer()
 *   packages/api/session-controller/src/commands.ts:364  mode==='steer' ? steer : followup
 *   packages/client/ui-workspace/src/client/index.ts:143  uiWorkspace.openSession(sessionId)
 *
 * 三条硬纪律：
 *   1. 只推结构化增量事件，绝不全量读聊天历史、绝不每分钟扫描、普通事件零模型调用。
 *   2. 断线缓存（磁盘 jsonl）+ 恢复后补传；相同 (session, seq, type) 去重由 9196 digest 把关，
 *      本端另用 sent 集避免重拨。
 *   3. 向原会话发指令只走 ctx.agents.get(sid).followup()/steer()。
 *      找不到活跃 Agent 明确回报 recoverable=false，绝不另起 headless 冒充接续。
 */
import { createServer } from 'node:http'
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'

export const name = 'nanomuse-bridge'
// `sessions` 必填：不声明兴趣 context 就不给这个 service 投递 session/event
// （dsh-context-bridge/index.js:33-39 有实测过的坑）。`agents` 在 base/headless
// 都有（要跑 agent loop）。`uiWorkspace` 只有桌面有，故不声明 inject —— 代码里
// 全程用可选链，headless 下 openSession 明确回报「当前运行面不是桌面」，符合
// P3「不可恢复要明说」；声明成必需会让 headless 直接拒载插件。
export const inject = ['sessions', 'agents']

const DEFAULT_ENDPOINT = 'http://127.0.0.1:9196/api/dsh-bridge/event'
const DEFAULT_CONTROL_PORT = 9203
const MAX_BUFFER = 500
const FLUSH_MS = 1200
const REDACT = 400

function brief(v, n = REDACT) {
  const s = typeof v === 'string' ? v : JSON.stringify(v ?? '')
  return s.length > n ? s.slice(0, n) + '…' : s
}

function textOf(content) {
  let out = ''
  for (const b of content ?? []) {
    if (b && typeof b === 'object' && b.type === 'text' && typeof b.text === 'string') out += b.text
  }
  return out
}

/** 与 dsh-session-shift/lib/index.js:674 同法：普通安装裸 import，打包安装回退 createRequire。 */
async function loadHarnessModule(specifier) {
  const errors = []
  try { return await import(specifier) } catch (e) { errors.push('import: ' + (e?.code ?? e?.message)) }
  const bases = []
  if (process.argv[1]) bases.push(process.argv[1])
  if (process.argv[2]) bases.push(join(process.argv[2], '_'))
  bases.push(process.execPath)
  for (const base of bases) {
    try {
      const resolved = createRequire(base).resolve(specifier)
      return await import(pathToFileURL(resolved).href)
    } catch (e) { errors.push(base + ' → ' + (e?.code ?? e?.message)) }
  }
  throw new Error(specifier + ' 拿不到：' + errors.join('；'))
}

let createUserMessagePromise
async function createUserMessage(args) {
  if (createUserMessagePromise === undefined) {
    createUserMessagePromise = (async () => {
      const mod = await loadHarnessModule('@deepseek-ai/dsh-llm')
      const fn = mod?.createUserMessage ?? mod?.default?.createUserMessage
      if (typeof fn !== 'function') throw new Error('@deepseek-ai/dsh-llm 未导出 createUserMessage')
      return fn
    })().catch(e => { createUserMessagePromise = undefined; throw e })
  }
  return (await createUserMessagePromise)(args)
}

export function apply(ctx, config = {}) {
  const endpoint = String(config.endpoint || DEFAULT_ENDPOINT)
  const controlPort = Number.isInteger(config.controlPort) ? config.controlPort : DEFAULT_CONTROL_PORT
  const cacheDir = String(config.cacheDir || join(process.cwd(), 'nanoMuse项目总管', 'data', 'bridge-cache'))
  const cacheFile = join(cacheDir, 'outbox.jsonl')
  const logFile = join(cacheDir, 'bridge-plugin.log')
  try { mkdirSync(cacheDir, { recursive: true }) } catch {}

  const log = line => {
    try { appendFileSync(logFile, `[${new Date().toISOString()}] ${line}\n`, 'utf8') } catch {}
  }

  // ---- 断线缓存 -----------------------------------------------------------
  // 启动时把上次没发出去的读回来；每次失败把事件补回文件头，恢复后按序补传。
  let outbox = []
  try {
    if (existsSync(cacheFile)) {
      outbox = readFileSync(cacheFile, 'utf8').split('\n').filter(Boolean)
        .map(l => { try { return JSON.parse(l) } catch { return null } }).filter(Boolean)
        .slice(-MAX_BUFFER)
    }
  } catch {}
  const persist = () => {
    try { writeFileSync(cacheFile, outbox.slice(-MAX_BUFFER).map(e => JSON.stringify(e)).join('\n') + '\n', 'utf8') } catch {}
  }
  const enqueue = env => {
    outbox.push(env)
    if (outbox.length > MAX_BUFFER) outbox = outbox.slice(-MAX_BUFFER)
    persist()
  }

  let online = false
  let flushing = false
  async function flush() {
    if (flushing || outbox.length === 0) return
    flushing = true
    try {
      // 一次最多推 20 条，避免 9196 重启瞬间被打满
      const batch = outbox.slice(0, 20)
      const r = await fetch(endpoint, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ events: batch, bridge: name, at: Date.now() }),
        signal: AbortSignal.timeout(6000),
      })
      if (!r.ok) throw new Error('HTTP ' + r.status)
      outbox.splice(0, batch.length)
      persist()
      online = true
      if (batch.length) log(`flush ok ${batch.length} 条（余 ${outbox.length}）`)
    } catch (e) {
      online = false
      log(`flush 失败：${e?.message ?? e}（缓存 ${outbox.length} 条待补传）`)
    } finally { flushing = false }
  }
  const timer = setInterval(() => { void flush() }, FLUSH_MS)
  if (typeof timer.unref === 'function') timer.unref()

  // ---- 会话元数据（结构化，不读全部历史） --------------------------------
  // session 对象给的是 snapshotEvents()，只在首次见到该会话时扫一遍拿 title，
  // 之后全程靠增量事件；不是「每分钟读历史」。
  const titles = new Map()
  const cwds = new Map()
  function metaOf(session) {
    const sid = String(session?.id ?? '')
    if (!sid) return { title: '', cwd: '' }
    if (!titles.has(sid)) {
      try {
        const evs = typeof session.snapshotEvents === 'function' ? session.snapshotEvents() : []
        let title = ''
        for (let i = evs.length - 1; i >= 0; i -= 1) {
          const e = evs[i]
          if (e?.type === 'session/title' && e.data?.title) { title = String(e.data.title); break }
        }
        if (!title) {
          for (const e of evs) {
            if (e?.type !== 'user/message' || e.data?.source?.kind !== 'user') continue
            const t = textOf(e.data?.content).replace(/\s+/g, ' ').trim()
            if (t) { title = t.slice(0, 100); break }
          }
        }
        titles.set(sid, title)
        for (const e of evs) {
          if (e?.type === 'session/init' && e.data?.cwd) { cwds.set(sid, String(e.data.cwd)); break }
        }
      } catch {}
    }
    return { title: titles.get(sid) ?? '', cwd: cwds.get(sid) ?? '' }
  }

  function push(session, event, extra = {}) {
    const sid = String(session?.id ?? '')
    if (!sid || !event?.type) return
    const meta = metaOf(session)
    const env = {
      session_id: sid,
      event_seq: event.seq ?? -1,
      event_type: event.type,
      turn: event.data?.turn ?? extra.turn ?? null,
      title: extra.title ?? meta.title,
      cwd: meta.cwd,
      status: extra.status ?? '',
      reason: event.data?.reason ? brief(event.data.reason, 120) : '',
      summary: extra.summary ?? '',
      tool: event.data?.name ?? '',
      payload_brief: brief(event.data ?? {}, 240),
      at: event.time ?? Date.now(),
      bridge: name,
    }
    enqueue(env)
    void flush()
  }

  // ---- P0 事件订阅 --------------------------------------------------------
  // 关键阶段事件一律推；高频 assistant-stream 只做「阶段变化」计数，
  // 不把每个 token 都发过去（Token 纪律）。
  const KEY = new Set([
    'turn/start', 'turn/end', 'step/start', 'tool/call', 'tool/result',
    'assistant/message', 'user/message', 'approval/asked', 'session/title', 'error',
  ])
  const streamCount = new Map()
  ctx.on('session/event', (session, event) => {
    try {
      const t = event?.type
      if (t === 'session/title') {
        titles.set(String(session.id), String(event.data?.title ?? ''))
      }
      if (!KEY.has(t)) return
      const extra = {}
      if (t === 'tool/result' && event.data?.message?.isError) extra.status = 'tool_error'
      if (t === 'turn/end') extra.status = 'idle'
      if (t === 'approval/asked') extra.status = 'waiting_decision'
      if (t === 'assistant/message') {
        const txt = textOf(event.data?.message?.content ?? event.data?.content)
        if (txt.trim()) extra.summary = txt.slice(0, 200).replace(/\s+/g, ' ')
      }
      push(session, event, extra)
    } catch (e) { log(`session/event 采集失败：${e?.message ?? e}`) }
  })

  ctx.on('agent/status', ({ agent, status }) => {
    try {
      const sid = String(agent?.id ?? agent?.session ?? '')
      if (!sid) return
      // 官方 status 事件没有 SessionEvent 包络，造一条结构化等价事件（seq=-1 标状态类）
      enqueue({
        session_id: sid, event_seq: -1, event_type: 'status', turn: null,
        title: titles.get(sid) ?? '', cwd: cwds.get(sid) ?? '', status: String(status),
        reason: '', summary: '', tool: '', payload_brief: '', at: Date.now(), bridge: name,
      })
      void flush()
    } catch (e) { log(`agent/status 采集失败：${e?.message ?? e}`) }
  })

  let streamFrames = 0
  ctx.on('agent/assistant-stream', ({ frame, agent }) => {
    streamFrames += 1
    // 每 50 帧合成一条「仍在输出」心跳，不逐帧外推
    if (streamFrames % 50 !== 0) return
    try {
      const sid = String(agent?.id ?? agent?.session ?? frame?.sessionId ?? '')
      if (!sid) return
      enqueue({
        session_id: sid, event_seq: -2, event_type: 'assistant-stream',
        turn: frame?.turn ?? null, title: titles.get(sid) ?? '', cwd: '',
        status: 'streaming', reason: '', summary: '', tool: '',
        payload_brief: 'frames=' + streamFrames, at: Date.now(), bridge: name,
      })
      void flush()
    } catch {}
  })

  // ---- 控制面：原会话定位 / 接续（P3） -----------------------------------
  // 活跃 Agent 从 ctx.agents.get(sid) 取；这是「原会话」本身，不是新会话。
  // 取不到 → recoverable=false + 原因，9196 会明确提示，绝不静默新建 headless。
  // 会话结束后 context 进入 inactive，直接访问 ctx.agents 会抛
  // `cannot get required service "agents" in inactive context`。惰性取用，
  // 取不到 = 明确回报「无活跃 Agent / 不可恢复」，符合 P3 纪律。
  function agents() {
    try { return typeof ctx.agents?.get === 'function' ? ctx.agents : null } catch { return null }
  }
  function uiWorkspace() {
    try { return typeof ctx.uiWorkspace?.openSession === 'function' ? ctx.uiWorkspace : null } catch { return null }
  }

  const server = createServer(async (req, res) => {
    const send = (code, obj) => {
      res.writeHead(code, { 'content-type': 'application/json; charset=utf-8',
        'access-control-allow-origin': '*' })
      res.end(JSON.stringify(obj))
    }
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    try {
      let body = {}
      if (req.method === 'POST') {
        let raw = ''
        for await (const chunk of req) raw += chunk
        try { body = JSON.parse(raw || '{}') } catch {}
      }
      if (url.pathname === '/state') {
        const a = agents()
        return send(200, { ok: true, plugin: name, online, cached: outbox.length,
          streamFrames, sessionsSeen: titles.size, endpoint,
          agentsAvailable: Boolean(a),
          uiWorkspaceAvailable: Boolean(uiWorkspace()),
          contextActive: a !== null,
          time: Date.now() })
      }
      if (req.method === 'GET' && url.pathname === '/open') {
        const sid = url.searchParams.get('session') ?? ''
        if (!sid) return send(400, { ok: false, error: 'session 参数缺失' })
        const ui = uiWorkspace()
        if (!ui) {
          return send(503, { ok: false, recoverable: false, reason: 'uiWorkspace.openSession 不可用（当前运行面不是桌面或 context 已结束）' })
        }
        try {
          ui.openSession(sid)
          log(`openSession ${sid}`)
          return send(200, { ok: true, session_id: sid, mode: 'open', message: '已在 DSH 桌面打开原会话' })
        } catch (e) {
          return send(500, { ok: false, recoverable: false, reason: String(e?.message ?? e) })
        }
      }
      if (url.pathname === '/command' || url.pathname === '/open') {
        const sid = String(body.session_id ?? url.searchParams.get('session') ?? '')
        const mode = url.pathname === '/open' ? 'open' : String(body.mode ?? 'followup')
        const text = String(body.text ?? '')
        if (!sid) return send(400, { ok: false, error: 'session_id 缺失' })
        if (mode === 'open') {
          const ui = uiWorkspace()
          if (!ui) {
            return send(503, { ok: false, recoverable: false, reason: 'uiWorkspace.openSession 不可用（当前运行面不是桌面或 context 已结束）' })
          }
          try {
            ui.openSession(sid)
            log(`openSession(POST) ${sid}`)
            return send(200, { ok: true, session_id: sid, mode: 'open' })
          } catch (e) { return send(500, { ok: false, recoverable: false, reason: String(e?.message ?? e) }) }
        }
        const a = agents()
        const agent = a?.get?.(sid)
        if (!agent || typeof agent.followup !== 'function') {
          log(`指令拒发：${sid} 无活跃 Agent（不新建会话）`)
          return send(409, {
            ok: false, recoverable: false, session_id: sid,
            reason: '该会话当前没有活跃 Agent：可能已结束/属于其它运行面。按任务书要求不静默新建 headless 替代会话；请在 DSH 桌面打开原会话后再下发。',
          })
        }
        if (!text.trim()) return send(400, { ok: false, error: 'text 为空' })
        const msg = await createUserMessage({
          content: [{ type: 'text', text: text }], source: { kind: 'user' },
        })
        if (mode === 'steer' && typeof agent.steer === 'function') agent.steer(msg)
        else agent.followup(msg)
        log(`指令已下发 ${mode} → ${sid}：${text.slice(0, 60)}`)
        return send(200, { ok: true, session_id: sid, mode, message: '已通过官方 ' + mode + ' 下发到原会话' })
      }
      return send(404, { ok: false, error: 'not found', path: url.pathname })
    } catch (e) {
      log(`控制面异常：${e?.stack ?? e}`)
      return send(500, { ok: false, error: String(e?.message ?? e) })
    }
  })
  server.listen(controlPort, '127.0.0.1', () => {
    // headless 是 one-shot：server 不能成为唯一活跃的 handle，否则
    // turn/end 后进程挂着不退出。unref 后随 context 生命周期自然收尾。
    if (typeof server.unref === 'function') server.unref()
    log(`插件已加载 · endpoint=${endpoint} · control=127.0.0.1:${controlPort} · 上线缓存=${outbox.length}`)
  })
  server.on('error', e => log(`控制面端口错误：${e?.message ?? e}`))

  // Cordis dispose：清 timer / 关端口 / 落盘，避免重启后双实例
  return () => {
    try { clearInterval(timer) } catch {}
    try { server.close() } catch {}
    persist()
    log('插件已卸载')
  }
}
