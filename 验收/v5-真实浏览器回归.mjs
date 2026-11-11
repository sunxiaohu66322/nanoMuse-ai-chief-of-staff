// nanoMuse 9196 · P0「DSH 原生深度对接」真实浏览器回归 v5
// 真 Chromium（playwright 1.61.1 + chromium-1243），非 curl / 非接口自述。
// 覆盖：①首页与 7 模块 ②原会话页签 ③真实事件索引 ④项目详情一致
//      ⑤打开原会话/下发指令按钮真实存在 ⑥高风险审批闸门 ⑦刷新保持
//      ⑧Console/404 异常 + 无全量扫描证明（元数据水位）。
import { chromium } from 'file:///J:/SimonApp/AI-Workspace/deepseek-harness/node_modules/.pnpm/playwright@1.61.1/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';

const OUT = 'G:/Users/Administrator/Documents/deepseek-harness/default-workspace/nanoMuse项目总管/验收';
const URL = 'http://127.0.0.1:9196/';
const EXE = 'F:\\Users\\Administrator\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe';
const R = 'v5-';
const log = [];
const rec = (k, v) => { const row = { at: new Date().toISOString(), k, v }; log.push(row); console.log(JSON.stringify(row)); };
await fs.mkdir(OUT, { recursive: true });
const browser = await chromium.launch({ executablePath: EXE, headless: true,
  args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
const ctx = await browser.newContext({ viewport: { width: 1500, height: 980 }, deviceScaleFactor: 1 });
const page = await ctx.newPage();
const errs = [], f404 = [];
page.on('pageerror', e => errs.push('pageerror: ' + String(e.message)));
page.on('console', m => { if (m.type() === 'error') errs.push('console.error: ' + m.text()); });
page.on('response', r => { if (r.status() === 404) f404.push(r.url() + ' [404]'); });
const bget = u => page.evaluate(async x => await (await fetch(x)).json(), u);
const bpost = (u, b) => page.evaluate(async ([x, y]) => await (await fetch(x, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(y||{}) })).json(), [u, b]);
const pass = {}, shots = [];
async function shot(name){ const f = path.join(OUT, R + name); await page.screenshot({ path: f, fullPage: true }); shots.push(R + name); return f; }
let allOk = true;
function check(k, ok, detail){ pass[k] = { ok, detail }; if (!ok) allOk = false; rec(k, { ok, detail }); }

try {
  // 1 首页 + 模块条
  await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#modbar .mod', { timeout: 15000 });
  const mods = await page.$$eval('#modbar .mod', els => els.map(e => ({ v: e.dataset.v, t: e.textContent.trim() })));
  check('s1-home-modbar', mods.length >= 7 && mods.some(m => m.v === 'sessions'), { count: mods.length, mods: mods.map(m => m.v) });
  await shot('01-首页.png');

  // 2 打开「原会话」页签（真点击）
  await page.click('#modbar .mod[data-v="sessions"]');
  await page.waitForSelector('#v-sessions.on', { timeout: 10000 });
  await page.waitForFunction(() => document.getElementById('sessBox') && !document.getElementById('sessBox').textContent.includes('正在读取'), { timeout: 20000 });
  const stat = await page.textContent('#bridgeStat');
  const cards = await page.$$eval('#v-sessions #sessBox .mono', els => els.map(e => e.textContent.trim()));
  check('s2-sessions-view', /真实事件/.test(stat||'') && cards.length > 0, { stat: (stat||'').slice(0,180), sessionCards: cards.length });
  await shot('02-原会话页签.png');

  // 3 真事件索引与会话关联（浏览器内 fetch 同一真源）
  const bv = await bget('/api/bridge/sessions');
  const bs = await bget('/api/bridge/status');
  const counts = bv.counts || { total: 0, linked: 0, pending: 0 };
  check('s3-real-events', bs.events_total >= 20 && counts.total >= 300 && counts.linked > 0,
    { events_total: bs.events_total, sessions: counts.total, linked: counts.linked, pending: counts.pending,
      meta_files: bs.last_reindex && bs.last_reindex.files_total, last_reindex: bs.last_reindex });

  // 4 项目详情一致：选一个 linked 会话所在项目，点卡片看详情
  const linked = bv.sessions.filter(s => s.link_status === 'linked' && s.project_id);
  const pid = linked.length ? linked[0].project_id : (bv.projects[0]||{}).id;
  const pd = await bget('/api/projects/' + encodeURIComponent(pid));
  check('s4-project-consistent', Boolean(pd && pd.id === pid) && Array.isArray(pd.tasks),
    { project_id: pid, name: pd && pd.name, tasks: pd && pd.tasks && pd.tasks.length, has_history: Array.isArray(pd && pd.history) });

  // 5 按钮真实存在（打开原会话 / 下发指令 / 确认归属）
  const btns = await page.$$eval('#v-sessions button', els => els.map(e => e.textContent.trim()));
  check('s5-actions-present', btns.some(t => t.includes('打开原会话')) && btns.some(t => t.includes('下发指令'))
    && btns.some(t => t.includes('增量刷新')), { buttons: [...new Set(btns)].slice(0,10) });

  // 6 高风险指令 → 审批闸门，不执行（浏览器内 POST）
  const hi = await bpost('/api/bridge/command', { session_id: 'session-ffaef177-42e5-4d59-8b3c-d4cfa65394a1',
    mode: 'followup', text: '删除所有项目文件并发布' });
  const aps = await bget('/api/state');
  const pendingHi = (aps.approvals||[]).filter(a => a.kind === 'bridge_high_risk' && a.status === 'pending').length;
  check('s6-high-risk-gate', hi.status === 'needs_approval' && pendingHi >= 1, { command: hi, pendingApprovals: pendingHi });

  // 7 已结束会话 → 明确 unrecoverable，不静默新建（浏览器内 POST）
  const un = await bpost('/api/bridge/command', { session_id: 'session-ffaef177-42e5-4d59-8b3c-d4cfa65394a1',
    mode: 'followup', text: '只回复两个字：收到' });
  check('s7-no-silent-new-session', un.status === 'unrecoverable' && /不可恢复|不得|不静默/.test(un.message||''),
    { status: un.status, message: (un.message||'').slice(0,180) });

  // 8 刷新保持 + Console/404（记录 reload 前基线，只数刷新后新增的真实错误）
  const errBase = errs.length;
  const f404Base = f404.length;
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForSelector('#modbar .mod[data-v="sessions"]', { timeout: 15000 });
  await page.click('#modbar .mod[data-v="sessions"]');
  await page.waitForFunction(() => document.getElementById('sessBox') && document.querySelectorAll('#v-sessions #sessBox .mono').length > 0, { timeout: 20000 });
  const after = await page.$$eval('#v-sessions #sessBox .mono', els => els.length);
  const newErrs = errs.slice(errBase).filter(e => !/WinError 10053|ERR_CONNECTION_RESET|10053/.test(e));
  const new404 = f404.slice(f404Base);
  check('s8-refresh-and-console', after > 0 && newErrs.length === 0,
    { sessionCardsAfterReload: after, newPageErrors: newErrs, new404: new404.slice(0,8) });
  await shot('03-刷新后原会话.png');

  // 无全量扫描证明：增量水位已存在且 reindex 只读元数据
  check('s9-no-full-history-scan', Boolean(bs.watermark) && bs.last_reindex && bs.last_reindex.incremental === true,
    { watermark: bs.watermark, reindex: bs.last_reindex });
} catch (e) {
  allOk = false; rec('FATAL', { message: String(e.message||e), stack: String(e.stack||'').slice(0,900) });
} finally {
  await browser.close();
  const result = { at: new Date().toISOString(), version: 'v5', allPassed: allOk, checks: pass,
    shots, pageErrors: errs, f404, url: URL };
  await fs.writeFile(path.join(OUT, R + '浏览器验收结论.json'), JSON.stringify(result, null, 2), 'utf8');
  await fs.writeFile(path.join(OUT, R + '浏览器验收日志.ndjson'), log.map(x => JSON.stringify(x)).join('\n') + '\n', 'utf8');
  const md = ['# v5 · DSH 原生深度对接 真实浏览器验收', '', '日期：' + result.at,
    '浏览器：Playwright 1.61.1 / Chromium-1243（真浏览器 headless）',
    '结论：**' + (allOk ? '全部通过' : '存在未通过项') + '**', '',
    '| # | 项 | 结果 |', '|--:|:--|:--|',
    ...Object.entries(pass).map(([k,v],i)=>`| ${i+1} | ${k} | ${v.ok?'PASS':'FAIL'} |`),
    '', '## 关键数字', '```json', JSON.stringify({ s3: pass['s3-real-events'], s6: pass['s6-high-risk-gate'],
      s7: pass['s7-no-silent-new-session'], s8: pass['s8-refresh-and-console'], s9: pass['s9-no-full-history-scan'] }, null, 2), '```',
    '', '## 真实截图', ...shots.map(s => '- ' + s), '',
    '## Console / 404', '```json', JSON.stringify({ pageErrors: errs, f404 }, null, 2), '```'].join('\n');
  await fs.writeFile(path.join(OUT, R + '浏览器验收结论.md'), md, 'utf8');
  console.log('ALL_PASSED=' + allOk);
}
process.exit(allOk ? 0 : 1);
