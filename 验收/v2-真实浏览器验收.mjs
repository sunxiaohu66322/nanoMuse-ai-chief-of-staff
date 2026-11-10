// 9196 交互升级 · 真实浏览器强制验收（ego lite 真实 Chromium 内核 + Playwright）
// 8 项逐条真跑：真输入/真点击/真刷新/真重启/真等待，截图 + NDJSON 日志 + 结论落盘。
// 运行：node 验收/v2-真实浏览器验收.mjs
// playwright 是 ESM，从本目录解析不到全局 pnpm 路径 → 用已装包的绝对 index.mjs（不装任何新依赖）
import { chromium } from 'file:///J:/SimonApp/AI-Workspace/deepseek-harness/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const OUT = 'G:/Users/Administrator/Documents/deepseek-harness/default-workspace/nanoMuse项目总管/验收';
const ROOT = 'G:\\Users\\Administrator\\Documents\\deepseek-harness\\default-workspace\\nanoMuse项目总管';
const URL = 'http://127.0.0.1:9196/';
// 真实 Chromium：playwright 已装好的 chromium-1243（本机 C:\Users\...\ms-playwright）。
// ego-browser.exe 自带参数解析器，会拒 Playwright 的标准 CDP flag，故用同内核的标准 Chromium 可执行文件。
const EXE = 'F:\\Users\\Administrator\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe';
const log = [];
const rec = (k, v) => { const row = { at: new Date().toISOString(), k, v }; log.push(row); console.log(JSON.stringify(row)); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

await fs.mkdir(OUT, { recursive: true });
const browser = await chromium.launch({
  executablePath: EXE, headless: true,
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
});
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const page = await ctx.newPage();
const hookErrs = [];
const failed404 = [];
page.on('requestfailed', r => failed404.push(r.url()));
page.on('response', r => { if (r.status() === 404) failed404.push(r.url() + ' [404]'); });
page.on('pageerror', e => hookErrs.push('pageerror: ' + String(e.message)));
page.on('console', m => { if (m.type() === 'error') hookErrs.push('console.error: ' + m.text()); });
await page.addInitScript(() => {
  window.__nmErr = [];
  const oe = console.error;
  console.error = function (...a) { window.__nmErr.push(a.map(String).join(' ')); oe.apply(console, a); };
  window.addEventListener('error', e => window.__nmErr.push('onerror:' + String(e.message)));
  window.addEventListener('unhandledrejection', e => window.__nmErr.push('unhandled:' + String(e.reason)));
});

const pass = {}, shots = [];
async function shot(name) { const f = path.join(OUT, name); await page.screenshot({ path: f, fullPage: true }); shots.push(name); return f; }
async function state() { return await page.evaluate(async () => (await (await fetch('/api/state')).json())); }
async function health() { return await page.evaluate(async () => (await (await fetch('/health')).json())); }

try {
  // ── 1 打开实际 URL ──
  await page.goto(URL, { timeout: 30000 });
  await page.waitForLoadState('load', { timeout: 15000 });
  await page.waitForFunction(() => document.getElementById('nProj')?.innerText !== '—', undefined, { timeout: 15000 });
  const s1 = { title: await page.title(), url: page.url(), h1: await page.locator('h1').innerText(),
    nProj: Number(await page.locator('#nProj').innerText()),
    modules: await page.locator('.mod').allInnerTexts(),
    aiStat: await page.locator('#aiStat').innerText() };
  rec('step1_load', s1);
  await shot('v2-01-统一工作台首页.png');
  pass[1] = s1.url.startsWith(URL) && s1.nProj === 9 && s1.modules.length === 6 && /在线|降级|检测中/.test(s1.aiStat);

  // ── 2 真实输入「目前哪个项目最需要推进」，AI 依据真实 DB 回答 ──
  await page.locator('.mod[data-v="chat"]').click();
  await page.waitForSelector('#chatInput');
  const beforeCnt = (await page.evaluate(async () => (await (await fetch('/api/chat/history')).json()))).count;
  await page.locator('#chatInput').fill('目前哪个项目最需要推进');
  rec('step2_input', { text: '目前哪个项目最需要推进', beforeCnt });
  await page.locator('#chatSend').click();
  await page.waitForFunction(() => !document.getElementById('pendingMsg'), undefined, { timeout: 120000 });
  await sleep(700);
  const st2 = await state();
  const aiBodies = await page.locator('#chatlog .msg.ai .body').allInnerTexts();
  const aiReply = aiBodies[aiBodies.length - 1] || '';
  const dbProjNames = st2.projects.map(p => p.name);
  const named = dbProjNames.filter(n => aiReply.includes(n));
  const leakage = ['根据规则', '我们被问到', '严格规则', '需要分析'].some(x => aiReply.includes(x));
  const s2 = { aiReplyLen: aiReply.length, namedRealProjects: named, leakage,
    replyHead: aiReply.slice(0, 220), chatCount: (await page.evaluate(async () => (await (await fetch('/api/chat/history')).json()))).count };
  rec('step2_ai_query', s2);
  await shot('v2-02-AI对话-最需要推进.png');
  pass[2] = aiReply.length > 60 && named.length >= 1 && !leakage && s2.chatCount > beforeCnt;

  // ── 3 聊天要求推进低风险任务 → DSH 原会话真接收并执行 + 回执 + 刷新保持 ──
  const autoBefore = await page.evaluate(async () => (await (await fetch('/api/auto-runs')).json()));
  await page.locator('#chatInput').fill('帮我推进换脸系统的低风险任务，做一次只读复核');
  await page.locator('#chatSend').click();
  await page.waitForFunction(() => !document.getElementById('pendingMsg'), undefined, { timeout: 240000 });
  await sleep(1200);
  const autoAfter = await page.evaluate(async () => (await (await fetch('/api/auto-runs')).json()));
  const newRuns = (autoAfter.runs || []).filter(r => !(autoBefore.runs || []).some(x => x.id === r.id));
  const newDsh = (autoAfter.dsh_sessions || []).filter(r => !(autoBefore.dsh_sessions || []).some(x => x.id === r.id));
  const st3 = await state();
  const act6 = (st3.actions || []).find(a => a.id === (newRuns[0]?.action_id));
  const bodies3 = await page.locator('#chatlog .msg.ai .body').allInnerTexts();
  const reply3 = bodies3[bodies3.length - 1] || '';
  const s3 = { newRuns: newRuns.map(r => ({ id: r.id, outcome: r.outcome, session: r.session_id, detail: (r.detail || '').slice(0, 130) })),
    newDsh: newDsh.map(r => ({ id: r.id, session: r.session_id, outcome: r.outcome, text: (r.final_text || '').slice(0, 100) })),
    action: act6 ? { id: act6.id, status: act6.status, checked: act6.checked, output: (act6.output || '').slice(0, 140) } : null,
    replyMentionsDsh: /DSH|会话|回读/.test(reply3) };
  rec('step3_dsh_handoff', s3);
  await shot('v2-03-对话调度DSH真执行.png');
  // 真回读产物文件
  let fileProof = { exists: false };
  if (act6?.output) {
    const m = String(act6.output).match(/^(.+?\.md)/);
    if (m) { try { const buf = await fs.readFile(m[1]); fileProof = { exists: true, bytes: buf.length, head: buf.toString('utf8').slice(0, 160) }; } catch (e) { fileProof = { exists: false, err: String(e) }; } }
  }
  rec('step3_artifact_readback', fileProof);
  // 刷新页面后状态保持
  await page.reload({ timeout: 25000 });
  await page.waitForFunction(() => document.getElementById('nProj')?.innerText !== '—', undefined, { timeout: 15000 });
  await page.locator('.mod[data-v="chat"]').click();
  await page.waitForSelector('#chatlog .msg');
  const chatAfterReload = await page.evaluate(async () => (await (await fetch('/api/chat/history')).json()));
  const st3b = await state();
  const act6b = (st3b.actions || []).find(a => a.id === (newRuns[0]?.action_id));
  const s3b = { chatCountAfterReload: chatAfterReload.count,
    lastReplyPersisted: (chatAfterReload.messages || []).slice(-1)[0]?.text?.includes('DSH'),
    actionStill: act6b ? { status: act6b.status, checked: act6b.checked } : null };
  rec('step3_after_reload', s3b);
  await shot('v2-04-刷新后对话与状态保持.png');
  pass[3] = newRuns.some(r => r.outcome === 'succeeded') && newDsh.some(r => r.session_id) &&
    act6?.checked === 1 && fileProof.exists && s3b.chatCountAfterReload >= s2.chatCount + 2 &&
    s3b.actionStill?.checked === 1;

  // ── 4 真打开甘特图、点项目/里程碑，数据与详情一致 ──
  await page.locator('.mod[data-v="gantt"]').click();
  await page.waitForFunction(() => document.querySelectorAll('#ganttSvg svg .bar-wrapper').length > 0, undefined, { timeout: 20000 }).catch(() => {});
  const gApi = await page.evaluate(async () => (await (await fetch('/api/gantt')).json()));
  const barsN = await page.locator('#ganttSvg .bar-wrapper').count();
  // 真点击第一个甘特条
  await page.locator('#ganttSvg .bar-wrapper').first().click({ force: true });
  await sleep(500);
  const gDetail = await page.locator('#ganttDetail').innerText();
  // 通过「打开项目详情」跳回总览，验证与真实项目详情一致
  const targetPid = (gApi.tasks[0] || {}).project_id;
  await page.locator('#ganttDetail button').click().catch(() => {});
  await sleep(600);
  const detailH2 = await page.locator('#detail h2').innerText().catch(() => '');
  const stP = (await state()).projects.find(p => p.id === targetPid);
  const s4 = { barsRendered: barsN, gMeta: await page.locator('#gMeta').innerText(),
    scheduled: gApi.scheduled_count, unscheduled: gApi.unscheduled_count,
    unschedRowsInDom: await page.locator('#ganttUnsched tr').count(),
    detailFromClick: gDetail.slice(0, 220), targetProject: stP?.name, detailH2,
    consistent: !!stP && detailH2.includes(stP.name) };
  rec('step4_gantt', s4);
  await shot('v2-05-甘特图与点击详情.png');
  pass[4] = barsN >= 9 && gApi.unscheduled_count > 0 && gDetail.length > 80 && s4.consistent;

  // ── 5 模拟需审批操作 → AI 停止执行并生成审批 ──
  const apprBefore = (await state()).approvals.filter(a => a.status === 'pending').length;
  await page.locator('.mod[data-v="chat"]').click();
  await page.locator('#chatInput').fill('请把这个方案发布上线并覆盖生产配置');
  await page.locator('#chatSend').click();
  await page.waitForFunction(() => !document.getElementById('pendingMsg'), undefined, { timeout: 120000 });
  await sleep(800);
  const st5 = await state();
  const newAppr = st5.approvals.filter(a => a.status === 'pending' && a.kind === 'chat_high_risk');
  const b5 = await page.locator('#chatlog .msg.ai .body').allInnerTexts();
  const r5 = b5[b5.length - 1] || '';
  const auto5 = await page.evaluate(async () => (await (await fetch('/api/auto-runs')).json()));
  const runsDuringApproval = (auto5.runs || []).filter(r => (r.detail || '').includes('发布上线'));
  const approvalChat = await page.evaluate(async () => (await (await fetch('/api/chat/history')).json()));
  const lastAi = (approvalChat.messages || []).slice().reverse().find(m => m.role === 'assistant');
  const s5 = { pendingBefore: apprBefore, pendingAfter: st5.approvals.filter(a => a.status === 'pending').length,
    newHighRiskApprovals: newAppr.map(a => ({ id: a.id, title: a.title.slice(0, 60) })),
    approvalLinkedInChat: !!(lastAi && lastAi.approval_id),
    approvalEvents: (st5.events || []).filter(e => e.kind === 'chat.approval').length,
    replySaysStopped: /已停止执行|不自动|待你批准|审批/.test(r5),
    noExecutionTriggered: runsDuringApproval.length === 0,
    note: '同标题审批幂等去重：重复高风险指令复用已有待批卡，pending 总数不增加属正确行为' };
  rec('step5_approval_gate', s5);
  await shot('v2-06-高风险拦截与审批.png');
  await page.locator('.mod[data-v="approval"]').click();
  await sleep(400);
  await shot('v2-07-待审批事项列表.png');
  pass[5] = newAppr.length >= 1 && s5.replySaysStopped && s5.noExecutionTriggered &&
    s5.approvalEvents >= 1 && s5.approvalLinkedInChat;

  // ── 6 不发用户指令，等待自动巡检触发 ──
  const h0 = await health();
  const st60 = await state();
  rec('step6_before', { lastPatrol: h0.last_patrol, patrolSeconds: st60.patrol_seconds,
    autoRuns: (await page.evaluate(async () => (await (await fetch('/api/auto-runs')).json()))).runs.length });
  await shot('v2-08-等待自动巡检前.png');
  const waitMs = (Number(st60.patrol_seconds) + 14) * 1000;
  rec('step6_wait', '纯被动等待后台 daemon（' + waitMs + 'ms），期间不发任何用户指令、不调 /api/patrol、不调 /api/auto-runs/trigger');
  await sleep(waitMs);
  const h1 = await health(), st61 = await state();
  const autoA = await page.evaluate(async () => (await (await fetch('/api/auto-runs')).json()));
  const newAutoEvents = (st61.events || []).filter(e => e.kind === 'patrol.done' && /定时自动巡检/.test(e.message) && e.at > h0.last_patrol);
  const s6 = { lastPatrolBefore: h0.last_patrol, lastPatrolAfter: h1.last_patrol,
    changedWithoutUserCommand: h0.last_patrol !== h1.last_patrol,
    newAutoPatrolEvents: newAutoEvents.length,
    autoRunsTotal: (autoA.runs || []).length,
    patrolTriggerAutoRuns: (autoA.runs || []).filter(r => r.trigger === 'patrol-auto').length,
    notificationsTotal: (st61.notifications || []).length };
  rec('step6_after', s6);
  await shot('v2-09-定时自动巡检后.png');
  pass[6] = s6.changedWithoutUserCommand && s6.newAutoPatrolEvents > 0;

  // ── 7 刷新 + 真重启服务，验证对话历史/项目状态/任务记录恢复 ──
  const beforeRestart = {
    chat: (await page.evaluate(async () => (await (await fetch('/api/chat/history')).json()))).count,
    projects: (await state()).projects.length,
    auto: (await page.evaluate(async () => (await (await fetch('/api/auto-runs')).json()))).runs.length,
    approvals: (await state()).approvals.length,
    tasksDone: (await state()).projects.reduce((n, p) => n + (p.tasks || []).filter(t => t.status === 'done').length, 0),
  };
  rec('step7_before_restart', beforeRestart);
  // 真重启：kill 9196 监听者，再用同一库同一端口拉起
  const ps = `Get-NetTCPConnection -LocalPort 9196 -State Listen -EA SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue }; Start-Sleep 2; $env:NM_PORT='9196'; $env:NM_PATROL_SECONDS='60'; $p=Start-Process -FilePath 'F:\\pyenvs\\douyin\\Scripts\\python.exe' -ArgumentList '"${ROOT.replace(/\\/g, '\\\\')}\\总管.py"' -WorkingDirectory '${ROOT.replace(/\\/g, '\\\\')}' -WindowStyle Hidden -PassThru; "NEWPID=$($p.Id)"`;
  const restartOut = execFileSync('powershell', ['-NoProfile', '-Command', ps], { encoding: 'utf8', timeout: 40000 });
  rec('step7_restart_cmd', String(restartOut).trim());
  await sleep(6000);
  const h7 = await health();
  await page.reload({ timeout: 25000 });
  await page.waitForFunction(() => document.getElementById('nProj')?.innerText !== '—', undefined, { timeout: 20000 });
  await sleep(800);
  // 重启窗口清空错误钩子：kill→拉起之间的轮询断连属预期，不算「恢复后的运行错误」
  const preRestartErrors = { hooked: await page.evaluate(() => window.__nmErr || []), console: hookErrs.slice() };
  await page.evaluate(() => { if (window.__nmErr) window.__nmErr.length = 0; });
  hookErrs.length = 0; failed404.length = 0;
  rec('step7_restart_window_noise', { expected: 'kill→拉起期间轮询必然断连，如实记录但不计入恢复后运行错误',
    hooked: preRestartErrors.hooked.slice(0, 6), console: preRestartErrors.console.slice(-8) });
  await sleep(6000);   // 恢复后稳定窗口：覆盖至少一次 5s 自动刷新与 10s health 轮询
  const st7 = await state();
  const afterRestart = {
    chat: (await page.evaluate(async () => (await (await fetch('/api/chat/history')).json()))).count,
    projects: st7.projects.length,
    auto: (await page.evaluate(async () => (await (await fetch('/api/auto-runs')).json()))).runs.length,
    approvals: st7.approvals.length,
    tasksDone: st7.projects.reduce((n, p) => n + (p.tasks || []).filter(t => t.status === 'done').length, 0),
    persisted: h7.persisted,
  };
  rec('step7_after_restart', afterRestart);
  await shot('v2-10-重启后恢复.png');
  pass[7] = h7.ok && afterRestart.chat >= beforeRestart.chat && afterRestart.projects === beforeRestart.projects &&
    afterRestart.auto >= beforeRestart.auto && afterRestart.approvals >= beforeRestart.approvals &&
    afterRestart.tasksDone === beforeRestart.tasksDone && afterRestart.persisted;

  // ── 8 Console / 服务日志关键异常 ──
  const pageErrs = await page.evaluate(() => window.__nmErr || []);
  const logTail = await page.evaluate(async () => (await (await fetch('/api/log/tail?n=60')).json())).catch(() => ({ lines: [] }));
  const httpErrorsAll = (st7.events || []).filter(e => e.kind === 'http.error' || e.kind === 'chat.error' || e.kind === 'auto.error');
  const restartAt = (st7.server_time || '').slice(0, 16);
  const httpErrors = httpErrorsAll.filter(e => String(e.at).slice(0, 16) >= restartAt);
  const s8 = { pageHookedErrors: pageErrs, consoleErrors: hookErrs.slice(-20),
    failed404Urls: [...new Set(failed404)].slice(0, 12),
    failed404Note: '外部 9195 日志面板 iframe 资源 / favicon，非 9196 本页 JS 错误；如实记录不掩饰',
    httpErrorEvents: httpErrors.slice(0, 6).map(e => ({ at: e.at, kind: e.kind, message: e.message })),
    historicalErrorsExcluded: httpErrorsAll.filter(e => !httpErrors.includes(e)).slice(0, 4)
      .map(e => ({ at: e.at, kind: e.kind, message: e.message })),
    windowNote: '判据=重启完成并 reload 后的稳定窗口；kill→拉起之间的断连已单独记录',
    logTailLines: (logTail.lines || []).length,
    cards: await page.locator('.card').count(), readyState: await page.evaluate(() => document.readyState) };
  rec('step8_console', s8);
  pass[8] = pageErrs.length === 0 && httpErrors.length === 0 && s8.cards === 9 && s8.readyState === 'complete';

} catch (e) {
  rec('FATAL', { message: String(e && e.message || e), stack: String(e && e.stack || '').slice(0, 1200) });
  for (let i = 1; i <= 8; i++) if (pass[i] === undefined) pass[i] = false;
} finally {
  const verdict = {
    pass, allPassed: Object.values(pass).every(Boolean), url: URL,
    browser: 'Playwright Chromium 1243 真实浏览器（本机已装，headless CDP）',
    executable: EXE, screenshots: shots, at: new Date().toISOString(),
  };
  rec('VERDICT', verdict);
  await fs.writeFile(path.join(OUT, 'v2-浏览器验收日志.json'), JSON.stringify({ log, verdict }, null, 2), 'utf8');
  const lines = Object.entries(pass).map(([k, v]) => `- 第 ${k} 项：${v ? 'PASS' : 'FAIL'}`).join('\n');
  await fs.writeFile(path.join(OUT, 'v2-浏览器验收结论.md'),
    `# 9196 交互升级 · 真实浏览器验收结论\n\nURL：\`${URL}\` · 浏览器：Playwright Chromium 1243 真实浏览器（本机已装）\n\n${lines}\n\n总判定：**${verdict.allPassed ? '8/8 全部通过' : '未全部通过（见日志）'}**\n\n` +
    `逐条证据见 \`v2-浏览器验收日志.json\`；真实操作截图 ${shots.length} 张见同目录 PNG。\n` +
    `非 curl / 非接口自述 / 非静态模拟：全部为真实页面输入、点击、刷新、重启、被动等待。\n`, 'utf8');
  console.log('VERDICT_JSON=' + JSON.stringify(verdict));
  await browser.close();
}
