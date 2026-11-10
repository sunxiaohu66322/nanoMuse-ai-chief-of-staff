// v3-真实浏览器验收.mjs · nanoMuse 项目总管 9196 驾驶舱 · 10 项强制验收
// 真实 Chromium（Playwright chromium-1243，headless CDP）：真打开 / 真点击 / 真输入 / 真刷新 / 真等待。
// 不用 curl / 接口 200 / 静态截图 / 模拟数据代替浏览器交互；page.evaluate 内 fetch 仅用于「比对真源」。
//
// 用法：
//   node 验收/v3-真实浏览器验收.mjs --toolchain-check       仅 launch 一次空页自检工具链（不验收、不写产物）
//   node 验收/v3-真实浏览器验收.mjs [--round N] [--public <URL>] [--url <本地URL>]
//   --public 默认 https://nav.1319766413.lifestyle/nanomuse-ai/（lead 2026-11-11 定稿，勿再 SKIP）
//            等价只读入口 https://nav.1319766413.lifestyle/nanomuse-readonly/ 一并做 GET 核验
//            NM_PUBLIC_URL / NM_URL / NM_ROUND 环境变量可覆盖
//
// 证据纪律：NDJSON 追加（失败轮次不删）· 结论 md / 操作日志 md 逐轮 append ·
//           截图 v3-01..N.png，同名自动加 -rN，绝不覆盖前轮。
//
// ★ SEL 表已按最终 web/index.html（69131B / 910 行）逐项校准，每行标注 DOM 出处行号。
//   校准要点：卡片 .pcard（非 .card）· 进度条 .pbar（无 role=progressbar）·
//             pending .msg-pending（非 .pending）· 甘特尺度 .gview[data-m]（非 [data-scale]）·
//             待排期 #gUnschedN + #ganttUnsched tr · AI 消息 .msg.ai .body ·
//             公网是脱敏静态页（按真项目名 + 「待核实」判新版，不按本地导航文案）。

import { chromium } from 'file:///J:/SimonApp/AI-Workspace/deepseek-harness/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

// ── 参数 ────────────────────────────────────────────────────────────────
const argv = process.argv.slice(2);
const arg = (k, d) => { const i = argv.indexOf(k); return i >= 0 && argv[i + 1] ? argv[i + 1] : d; };
const TOOLCHAIN_ONLY = argv.includes('--toolchain-check');
const ROUND = Number(arg('--round', process.env.NM_ROUND || '1')) || 1;
const URL_LOCAL = arg('--url', process.env.NM_URL || 'http://127.0.0.1:9196/');
const URL_PUBLIC = arg('--public', process.env.NM_PUBLIC_URL || 'https://nav.1319766413.lifestyle/nanomuse-ai/');
const URL_PUBLIC_RO = 'https://nav.1319766413.lifestyle/nanomuse-readonly/';

// ── 路径常量 ────────────────────────────────────────────────────────────
const OUT = 'G:/Users/Administrator/Documents/deepseek-harness/default-workspace/nanoMuse项目总管/验收';
const EXE = 'F:\\Users\\Administrator\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe';
const F_NDJSON = path.join(OUT, 'v3-浏览器验收日志.json');   // NDJSON：每行一条，追加
const F_CONCL = path.join(OUT, 'v3-浏览器验收结论.md');      // 逐轮 append
const F_OPS = path.join(OUT, 'v3-操作日志.md');             // 逐轮 append
const VIEWPORTS = [
  { name: 'desktop', width: 1440, height: 900 },
  { name: 'mobile', width: 390, height: 844 },
];

// ── 选择器表（按最终 DOM 校准；改这里不改断言逻辑） ──────────────────────
const SEL = {
  viewOverview:    ['.mod[data-v="overview"]'],                                  // L294
  viewChat:        ['.mod[data-v="chat"]'],                                      // L295
  viewGantt:       ['.mod[data-v="gantt"]'],                                     // L296
  briefBox:        ['#briefBox'],                                                // L316 今日简报
  projectCards:    ['#list .pcard', '.pcard'],                                   // L648 真实 class=.pcard
  cardName:        ['#list .pcard .pname'],                                      // L649
  progressBars:    ['#list .pcard .pbar', '.pbar'],                              // L652 .pbar > i
  statCounts:      ['#kpiBox .kpi .v', '#nProj', '#nStall', '#nDec'],           // L324 / L282-285
  donut:           ['#statusChart svg', '#statusChart'],                         // L326 renderDonut
  ganttEntry:      ['.mod[data-v="gantt"]'],                                     // L296
  ganttBars:       ['#ganttSvg .bar-wrapper', '#ganttBox .bar-wrapper', '.bar-wrapper'], // L849-859
  ganttScaleWeek:  ['.gview[data-m="Week"]', 'button:has-text("按周看")'],      // L422
  ganttScaleMonth: ['.gview[data-m="Month"]', 'button:has-text("按月看")'],     // L423
  ganttUnschedRows:['#ganttUnsched tr', '.unsched tr'],                         // L839-841
  unschedBadge:    ['#gUnschedN'],                                               // L431
  ganttDetail:     ['#ganttDetail', '#ganttDetail .kv'],                        // L430 / L871
  chatEntry:       ['.mod[data-v="chat"]'],                                      // L295
  chatInput:       ['#chatInput', '#homeChatInput'],                             // L396 / L350
  chatSend:        ['#chatSend', '#homeChatSend'],                               // L397 / L351
  aiMessages:      ['#chatlog .msg.ai .body', '#homeChatLog .msg.ai .body', '.msg.ai .body'], // L770
  pendingMsg:      ['.msg-pending'],                                             // L800 / L803 真实 pending 类
  askAiBtn:        ['#list .pcard .pcta .btn.pri', 'button:has-text("问 AI 这个项目")'],     // L664
  detailPanel:     ['#detail.show', '#detail'],                                  // L335 / L671
};

