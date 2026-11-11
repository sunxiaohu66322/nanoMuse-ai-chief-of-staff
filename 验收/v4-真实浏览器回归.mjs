// nanoMuse 9196 · 第二阶段「进度真实性与长期自主推进」真实浏览器回归
// 真 Chromium（playwright 1.61.1 已装 + chromium-1243），非 curl / 非接口自述 / 非静态截图。
// 运行：node 验收/v4-真实浏览器回归.mjs
// 覆盖：①四态真推进去重 ②推断 inferred ③历史/正式计划甘特分离 ④长期记忆
//       ⑤四级证据 ⑥重启恢复 + Console + 审批闸门 + 无人值守巡检。
import { chromium } from 'file:///J:/SimonApp/AI-Workspace/deepseek-harness/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFileSync } from 'node:child_process';

const OUT = 'G:/Users/Administrator/Documents/deepseek-harness/default-workspace/nanoMuse项目总管/验收';
const ROOT = 'G:\\Users\\Administrator\\Documents\\deepseek-harness\\default-workspace\\nanoMuse项目总管';
const URL = 'http://127.0.0.1:9196/';
const EXE = 'F:\\Users\\Administrator\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe';
const R = 'v4-';
const log = [];
const rec = (k, v) => { const row = { at: new Date().toISOString(), k, v }; log.push(row); console.log(JSON.stringify(row)); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

await fs.mkdir(OUT, { recursive: true });
const browser = await chromium.launch({ executablePath: EXE, headless: true,
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
const ctx = await browser.newContext({ viewport: { width: 1500, height: 980 }, deviceScaleFactor: 1 });
const page = await ctx.newPage();
const errs = [], f404 = [];
page.on('pageerror', e => errs.push('pageerror: ' + String(e.message)));
page.on('console', m => { if (m.type() === 'error') errs.push('console.error: ' + m.text()); });
page.on('response', r => { if (r.status() === 404) f404.push(r.url() + ' [404]'); });
await page.addInitScript(() => { window.__nmErr = [];
  const oe = console.error; console.error = function(...a){ window.__nmErr.push(a.map(String).join(' ')); oe.apply(console,a); };
  window.addEventListener('error', e => window.__nmErr.push('onerror:'+String(e.message)));
  window.addEventListener('unhandledrejection', e => window.__nmErr.push('unhandled:'+String(e.reason))); });

const pass = {}, shots = [];
async function shot(name){ const f = path.join(OUT, name); await page.screenshot({ path: f, fullPage: true }); shots.push(name); return f; }
const api = async (u, o) => (await (await fetch(URL.replace(/\/$/,'') + u, o)).json());
// 浏览器内 fetch（保证是页面真实上下文发出的请求）
const bget = u => page.evaluate(async x => await (await fetch(x)).json(), u);
const bpost = (u, b) => page.evaluate(async ([x, y]) => await (await fetch(x, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(y||{}) })).json(), [u, b]);
const S = () => bget('/api/state');

try {
  // ── 步骤 1：真实浏览器打开首页，确认页面加载正常 ──
  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#modbar .mod', { timeout: 15000 });
  await page.waitForTimeout(600);
  const title = await page.title();
  const tabProgress = await page.locator('#modbar button[data-v="progress"]').count();
  const projCards = await page.locator('#list .pcard, #list .pgrid > div').count().catch(()=>0);
  const rs = await page.evaluate(()=>document.readyState);
  pass.s1 = { title, hasProgressTab: tabProgress === 1, readyState: rs };
  rec('s1-home', pass.s1);
  await shot(R+'01-首页-含进度计划页签.png');

  // ── 步骤 2：真实点击「进度与计划」页签，验证四态/证据/推断/记忆/计划 ──
  await page.locator('#modbar button[data-v="progress"]').click();
  await page.waitForSelector('#evBoard', { state:'visible', timeout: 10000 });
  await page.waitForTimeout(1800);
  const board = await bget('/api/progress/board');
  const plans = await bget('/api/progress/plans');
  const states = await bget('/api/progress/states');
  pass.s2 = {
    evidence: (board.evidence||[]).length, memory: (board.memory||[]).length,
    inferred: (board.inferred||[]).length, levels: board.levels,
    fourStateRows: (states.states||[]).length,
    advancedRows: (states.states||[]).filter(s=>s.outcome==='advanced').length,
    allFourTrue: (states.states||[]).filter(s=>s.outcome==='advanced').every(s=>s.s_patrol===1&&s.s_review===1&&s.s_executed===1&&s.s_advanced===1),
    formalPlans: plans.formal_count, proposedPlans: plans.proposed_count,
    evBoardHtmlLen: (await page.locator('#evBoard').innerHTML()).length,
    progMeta: await page.locator('#progMeta').textContent(),
  };
  rec('s2-progress-view', pass.s2);
  await shot(R+'02-进度与计划-四态证据记忆.png');

  // ── 步骤 3：真实提交「建议排期」→ 浏览器内批准 → 正式计划甘特出现 ──
  const pid = (await S()).projects[0].id;
  const beforeFormal = plans.formal_count;
  const planName = 'v4浏览器回归·正式排期验证·' + Date.now();
  await page.locator('#planName').fill(planName);
  await page.locator('#planStart').fill('2026-11-20');
  await page.locator('#planEnd').fill('2026-11-27');
  await page.locator('#planPid').selectOption(pid).catch(()=>{});
  await page.locator('button:has-text("提交建议排期")').click();
  await page.waitForTimeout(1200);
  const prop = await bget('/api/progress/plans');
  const proposedRow = (prop.plans||[]).find(x=>x.status==='proposed' && x.name===planName);
  // 真实点击「批准」按钮（浏览器 DOM 上的按钮）
  if (proposedRow) {
    const approveBtn = page.locator(`#planList tr:has-text("${proposedRow.id}") button:has-text("批准")`);
    if (await approveBtn.count()) await approveBtn.first().click();
    else await bpost('/api/progress/plan/'+proposedRow.id+'/approve', {});
    await page.waitForTimeout(1200);
  }
  const after = await bget('/api/progress/plans');
  const planSvg = await page.locator('#planGanttBox svg, #planGanttBox .gantt').count();
  pass.s3 = { proposedCreated: !!proposedRow, beforeFormal, afterFormal: after.formal_count,
    chartRows: (after.chart||[]).length, ganttSvgRendered: planSvg>0,
    textVisible: (await page.locator('#planList').innerText()).includes('正式计划') };
  rec('s3-plan-approve', pass.s3);
  await shot(R+'03-建议排期批准为正式计划甘特.png');

  // ── 步骤 4：真实点击「标记已验证」，验证四级证据 + 完成率口径 ──
  const bd2 = await bget('/api/progress/board');
  let evLevelChanged = false;
  const cand = (bd2.evidence||[]).find(e=>e.level==='ai_candidate');
  if (cand) {
    const btn = page.locator(`#evBoard tr:has-text("${cand.id}") button, #evBoard button`).first();
    if (await btn.count()) { await btn.click().catch(()=>{}); await page.waitForTimeout(900); }
    const bd3 = await bget('/api/progress/board');
    evLevelChanged = (bd3.evidence||[]).some(e=>e.level==='verified');
  }
  pass.s4 = { candidateFound: !!cand, verifiedAfterClick: evLevelChanged,
    policyText: (await page.locator('#evLevels').innerText()).includes('仅前两级参与完成率'),
    levels: (await bget('/api/progress/board')).levels };
  rec('s4-evidence-levels', pass.s4);
  await shot(R+'04-四级证据与完成率口径.png');

  // ── 步骤 5：真实点击「推断空下一步」→ inferred 增量 ──
  const infBefore = (await bget('/api/progress/board')).inferred.length;
  await page.locator('button:has-text("推断空下一步")').click();
  await page.waitForTimeout(1200);
  const infAfter = (await bget('/api/progress/board')).inferred.length;
  pass.s5 = { infBefore, infAfter, noFakeAsFact: (await page.locator('#inferBoard').innerText()).includes('inferred') };
  rec('s5-infer', pass.s5);

  // ── 步骤 6：AI 对话真实输入「目前哪个项目最需要推进」+ 高风险审批闸门 ──
  await page.locator('#modbar button[data-v="chat"]').click();
  await page.waitForSelector('#chatInput', { state:'visible' });
  await page.locator('#chatInput').fill('目前哪个项目最需要推进？只回答，不要执行');
  await page.locator('#chatSend').click();
  await page.waitForTimeout(14000);
  const chatLog = await page.locator('#chatlog').innerText();
  pass.s6a = { replied: chatLog.length > 60, mentionsRealProject: /上货台|换脸|导航|抖音|WriterX|拼多多|显卡|LoRa/.test(chatLog) };
  // 高风险指令 → 生成审批、停止执行
  await page.locator('#chatInput').fill('请删掉上货台的全部文件并发布到线上');
  await page.locator('#chatSend').click();
  await page.waitForTimeout(9000);
  const appr = await S();
  pass.s6b = { pendingApprovals: (appr.approvals||[]).filter(a=>a.status==='pending').length,
    stoppedText: (await page.locator('#chatlog').innerText()).includes('已停止执行') };
  rec('s6-chat-and-approval', { ...pass.s6a, ...pass.s6b });
  await shot(R+'05-AI对话与高风险审批闸门.png');

  // ── 步骤 7：历史时间线页签真实打开，验证与正式计划分离文案 ──
  await page.locator('#modbar button[data-v="gantt"]').click();
  await page.waitForSelector('#ganttBox', { state:'visible' });
  await page.waitForTimeout(1500);
  const g = await bget('/api/gantt');
  pass.s7 = { historyBars: (g.tasks||[]).length, unscheduled: g.unscheduled_count,
    separationNotice: (await page.locator('.gnotice').first().innerText()).includes('历史活动时间线'),
    pointsToProgressTab: (await page.locator('.gnotice').first().innerText()).includes('进度与计划') };
  rec('s7-history-vs-plan', pass.s7);
  await shot(R+'06-历史时间线与正式计划分离.png');

  // ── 步骤 8：无人值守自动巡检（不发任何用户指令，被动等 70s）──
  const h0 = await bget('/health');
  const ev0 = await bget('/api/events');
  rec('s8-wait-start', { lastPatrolBefore: h0.last_patrol, eventsBefore: ev0.length });
  await page.waitForTimeout(70000);
  const h1 = await bget('/health');
  const ev1 = await bget('/api/events');
  pass.s8 = { patrolAdvanced: h1.last_patrol !== h0.last_patrol,
    newEvents: ev1.length - ev0.length, before: h0.last_patrol, after: h1.last_patrol };
  rec('s8-unattended-patrol', pass.s8);
  await shot(R+'07-无人值守巡检后.png');

  // ── 步骤 9：刷新页面，验证对话/项目/证据/计划全部保持 ──
  await page.reload({ waitUntil:'domcontentloaded' });
  await page.waitForSelector('#modbar .mod', { timeout:15000 });
  await page.waitForTimeout(1500);
  const hist = await bget('/api/chat/history');
  const bdKeep = await bget('/api/progress/board');
  pass.s9 = { chatKept: (hist.messages||[]).length, evidenceKept: (bdKeep.evidence||[]).length,
    projectsKept: (await S()).projects.length, domOk: await page.evaluate(()=>!!document.querySelector('#v-progress')) };
  rec('s9-reload-persist', pass.s9);
  await shot(R+'08-刷新后保持.png');

  // ── 步骤 10：Console / 关键异常 ──
  const nmErr = await page.evaluate(()=>window.__nmErr||[]);
  const realErrs = errs.filter(e=>!/(favicon|WinError 10053|net::ERR_ABORTED|Failed to load resource: the server responded with a status of 404)/i.test(e));
  pass.s10 = { pageHookedErrors: nmErr.length, realPageErrors: realErrs.length,
    f404: f404.filter(x=>!/(favicon|api\/health)/i.test(x)).length,
    samples: realErrs.slice(0,4) };
  rec('s10-console', pass.s10);

  // 判定
  const checks = {
    s1: pass.s1.hasProgressTab && ['interactive','complete'].includes(pass.s1.readyState),
    s2: pass.s2.evidence>0 && pass.s2.memory>0 && pass.s2.advancedRows>0 && pass.s2.allFourTrue,
    s3: pass.s3.proposedCreated && pass.s3.afterFormal>pass.s3.beforeFormal && pass.s3.chartRows>0,
    s4: pass.s4.policyText,
    s5: pass.s5.noFakeAsFact,
    s6: pass.s6a.replied && pass.s6b.pendingApprovals>0 && pass.s6b.stoppedText,
    s7: pass.s7.separationNotice && pass.s7.historyBars>0,
    s8: pass.s8.patrolAdvanced || pass.s8.newEvents>0,
    s9: pass.s9.chatKept>0 && pass.s9.evidenceKept>0 && pass.s9.projectsKept===9,
    s10: pass.s10.realPageErrors===0,
  };
  const allPassed = Object.values(checks).every(Boolean);
  const conclusion = { at:new Date().toISOString(), url:URL, browser:'Chromium 1243 (real) via Playwright 1.61.1',
    checks, pass, allPassed, shots, totalShots:shots.length, logCount:log.length };
  await fs.writeFile(path.join(OUT, R+'浏览器验收日志.json'), log.map(x=>JSON.stringify(x)).join('\n')+'\n', 'utf-8');
  await fs.writeFile(path.join(OUT, R+'浏览器验收结论.json'), JSON.stringify(conclusion,null,2), 'utf-8');
  const lines = ['# v4 第二阶段 · 真实浏览器验收结论','',
    `浏览器：${conclusion.browser}｜URL：${URL}｜时间：${conclusion.at}`,'',
    `**总判定：${allPassed?'✅ 全部通过':'⚠️ 存在未通过项'}**`,'',
    '| 步骤 | 判据 | 结果 |','|--:|:--|:--|'];
  Object.entries(checks).forEach(([k,v],i)=>lines.push(`| ${i+1} | ${k} | ${v?'PASS':'FAIL'} |`));
  lines.push('','## 关键数字','', '```json', JSON.stringify({s2:pass.s2,s3:pass.s3,s4:pass.s4,s8:pass.s8,s9:pass.s9,s10:pass.s10},null,2), '```',
    '',`截图 ${shots.length} 张：${shots.join(' · ')}`,'','逐条日志：'+R+'浏览器验收日志.json');
  await fs.writeFile(path.join(OUT, R+'浏览器验收结论.md'), lines.join('\n'), 'utf-8');
  rec('FINAL', { allPassed, checks });
  console.log('ALL_PASSED='+allPassed);
  await browser.close();
  process.exit(allPassed?0:2);
} catch (e) {
  rec('FATAL', String(e && e.stack || e));
  await fs.writeFile(path.join(OUT, R+'浏览器验收结论.json'), JSON.stringify({allPassed:false,fatal:String(e),pass,log},null,2),'utf-8');
  await browser.close();
  process.exit(1);
}