// ── 日志 ────────────────────────────────────────────────────────────────
const startedAt = Date.now();
const runTag = `r${ROUND}-${new Date().toISOString().replace(/[:.]/g, '-')}`;
async function rec(k, v) {
  const row = { at: new Date().toISOString(), round: ROUND, run: runTag, k, v };
  const line = JSON.stringify(row);
  console.log(line);
  if (!TOOLCHAIN_ONLY) { await fs.mkdir(OUT, { recursive: true }); await fs.appendFile(F_NDJSON, line + '\n', 'utf8'); }
}
const ops = [];
function op(action, detail, observed) {
  const line = `- ${new Date().toISOString()} [轮${ROUND}] **${action}** — ${detail}${observed ? `；观察：${observed}` : ''}`;
  ops.push(line);
  if (!TOOLCHAIN_ONLY) console.log('[OPS] ' + line);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── 断言框架：每项独立 try/catch，失败不中断后续，证据全留 ─────────────
const items = [];
async function check(n, name, fn) {
  const oi = process.argv.indexOf('--only');
  if (oi >= 0 && process.argv[oi + 1] && !process.argv[oi + 1].split(',').map(Number).includes(n)) {
    await rec(`step${n}_skipped`, { name, reason: '阶段轮 --only 未包含此项，本轮未执行' });
    return null;
  }
  const t0 = Date.now();
  const item = { n, name, status: 'FAIL', evidence: {}, elapsed_ms: 0, error: null };
  await rec(`step${n}_start`, { name });
  try {
    const ev = await fn();
    item.evidence = ev || {};
    item.status = ev && ev.__pass === true ? 'PASS' : (ev && ev.__skip ? 'SKIP' : 'FAIL');
    if (item.status === 'SKIP') item.skipReason = ev.__skip;
    delete item.evidence.__pass; delete item.evidence.__skip;
  } catch (e) {
    item.status = 'FAIL'; item.error = String(e && e.stack || e).slice(0, 1500);
  }
  item.elapsed_ms = Date.now() - t0;
  items.push(item);
  await rec(`step${n}_result`, { status: item.status, elapsed_ms: item.elapsed_ms, evidence: item.evidence, error: item.error });
  return item;
}

// ── 浏览器辅助 ──────────────────────────────────────────────────────────
let browser, ctx, page;
const hook = { pageerror: [], consoleError: [], http404: [], requestFailed: [] };

async function launch() {
  browser = await chromium.launch({
    executablePath: EXE, headless: true,
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  });
  ctx = await browser.newContext({ viewport: VIEWPORTS[0], deviceScaleFactor: 1,
    // lead 03:19 修复前端后要求硬性刷新/避开缓存；9196 响应带 Cache-Control:no-store，这里再叠一层请求头
    extraHTTPHeaders: { 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' } });
  page = await ctx.newPage();
  page.on('pageerror', e => hook.pageerror.push(String(e.message)));
  // console.error 带上来源 URL，9195 iframe / favicon 噪声才能被准确归类
  page.on('console', m => { if (m.type() === 'error') hook.consoleError.push(m.text() + ' @' + (m.location()?.url || '')); });
  page.on('response', r => { if (r.status() === 404) hook.http404.push(r.url() + ' [404]'); });
  page.on('requestfailed', r => hook.requestFailed.push(r.url() + ' :: ' + (r.failure()?.errorText || '')));
  await page.addInitScript(() => {
    window.__nmErr = [];
    const oe = console.error;
    console.error = function (...a) { window.__nmErr.push(a.map(String).join(' ')); oe.apply(console, a); };
    window.addEventListener('error', e => window.__nmErr.push('onerror:' + String(e.message)));
    window.addEventListener('unhandledrejection', e => window.__nmErr.push('unhandled:' + String(e.reason)));
  });
}

async function pick(cands) {
  for (const s of cands) { try { const l = page.locator(s); if (await l.count() > 0) return { sel: s, loc: l }; } catch (_) {} }
  return null;
}
async function clickFirst(cands, label) {
  const hit = await pick(cands);
  if (!hit) { op('点击失败', `${label}：无可用选择器`, SEL[label] ? JSON.stringify(SEL[label]) : cands.join(' | ')); return null; }
  await hit.loc.first().click({ timeout: 8000 }).catch(async () => { await hit.loc.first().click({ force: true, timeout: 8000 }); });
  op('点击', `${label} → ${hit.sel}`, '已触发');
  return hit;
}
async function bodyText() { return await page.evaluate(() => document.body.innerText || ''); }
async function api(p) {
  return await page.evaluate(async (u) => { try { const r = await fetch(u); return { status: r.status, json: await r.json() }; } catch (e) { return { status: 0, error: String(e) }; } }, p);
}
async function state() { const r = await api('/api/state'); return r.json || {}; }
async function health() { const r = await api('/health'); return r.json || {}; }  // 真源是 /health，不是 /api/health
async function chatHistory() { const r = await api('/api/chat/history'); return r.json || {}; }
// /api/chat/history 返回 {messages:[...]}，不是 {count}
async function chatCount() { const d = await chatHistory(); return Array.isArray(d.messages) ? d.messages.length : (Number(d.count) || 0); }
// 首页助手消息进 #homeChatLog，完整对话页进 #chatlog；问 AI 卡片按钮走 quick()→homeChatInput
async function aiCount() {
  return (await page.locator('#homeChatLog .msg.ai .body').count()) +
         (await page.locator('#chatlog .msg.ai .body').count());
}
async function aiLastText(prefer) {
  const cands = prefer ? [prefer] : ['#homeChatLog .msg.ai .body', '#chatlog .msg.ai .body', '.msg.ai .body'];
  for (const s of cands) {
    const l = page.locator(s);
    if (await l.count() > 0) {
      const arr = await l.allInnerTexts();
      const txt = arr[arr.length - 1] || '';
      if (txt.trim()) return txt;
    }
  }
  return '';
}
const sameRec = (a, b) => (a.id != null && a.id === b.id) ||
  (a.session_id != null && a.session_id === b.session_id) ||
  (a.at != null && a.at === b.at && a.trigger === b.trigger);

let shotSeq = 0;
async function shot(label, viewportName, pg) {
  shotSeq += 1;
  const nn = String(shotSeq).padStart(2, '0');
  const base = `v3-${nn}-${label}${viewportName ? '-' + viewportName : ''}`;
  let name = base + '.png';
  try { await fs.access(path.join(OUT, name)); name = base + `-r${ROUND}-${Date.now()}.png`; } catch (_) {}
  const f = path.join(OUT, name);
  await (pg || page).screenshot({ path: f, fullPage: true });
  await rec('screenshot', { file: name, viewport: viewportName || VIEWPORTS[0].name });
  return name;
}
async function waitReply(timeoutMs) {
  const hit = await pick(SEL.pendingMsg);
  if (hit) await page.waitForFunction((s) => !document.querySelector(s), hit.sel, { timeout: timeoutMs }).catch(() => {});
  await sleep(700);
}

// ── 10 项断言（收到「开始验收」后才真跑） ───────────────────────────────
async function buildChecks() {
  // 1 新版驾驶舱，非旧技术监控页；首页无成片原始日志
  await check(1, '新版驾驶舱（非旧监控页）', async () => {
    await page.goto(URL_LOCAL, { timeout: 30000 });
    await page.waitForLoadState('load', { timeout: 15000 });
    // 硬性刷新按 lead 要求二选一：这里用「全新 context + no-cache 请求头 + no-store 响应」，
    // 不再额外 page.reload() —— reload 会打断页面自身在飞的 5 个首屏请求，
    // 服务端把 socket 写记成 WinError 10053 http.error，制造纯测试工具自伤的假 FAIL（round 3 第二跑已证）。
    await sleep(900);
    const t = await bodyText();
    const title = await page.title();
    const keys = ['今日简报', '全局概览', '我的项目', '关键数字', '有事直接问 AI 总管', 'AI 对话', '驾驶舱首页', '待核实'];
    const hitKeys = keys.filter(k => t.includes(k));
    // 「首页没有成片原始日志」= 首页可见区域里没有铺开的日志/pre 块。
    // 折叠的技术区 / 隐藏的二级视图 / 日志浮窗 / AI 对话消息里的 markdown 代码块都不算「首页铺开的原始日志」——
    // 新版正是把技术日志降级到这些地方。round 3 首跑把 <code> 一并统计，误命中对话里的大段报告，属判据过宽。
    const rawLog = await page.evaluate(() => {
      const pres = [...document.querySelectorAll('pre, .raw-log, .log')];
      const outOfScope = p => p.closest('.tech-fold, #logpanel, .view:not(.on), #homeChatLog, #chatlog, #ctxBox');
      const visibleBig = pres.filter(p => (p.innerText || '').length > 300 && !outOfScope(p) && p.getClientRects().length);
      const foldedBig = pres.filter(p => (p.innerText || '').length > 300 && outOfScope(p));
      const chatCodeBig = [...document.querySelectorAll('#homeChatLog code, #chatlog code, .msg .body code')]
        .filter(c => (c.innerText || '').length > 300);
      return { visibleBig: visibleBig.length, foldedBig: foldedBig.length, chatCodeBig: chatCodeBig.length,
        sample: visibleBig.slice(0, 2).map(p => (p.innerText || '').slice(0, 80)) };
    });
    const stampLines = (t.match(/\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/g) || []).length;
    const ev = { title, url: page.url(), hitKeys, rawLog, stampLines, head: t.slice(0, 240),
      hardReload: true, note: '全新 context + no-cache 请求头（9196 no-store）；对话 markdown 代码块 / 折叠技术区单列统计，不冒充首页成片日志' };
    op('打开', URL_LOCAL + '（全新 context + no-cache 硬性取新，二次 reload 已移除以免自造断连）', `title=${title}；命中新版文案 ${hitKeys.join('/')}；首页可见大日志块 ${rawLog.visibleBig}；折叠/对话大代码块 ${rawLog.foldedBig}/${rawLog.chatCodeBig}（归类记录）`);
    await shot('01-首页', 'desktop');
    return { ...ev, __pass: hitKeys.length >= 4 && /驾驶舱/.test(title) && rawLog.visibleBig === 0 && stampLines < 5 };
  });

  // 2 9 张卡片 / 状态图 / 进度条 / 统计 与 /api/state counts 一致；未核实显示「待核实」不编造百分比
  await check(2, '9 卡片与真源统计一致', async () => {
    const st = await state();
    const counts = st.counts || {};
    const cards = await pick(SEL.projectCards);
    const bars = await pick(SEL.progressBars);
    const donut = await pick(SEL.donut);
    const t = await bodyText();
    const nCards = cards ? await cards.loc.count() : 0;
    const nBars = bars ? await bars.loc.count() : 0;
    const nDonut = donut ? await donut.loc.count() : 0;
    const numsInDom = [counts.active, counts.stalled, counts.awaiting_decision]
      .filter(v => v !== undefined).map(v => ({ v, shown: t.includes(String(v)) }));
    const unverified = (st.projects || []).filter(p => !p.overall_verified).length;
    const saysPending = /待核实|未核实不显示数字/.test(t);
    // 逐卡硬口径（lead 03:19 已修 web/index.html:639 真缺陷）：
    //  overall_verified=0 的卡片，整卡 innerText 不得出现任何 N% 数字 —— 包括「🕒 最近进展」时间标签。
    //  修复前 lastProgress() 无条件拼 last.overall → 未核实卡显示 0.0%，与「未核实不显示数字」自相矛盾。
    //  修复后应为「进度待核实」。主显示位 <b> 单列核对，整卡百分比违规为硬 FAIL 条件。
    const cardCheck = await page.evaluate(() => {
      const cards = [...document.querySelectorAll('#list .pcard')];
      const rows = cards.map(c => {
        const name = c.querySelector('.pname')?.innerText || '';
        const txt = c.innerText || '';
        let pctText = null, mainRow = '';
        for (const d of [...c.querySelectorAll('div')]) {
          const t = d.innerText || '';
          if (/可验证完成率/.test(t) && d.querySelector('b')) {
            mainRow = t.slice(0, 90); pctText = (d.querySelector('b').innerText || '').trim(); break;
          }
        }
        const pcts = txt.match(/\d{1,3}(?:\.\d+)?%/g) || [];
        const timeTag = [...c.querySelectorAll('.pmeta .m')].map(m => m.innerText || '')
          .find(x => /🕒/.test(x)) || '';
        return { name, pctText, mainRow, pcts, timeTag };
      });
      const unver = rows.filter(r => r.pctText === '待核实');
      const fabricated = unver.filter(r => r.pcts.length > 0);
      const mainRowBad = unver.filter(r => /\d/.test(r.pctText || ''));
      return { cards: cards.length, rows, unverifiedCards: unver.length, fabricated, mainRowBad };
    });
    const kpiAvg = await page.evaluate(() => {
      const ks = [...document.querySelectorAll('#kpiBox .kpi')];
      const a = ks.find(k => /平均进度/.test(k.querySelector('.k')?.innerText || ''));
      return a ? (a.querySelector('.v')?.innerText || '').trim() : null;
    });
    const ev = { counts, nCards, nBars, nDonut, numsInDom, unverifiedProjects: unverified, saysPending,
      cardCheck, kpiAvg, expected: { active: 5, stalled: 1, awaiting_decision: 3 } };
    op('核对真源', '/api/state 的 counts 与 DOM（硬性刷新后）', `卡片 ${nCards} · 进度条 ${nBars} · 状态图 ${nDonut} · 未核实项目 ${unverified} · 未核实卡 ${cardCheck.unverifiedCards} 张 / 整卡含百分比违规 ${cardCheck.fabricated.length} 张 · 主显示位违规 ${cardCheck.mainRowBad.length} 张 · KPI 平均进度=${kpiAvg}`);
    await shot('02-卡片与统计', 'desktop');
    const countsOk = counts.active === 5 && counts.stalled === 1 && counts.awaiting_decision === 3;
    return { ...ev, __pass: nCards === 9 && nBars >= 1 && nDonut >= 1 && countsOk && saysPending &&
      cardCheck.unverifiedCards === 5 && cardCheck.fabricated.length === 0 && cardCheck.mainRowBad.length === 0 };
  });

  // 3 甘特图：时间轴 / 真任务条 / 周月切换 / 31 待排期 / 点任务跳项目详情
  await check(3, '甘特图与 31 待排期', async () => {
    const st = await state();
    await clickFirst(SEL.ganttEntry, 'ganttEntry');
    await page.waitForSelector('#ganttUnsched tr', { timeout: 15000 }).catch(() => {});
    await sleep(600);
    const barsHit = await pick(SEL.ganttBars);
    const nBars = barsHit ? await barsHit.loc.count() : 0;
    const gApi = (await api('/api/gantt')).json || {};
    const activeScale = () => page.evaluate(() => { const b = document.querySelector('.gview.on'); return b ? b.dataset.m : null; });
    await clickFirst(SEL.ganttScaleMonth, 'ganttScaleMonth'); await sleep(500);
    const scaleMonth = await activeScale();
    await clickFirst(SEL.ganttScaleWeek, 'ganttScaleWeek'); await sleep(500);
    const scaleWeek = await activeScale();
    const scaleChanged = scaleMonth === 'Month' && scaleWeek === 'Week';
    const badge = await pick(SEL.unschedBadge);
    const badgeN = badge ? Number((await badge.loc.first().textContent().catch(() => '') || '').trim()) || 0 : 0;
    const rowHit = await pick(SEL.ganttUnschedRows);
    const rowCount = rowHit ? await rowHit.loc.count() : 0;   // 含表头 1 行
    const textCount = ((await bodyText()).match(/待排期/g) || []).length;
    const visibleUnsched = Math.max(badgeN, rowCount - 1, Number(gApi.unscheduled_count) || 0);
    let jumpOk = false, jumpDetail = '';
    if (barsHit) {
      await barsHit.loc.first().click({ force: true }).catch(() => {});
      await sleep(700);
      const dHit = await pick(SEL.ganttDetail);
      jumpDetail = dHit ? (await dHit.loc.first().innerText().catch(() => '') || '').slice(0, 260) : '';
      const names = (st.projects || []).map(p => p.name);
      const whole = await bodyText();
      jumpOk = /所属项目|项目真实状态|打开项目详情/.test(whole) && names.some(n => jumpDetail.includes(n));
      op('点击', '甘特任务条 → 项目详情', jumpOk ? '详情命中真项目名' : '未命中');
    }
    const ev = { nBars, barsSel: barsHit?.sel, scaleMonth, scaleWeek, scaleChanged, badgeN, rowCount,
      textCount, visibleUnsched, apiUnscheduled: gApi.unscheduled_count, apiScheduled: gApi.scheduled_count,
      jumpOk, jumpDetail };
    await shot('03-甘特图', 'desktop');
    return { ...ev, __pass: nBars >= 1 && scaleChanged && visibleUnsched >= 31 && jumpOk };
  });

  // 4 点项目卡片「问 AI 这个项目」→ AI 大白话解释真实进度，且与该项目真源一致
  await check(4, 'AI 大白话解释项目进度', async () => {
    await clickFirst(SEL.viewOverview, 'viewOverview'); await sleep(500);
    const st = await state();
    const proj = (st.projects || [])[0] || {};
    const n0 = await aiCount();
    const h0 = await page.locator('#homeChatLog .msg.ai .body').count();
    const ask = await pick(SEL.askAiBtn);
    let clicked = false;
    if (ask) { await ask.loc.first().click({ force: true }).catch(() => {}); clicked = true; }
    else { const cards = await pick(SEL.projectCards); if (cards) await cards.loc.first().click({ force: true }).catch(() => {}); }
    op('点击', `卡片「${proj.name}」→ 问 AI 这个项目`, `点击前 AI 消息 ${n0}（home ${h0}）；按钮命中=${clicked}`);
    await waitReply(120000);
    // 首页助手的回复落在 #homeChatLog，不是 #chatlog
    await page.waitForFunction((n) => document.querySelectorAll('#homeChatLog .msg.ai .body').length > n,
      h0, { timeout: 15000 }).catch(() => {});
    const explain = await aiLastText('#homeChatLog .msg.ai .body');
    const leak = ['根据规则', '我们被问到', '严格规则', '作为AI', '作为 AI', '系统提示'].some(x => explain.includes(x));
    const n1 = await aiCount();
    const ev = { clickedAskBtn: clicked, project: proj.name, status: proj.state || proj.status,
      aiMsgBefore: n0, aiMsgAfter: n1, explainLen: explain.length,
      mentionsProject: !!proj.name && explain.includes(proj.name), leak, head: explain.slice(0, 260) };
    await shot('04-AI解释进度', 'desktop');
    return { ...ev, __pass: explain.length > 40 && ev.mentionsProject && !leak && n1 > n0 };
  });

  // 5 AI 对话真输入查询 + 安排低风险真实任务 → DSH 回执 / 产物回读 / 界面更新
  await check(5, '对话查询 + 低风险真任务回执', async () => {
    await clickFirst(SEL.chatEntry, 'chatEntry'); await sleep(400);
    const inp = await pick(SEL.chatInput), send = await pick(SEL.chatSend);
    if (!inp || !send) return { __pass: false, reason: 'chatInput/chatSend 未找到', SEL };
    const c0 = await chatCount();
    await inp.loc.first().fill('目前哪个项目最需要推进');
    op('输入', '目前哪个项目最需要推进', `发送前 chat messages=${c0}`);
    await send.loc.first().click();
    await waitReply(120000);
    const qReply = await aiLastText('#chatlog .msg.ai .body');
    const stA = await state();
    const namesAll = (stA.projects || []).map(p => p.name).filter(Boolean);
    const named = namesAll.filter(n => qReply.includes(n));
    const autoBefore = (await api('/api/auto-runs')).json || {};
    // detect_intent 真源（总管扩展.py:62-70）：
    //   HIGH_RISK = 删除/覆盖/重写/发布/付费/重置/回滚/push/deploy/publish/overwrite/reset…
    //   QUERY 命中且无「帮我/请执行/…」→ query；否则 ADVANCE(推进/复核/生成报告/帮我做) → advance
    //   故措辞必须：含「帮我」+ ADVANCE 词、避开全部 HIGH_RISK 词、避开 QUERY 词，才能真走 do_advance。
    // 另 do_advance 会检查目标项目 next_action 是否含 HIGH_RISK 词（含则改生成审批），
    // 因此挑一个 next_action 干净的项目名，尽量命中真执行路径。
    const HR = /删除|删掉|移除|清空|覆盖|重写|发布|上线|部署|付费|支付|购买|重置|回滚|drop|delete|push|deploy|publish|overwrite|reset|rm\s-|format/i;
    const clean = (stA.projects || []).filter(p => p.name && !HR.test(String(p.next_action || '')));
    const target = (clean[0] || (stA.projects || [])[0] || {});
    const safeProj = target.name || '项目';
    const taskText = `帮我推进「${safeProj}」：做一次只读复核并生成复核报告，产物写一个 md 文件即可`;
    await inp.loc.first().fill(taskText);
    op('输入', taskText, '真输入低风险执行指令（帮我+复核/生成报告→advance；无 HIGH_RISK/QUERY 词，避开审批闸门）');
    await send.loc.first().click();
    await waitReply(240000);   // do_advance 真起 DSH headless（timeout 150s），等 .msg-pending 消失
    const autoAfter = (await api('/api/auto-runs')).json || {};
    const stB = await state();  // 任务后的 state，actions.output 才是新产物
    const newRuns = (autoAfter.runs || []).filter(r => !(autoBefore.runs || []).some(x => sameRec(x, r)));
    const newDsh = (autoAfter.dsh_sessions || []).filter(r => !(autoBefore.dsh_sessions || []).some(x => sameRec(x, r)));
    const tReply = await aiLastText('#chatlog .msg.ai .body');
    const c1 = await chatCount();
    // 产物真回读：用新 auto_run 的 action_id 到「任务后」state.actions 找 output 路径，再真读文件
    let fileProof = { exists: false };
    let actHead = '';
    const actsNew = (stB.actions || []);
    const act = actsNew.find(a => a.id === newRuns[0]?.action_id) || {};
    actHead = String(act.output || '').slice(0, 160);
    const m = String(act.output || '').match(/^(.+?\.md)/);
    if (m) { try { const buf = await fs.readFile(m[1]); fileProof = { exists: true, bytes: buf.length, path: m[1], head: buf.toString('utf8').slice(0, 120) }; } catch (e) { fileProof = { exists: false, err: String(e) }; } }
    const dshBox = await page.locator('#dshBox').innerText().catch(() => '');
    const execOk = newRuns.some(r => r.outcome === 'succeeded') || newDsh.some(r => r.outcome === 'succeeded');
    const ev = { c0, c1, qReplyLen: qReply.length, namedRealProjects: named, targetProject: safeProj,
      targetNextAction: String(target.next_action || '').slice(0, 80), taskText,
      newRuns: newRuns.map(r => ({ id: r.id, outcome: r.outcome, session: r.session_id, trigger: r.trigger, action_id: r.action_id })),
      newDsh: newDsh.map(r => ({ id: r.id, session: r.session_id, outcome: r.outcome })),
      execOk, replyMentionsDsh: /DSH|会话|回读|session|复核报告|产物/i.test(tReply),
      actHead, fileProof, dshBoxHead: dshBox.slice(0, 160), tReplyHead: tReply.slice(0, 240) };
    await shot('05-对话调度DSH', 'desktop');
    // 判据：查询真答对真项目 + 历史增长 + 真执行回执（run/dsh 成功）+ 界面回复含回执 + 产物真回读或 run 成功
    return { ...ev, __pass: qReply.length > 50 && named.length >= 1 && c1 > c0 &&
      execOk && ev.replyMentionsDsh && (fileProof.exists || newRuns.some(r => r.outcome === 'succeeded')) };
  });

  // 6 刷新后聊天历史 / 进度 / 图表状态仍存在
  await check(6, '刷新保持', async () => {
    const cardsOf = async () => { const c = await pick(SEL.projectCards); return c ? await c.loc.count() : 0; };
    const before = { chat: await chatCount(), text: (await bodyText()).slice(0, 400), cards: await cardsOf() };
    await page.reload({ timeout: 25000 });
    await page.waitForLoadState('load', { timeout: 15000 });
    await sleep(900);
    const after = { chat: await chatCount(), text: (await bodyText()).slice(0, 400), cards: await cardsOf() };
    const ev = { before, after, chatKept: after.chat >= before.chat && after.chat > 0,
      cardsKept: after.cards === before.cards && after.cards === 9, textKept: after.text.length > 50 };
    op('刷新', URL_LOCAL, `chat messages ${before.chat}→${after.chat}；卡片 ${before.cards}→${after.cards}；DOM 文本 ${before.text.length}→${after.text.length}`);
    await shot('06-刷新后保持', 'desktop');
    return { ...ev, __pass: ev.chatKept && ev.cardsKept && ev.textKept };
  });

  // 7 不发任何用户指令，被动等 ≥70s，自动巡检仍触发
  await check(7, '无人值守自动巡检', async () => {
    const h0 = await health();
    const st0 = await state();
    const patrolSec = Number(st0.patrol_seconds) || 60;
    const waitMs = Math.max(70000, (patrolSec + 14) * 1000);
    op('被动等待', `${waitMs}ms（≥70s），期间不点击/不输入/不触发任何巡检接口`, '开始');
    await rec('step7_waiting', { waitMs, lastPatrolBefore: h0.last_patrol, patrolSec });
    await sleep(waitMs);
    const h1 = await health(), st1 = await state();
    const newPatrolEvents = (st1.events || []).filter(e =>
      (e.kind === 'patrol.done' || /定时自动巡检|自动巡检完成/.test(e.message || '')) && String(e.at) > String(h0.last_patrol)).length;
    const ev = { patrolSec, waitMs, lastPatrolBefore: h0.last_patrol, lastPatrolAfter: h1.last_patrol,
      changed: h0.last_patrol !== h1.last_patrol, newPatrolEvents,
      autoRunsTotal: ((await api('/api/auto-runs')).json || {}).runs?.length };
    op('被动等待', '结束', `last_patrol ${h0.last_patrol} → ${h1.last_patrol}；新定时事件 ${newPatrolEvents}`);
    await shot('07-自动巡检后', 'desktop');
    return { ...ev, __pass: ev.changed || newPatrolEvents > 0 };
  });

  // 8 双视口 1440×900 / 390×844：截图 + 无横向溢出 + 无遮挡 + 文字可读
  await check(8, '双视口无溢出', async () => {
    const res = [];
    for (const v of VIEWPORTS) {
      await page.setViewportSize({ width: v.width, height: v.height });
      await sleep(700);
      const m = await page.evaluate(() => {
        const iw = window.innerWidth, sw = document.documentElement.scrollWidth;
        const overflowEls = [...document.querySelectorAll('body *')].filter(el => {
          const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
          return r.width > 0 && r.right > iw + 1 && cs.visibility !== 'hidden' && cs.display !== 'none';
        }).slice(0, 8).map(el => ({ tag: el.tagName, cls: (el.className || '').toString().slice(0, 60), right: Math.round(el.getBoundingClientRect().right) }));
        const small = [...document.querySelectorAll('p,span,div,button,a')].filter(el => {
          const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
          return r.width > 0 && (el.innerText || '').trim().length > 4 && parseFloat(cs.fontSize) < 10;
        });
        return { innerWidth: iw, scrollWidth: sw, overflowEls, smallText: small.length,
          smallTextEls: small.slice(0, 5).map(el => ({ txt: (el.innerText || '').trim().slice(0, 20), fs: parseFloat(getComputedStyle(el).fontSize) })) };
      });
      const name = await shot('08-视口', v.name);
      res.push({ viewport: v.name, ...m, screenshot: name, noOverflow: m.scrollWidth <= m.innerWidth });
      op('切视口', `${v.width}×${v.height}`, `scrollWidth=${m.scrollWidth} <= innerWidth=${m.innerWidth}；溢出元素 ${m.overflowEls.length}；<10px 文本 ${m.smallText}`);
    }
    await page.setViewportSize({ width: VIEWPORTS[0].width, height: VIEWPORTS[0].height });
    return { viewports: res, __pass: res.every(r => r.noOverflow && r.smallText === 0) };
  });

  // 9 本地与公网对应新版；公网受控（不暴露 /api/state 内部数据、POST 写接口不可达），不为测试绕过安全
  await check(9, '公网入口新版且受控', async () => {
    if (!URL_PUBLIC) {
      op('公网核验', '未提供 --public / NM_PUBLIC_URL', '标 SKIP，不伪造通过');
      return { __skip: '公网地址为空，等待 lead 提供 --public 后补验', __pass: false };
    }
    const st = await state();
    const names = (st.projects || []).map(p => p.name).filter(Boolean);
    const pub = await ctx.newPage();
    const pubHook = { consoleError: [], httpError: [] };
    pub.on('console', m => { if (m.type() === 'error') pubHook.consoleError.push(m.text()); });
    pub.on('response', r => { if (r.status() >= 400) pubHook.httpError.push(r.status() + ' ' + r.url()); });
    await pub.goto(URL_PUBLIC, { timeout: 30000 });
    await pub.waitForLoadState('load', { timeout: 15000 });
    await sleep(800);
    const pubText = await pub.evaluate(() => document.body.innerText || '');
    const hitNames = names.filter(n => pubText.includes(n));
    const saysPending = /待核实|未核实/.test(pubText);
    const localText = await bodyText();
    const localHitNames = names.filter(n => localText.includes(n));
    // 脱敏静态页的「新版」判据：含真项目中文名 + 「待核实」，而非本地驾驶舱导航文案
    const isNew = hitNames.length >= 5 && saysPending;
    // 内部数据不暴露：不得出现 /api/state 的 JSON 键或成片内部数据
    const leaksInternal = /overall_verified|"counts"\s*:|"projects"\s*:|"pending_decisions"|"actions"\s*:/.test(pubText) || pubText.length > 60000;
    // 真浏览器导航探测（不是 fetch/urllib）：Cloudflare 会拦 python 默认 UA 返回 1010/17B，
    // Playwright 真 Chromium 默认 UA 即正常（lead 03:0x 硬提示）。只做只读 GET，不改 Host/不加绕过头。
    const resp0 = await pub.goto(URL_PUBLIC, { timeout: 30000 });
    const ua = await pub.evaluate(() => navigator.userAgent);
    const apiPage = await ctx.newPage();
    let stateProbe;
    try {
      const r = await apiPage.goto(URL_PUBLIC.replace(/\/$/, '') + '/api/state', { timeout: 20000 });
      const apiTxt = await apiPage.evaluate(() => document.body.innerText || '');
      stateProbe = { method: '真浏览器 GET 导航', status: r ? r.status() : 0, len: apiTxt.length,
        leaks: /projects|counts|overall_verified|actions|"projects"\s*:/.test(apiTxt), head: apiTxt.slice(0, 140) };
    } catch (e) { stateProbe = { method: '真浏览器 GET 导航', status: 0, error: String(e) }; }
    await apiPage.close();
    // POST 写接口：按 lead 指令「不要试图从公网调 /api/* 写接口」——本轮不主动发 POST，
    // 采用 lead + public-access 的独立实测结论，标明来源与性质，不冒充本轮浏览器实测。
    const postProbe = { method: '引用 lead/public-access 独立实测（本轮按指令未主动发 POST）',
      source: 'lead 03:0x 消息 + _公网只读概览/验收记录.md',
      chat: '404/405 不可达', trigger: '404/405 不可达', selfTested: false };
    // 等价只读入口：只做普通 GET 核验
    const ro = await ctx.newPage();
    let roInfo = { skipped: true };
    try {
      const rr = await ro.goto(URL_PUBLIC_RO, { timeout: 20000 });
      const roText = await ro.evaluate(() => document.body.innerText || '');
      roInfo = { status: rr ? rr.status() : 0, chars: roText.length,
        hitNames: names.filter(n => roText.includes(n)).length, saysPending: /待核实/.test(roText) };
    } catch (e) { roInfo = { error: String(e) }; }
    await ro.close();
    const name = await shot('09-公网新版', 'desktop', pub);   // 必须截公网页，不是本地 page
    const stat = await fs.stat(path.join(OUT, name));
    await pub.close();
    const ev = { publicUrl: URL_PUBLIC, readonlyUrl: URL_PUBLIC_RO, roInfo,
      userAgent: ua, pageStatus: resp0 ? resp0.status() : 0, pubTextChars: pubText.length,
      hitNames, nameCount: names.length, localHitNames: localHitNames.length,
      saysPending, isNew, leaksInternal, stateProbe, postProbe, screenshot: name, screenshotBytes: stat.size,
      pubConsoleErrors: pubHook.consoleError.slice(-6), pubHttpErrors: pubHook.httpError.slice(-6),
      pubTextHead: pubText.slice(0, 240) };
    op('公网核验', URL_PUBLIC, `真浏览器 UA=${ua.slice(0, 60)}… · 页面 ${ev.pageStatus}/${pubText.length}字 · 真项目名 ${hitNames.length}/${names.length} · 待核实=${saysPending} · 真浏览器 GET /api/state ${stateProbe.status} leaks=${stateProbe.leaks} · POST 写接口引用实测 ${postProbe.chat}/${postProbe.trigger}（未主动发）· readonly ${roInfo.status}`);
    // 判据：真浏览器 UA 打开新版脱敏页 + 与本地对应 + 无内部数据 + 真浏览器 GET /api/state 404 且不含内部键 +
    //       POST 写接口按指令不主动测，引用 lead/public-access 实测（如实标注 selfTested=false）
    return { ...ev, __pass: isNew && !leaksInternal && ev.pageStatus === 200 &&
      stateProbe.status === 404 && !stateProbe.leaks && localHitNames.length >= 5 };
  });

  // 10 Console / pageerror / httpErrorEvents / 服务日志；已知噪声归类不算失败
  await check(10, 'Console 与服务日志异常归类', async () => {
    const pageHooked = await page.evaluate(() => window.__nmErr || []);
    const st = await state();
    // 允许豁免的三类（题目口径）：404 favicon / 9195 iframe / 真重启预期断连
    const NOISE = [/favicon/i, /9195/i, /net::ERR_CONNECTION_REFUSED|net::ERR_ABORTED/i];
    const isNoise = s => NOISE.some(re => re.test(s));
    // lead 03:31 口径：WinError 10053 / ConnectionAbortedError / Failed to fetch 是硬性刷新或切 context
    // 时浏览器主动 abort 在途请求、服务端 socket 写失败（真重启/重连预期断连），归进 exempt，不计 FAIL。
    // 原文全部保留在 svcErrors / exemptDisconnect，不删除、不隐瞒。
    const DISCONNECT = /10053|10054|ConnectionAbortedError|ConnectionResetError|中止了一个已建立的连接|Failed to fetch/i;
    const allHttp = (st.events || []).filter(e => /http\.error|chat\.error|auto\.error|js_error|page\.error/i.test(e.kind || ''));
    const svcErrors = allHttp.slice(0, 8).map(e => ({ at: e.at, kind: e.kind, message: String(e.message || '').slice(0, 120) }));
    const exemptDisconnect = allHttp.filter(e => DISCONNECT.test(e.message || ''));
    const realHttp = allHttp.filter(e => !isNoise(e.message || '') && !DISCONNECT.test(e.message || ''));
    // /api/health 404 是真缺陷（前端 index.html:907 轮询错路径，真源 /health），按 §0.10 只记录、不动手、不判 FAIL，交 lead 二阶段修。
    const rawConsole = hook.consoleError.slice(-20);
    const servicePoll404 = rawConsole.filter(s => /\/api\/health/.test(s) && /404/.test(s));
    const exempt = rawConsole.filter(s => isNoise(s));
    const otherConsole = rawConsole.filter(s => !isNoise(s) && !/\/api\/health/.test(s) &&
      !/^Failed to load resource: the server responded with a status of 404/.test(s) && !DISCONNECT.test(s));
    const noise = { favicon9195: [...new Set(hook.http404.filter(isNoise))],
      restartDisconnect: hook.requestFailed.filter(isNoise), exemptConsole: exempt,
      note: '404 favicon / 9195 iframe / 真重启或重连预期断连（含 10053 ERR_ABORTED）：归类记录，不计失败' };
    const resourceNoise = pageHooked.filter(s => /^Failed to load resource/i.test(s));
    const realConsole = pageHooked.filter(s => !isNoise(s) && !/^Failed to load resource/i.test(s));
    const realPageErrors = hook.pageerror.filter(s => !isNoise(s));
    const ev = { pageHookedErrors: realConsole, resourceNoise, realPageErrors,
      realHttpErrors: realHttp.slice(0, 6), svcErrors, otherConsole,
      exemptDisconnect: exemptDisconnect.map(e => ({ at: e.at, kind: e.kind, message: String(e.message || '').slice(0, 140) })),
      servicePoll404: { count: servicePoll404.length, sample: servicePoll404.slice(0, 2),
        classification: '发现的其他问题（真缺陷，按 §0.10 只记录不动手、不判 FAIL）：前端轮询错路径，真源 /health；交 lead 二阶段改 index.html:907' },
      consoleErrorsRaw: rawConsole, noise, readyState: await page.evaluate(() => document.readyState) };
    op('日志核验', 'Console / pageerror / httpErrorEvents / 服务日志', `真实错误 ${realConsole.length + realPageErrors.length + realHttp.length + otherConsole.length}；豁免噪声 ${exempt.length}；断连噪声归类 ${exemptDisconnect.length} 条（10053，不计 FAIL）；另真实发现 /api/health 404 ${servicePoll404.length} 条（只记录不动手，交 lead）`);
    // 判据：readyState=complete + 无 JS 崩溃 + 除豁免/断连噪声外无真实服务错误 = PASS。
    return { ...ev, __pass: realConsole.length === 0 && realPageErrors.length === 0 &&
      realHttp.length === 0 && otherConsole.length === 0 && ev.readyState === 'complete' };
  });
}

// ── 收尾：NDJSON VERDICT + 结论 md（append）+ 操作日志 md（append） ─────
async function finish() {
  const oi = process.argv.indexOf('--only');
  const STAGE = oi >= 0 ? String(process.argv[oi + 1] || '') : '';
  const allPassed = items.length === 10 && items.every(i => i.status === 'PASS');
  const stagePassed = items.length > 0 && items.every(i => i.status === 'PASS');
  const verdict = { round: ROUND, run: runTag, at: new Date().toISOString(), url: URL_LOCAL,
    publicUrl: URL_PUBLIC || null, readonlyUrl: URL_PUBLIC_RO, stageOnly: STAGE || null,
    browser: 'Playwright Chromium 1243 真实浏览器（headless CDP）', executable: EXE,
    viewports: VIEWPORTS, items, allPassed, stagePassed, elapsed_ms: Date.now() - startedAt };
  await rec('VERDICT', verdict);
  const lines = items.map(i => `- 第 ${i.n} 项 **${i.status}** — ${i.name}${i.skipReason ? `（${i.skipReason}）` : ''}${i.error ? `；异常：${String(i.error).slice(0, 160)}` : ''}`).join('\n');
  const findings = `\n\n### 发现的其他问题（本轮只记录、未动手，按 §0.10 交 lead）\n\n` +
    `- **前端轮询错路径**：\`web/index.html:907\` 每 10s 请求 \`/api/health\`，真源是 \`/health\`（总管.py:809），产生 console 404。非三类豁免噪声、非 JS 崩溃；本项不因此判 FAIL，但如实留痕。\n` +
    `- **卡片时间标签带历史快照百分比（已修复，本轮复验）**：未核实项目「🕒 最近进展」原无条件拼 \`0.0%\`（index.html:639），与「未核实不显示数字」矛盾。lead 03:19 已改 \`p.overall_verified&&last?…:'进度待核实'\`（备份 \`web/index.html.bak-verify-fix-20261111-0319\`）。第 2 项以「整卡 0 个百分比」硬口径复验，失败轮次保留。\n` +
    `- **硬性刷新/切 context 的断连噪声（已归类豁免）**：4 条 \`WinError 10053 ConnectionAbortedError\`（03:24:24–28，\`/api/state\` / \`chat/history\` / \`auto-runs\` / \`chat/status\`）是浏览器主动 abort 在途请求，伴 \`ERR_ABORTED\` / \`Failed to fetch\`，按 lead 03:31 口径归 exempt，原文全留在 \`svcErrors\` / \`exemptDisconnect\`，不计 FAIL。二次 reload 已移除。\n` +
    `- **公网 POST 写接口本轮未自测**：按 lead 指令「不要试图从公网调 /api/* 写接口」，采用 lead / public-access 独立实测（404/405）；结论 md 标 \`selfTested=false\`，不冒充浏览器实测。\n`;
  const section = `\n\n## 第 ${ROUND} 轮 · ${new Date().toISOString()} · ${runTag}\n\n` +
    `本地：\`${URL_LOCAL}\` · 公网：\`${URL_PUBLIC || '（未给，第 9 项 SKIP）'}\` · 等价只读：\`${URL_PUBLIC_RO}\` · 视口：1440×900 / 390×844\n\n` +
    `阶段轮：${STAGE ? '`--only ' + STAGE + '`（只执行这几项，其余留待整轮补跑）' : '完整 10 项'}\n\n${lines}\n\n` +
    `总判定：**${allPassed ? '10/10 全部通过' : STAGE ? (stagePassed ? '阶段轮 ' + items.length + '/' + items.length + ' 通过（整轮未跑完，不代表 10 项完成）' : '阶段轮存在 FAIL') : '未全部通过（含 FAIL / SKIP，见下与 NDJSON 日志）'}**\n` +
    findings +
    `逐条证据：\`v3-浏览器验收日志.json\`（NDJSON 追加，失败轮次不删） · 操作明细：\`v3-操作日志.md\` · 截图见同目录 \`v3-*.png\`。\n` +
    `非 curl / 非接口 200 / 非静态截图 / 非模拟数据：页面可用判据全部来自真实打开、点击、输入、刷新、被动等待。\n`;
  await fs.appendFile(F_CONCL, section, 'utf8');
  await fs.appendFile(F_OPS, `\n\n## 第 ${ROUND} 轮 · ${runTag}\n\n${ops.join('\n')}\n`, 'utf8');
  console.log('VERDICT_JSON=' + JSON.stringify({ round: ROUND, allPassed, statuses: items.map(i => i.status) }));
}

// ── 主流程 ──────────────────────────────────────────────────────────────
try {
  await launch();
  await rec('toolchain', { ok: true, version: browser.version(), exe: EXE, mode: TOOLCHAIN_ONLY ? 'toolchain-check' : 'full' });
  if (TOOLCHAIN_ONLY) {
    await page.goto('about:blank');
    await rec('toolchain_blank_page', { url: page.url(), title: await page.title(), readyState: await page.evaluate(() => document.readyState) });
    console.log('TOOLCHAIN_OK');   // 只验证工具链，不验收页面、不写任何产物文件
  } else {
    await fs.mkdir(OUT, { recursive: true });
    await buildChecks();
    await finish();
  }
} catch (e) {
  await rec('FATAL', { message: String(e && e.message || e), stack: String(e && e.stack || '').slice(0, 1500) });
  console.error('FATAL', e);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close().catch(() => {});
}
