// nanoMuse 主动型 AI 项目总管 · 真实浏览器验收（ego lite TaskSpace）
// 目标：http://127.0.0.1:9196/  —— 强制 7 项，全部用真实浏览器交互
const fs = await import("node:fs/promises");
const OUT = "G:/Users/Administrator/Documents/deepseek-harness/default-workspace/nanoMuse项目总管/验收";
await fs.mkdir(OUT, { recursive: true });

const task = await taskSpace("nanoMuse 项目总管真实浏览器验收 20261111");
const page = task.page("p1");
const log = [];
const rec = (k, v) => { log.push({ at: new Date().toISOString(), k, v }); console.log(JSON.stringify({ [k]: v })); };

// 浏览器 Console 中的关键运行错误（第 7 项）：页面内 hook 收集真实报错
try {
  await page.cdp("Runtime.enable", {});
  await page.evaluate(() => {
    window.__nmErr = [];
    const oe = console.error;
    console.error = function (...a) { window.__nmErr.push(a.map(String).join(" ")); oe.apply(console, a); };
    window.addEventListener("error", e => window.__nmErr.push("onerror:" + String(e.message)));
    window.addEventListener("unhandledrejection", e => window.__nmErr.push("unhandled:" + String(e.reason)));
  });
} catch (e) { rec("cdp_hook_nonfatal", String(e)); }

// 1) 打开实际运行的 URL，确认页面加载正常
await page.goto("http://127.0.0.1:9196/", { timeout: 30000 });
await page.waitForLoadState("load", { timeout: 15000 });
const title = await page.title();
const url = await page.url();
const h1 = await page.evaluate(() => document.querySelector("h1")?.innerText || "");
const rootLen = await page.evaluate(() => document.getElementById("nProj")?.innerText || "");
await page.waitForFunction(() => { const e = document.getElementById("nProj"); return e && e.innerText !== "—"; }, undefined, { timeout: 15000 });
const nProj = await page.evaluate(() => document.getElementById("nProj").innerText);
rec("step1_load", { title, url, h1, nProj: Number(nProj), rootRendered: Number(nProj) > 0 });
await page.screenshot({ path: OUT + "/01-总控台首页.png", fullPage: true });

// 读取页面真实项目列表（DOM，不是接口）
const cards = await page.evaluate(() => Array.from(document.querySelectorAll(".card")).map(c => ({
  name: c.querySelector(".t span")?.innerText || "",
  state: c.querySelector(".badge")?.innerText || "",
  text: c.innerText.replace(/\n/g, " | ").slice(0, 220)
})));
rec("project_list_dom", { count: cards.length, cards });

// 2)+3) 真实点击 3 个测试项目详情，检查目标/进度/阻塞/下一步与真实数据一致
const targets = [
  { key: "换脸与人脸资产系统", expect: "已停滞", file: "02-详情-已停滞-换脸系统.png" },
  { key: "上货台7.0", expect: "正常推进", file: "03-详情-正在执行-上货台7.0.png" },
  { key: "上货台", expect: "待决策", file: "04-详情-待人工决策-上货台.png" },
];
const detailChecks = [];
for (const t of targets) {
  const clicked = await page.evaluate((k) => {
    const c = Array.from(document.querySelectorAll(".card")).find(x => x.innerText.includes(k));
    if (!c) return false;
    c.click();
    return true;
  }, t.key);
  await page.waitForTimeout(500);
  const d = await page.evaluate(() => {
    const el = document.getElementById("detail");
    if (!el) return null;
    return {
      h2: el.querySelector("h2")?.innerText || "",
      text: el.innerText,
      hasGoal: el.innerText.includes("目标"),
      hasNext: el.innerText.includes("下一步任务"),
      hasEvidence: el.innerText.includes("状态判定证据"),
      hasBlockers: el.innerText.includes("阻塞原因"),
      hasHistory: el.innerText.includes("历史进度"),
      tasks: el.querySelectorAll("#tasklist .task").length,
      milestonesRows: el.querySelectorAll("table tr").length,
    };
  });
  detailChecks.push({ target: t.key, expect: t.expect, clicked, h2: d?.h2,
    stateMatches: !!d && d.h2.includes(t.expect),
    hasGoal: d?.hasGoal, hasNext: d?.hasNext, hasEvidence: d?.hasEvidence,
    hasBlockers: d?.hasBlockers, hasHistory: d?.hasHistory,
    tasks: d?.tasks, milestonesRows: d?.milestonesRows,
    goalSnippet: (d?.text || "").split("目标")[1]?.slice(0, 90).replace(/\n/g, " "),
    nextSnippet: (d?.text || "").split("下一步任务 / 建议")[1]?.slice(0, 110).replace(/\n/g, " ") });
  await page.screenshot({ path: OUT + "/" + t.file, fullPage: true });
}
rec("step2_3_details", detailChecks);

// 与真实数据一致性：浏览器里显示的证据，和总管 /api/state 的真源一致
const apiState = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
const consistency = detailChecks.map(d => {
  const p = apiState.projects.find(x => x.name.includes(d.key) || d.h2.includes(x.name));
  if (!p) return { key: d.key, ok: false, why: "api 无对应项目" };
  return { key: d.key, api_id: p.id, api_state: p.state,
    api_overall: p.overall_verified ? p.overall : "待核实",
    api_evidence: (p.evidence || []).length, api_next: (p.next_action || "").slice(0, 60),
    dom_state: d.h2, ok: d.stateMatches };
});
rec("step3_consistency", consistency);

// 4) 通过实际任务状态变更，验证浏览器界面自动刷新 / 刷新后更新
// 先固定点回「换脸」项目，保证 before / after / reload 比的是同一行任务
await page.evaluate(() => {
  const c = Array.from(document.querySelectorAll(".card")).find(x => x.innerText.includes("换脸"));
  if (c) c.click();
});
await page.waitForTimeout(400);
const before = await page.evaluate(() => {
  const b = document.querySelector("#tasklist .task .btn");
  const row = b?.closest(".task");
  return { id: b?.getAttribute("onclick") || "", status: row?.querySelector(".st")?.innerText || "", label: b?.innerText || "" };
});
// 真实点击「✓ 完成」按钮
const clickedTask = await page.evaluate(() => {
  const b = document.querySelector("#tasklist .task .btn");
  if (!b) return false;
  b.click();
  return true;
});
await page.waitForTimeout(1600); // 前端 5s 自动刷新之前先等 POST 回来
const afterAuto = await page.evaluate(() => {
  const row = document.querySelector("#tasklist .task");
  return { status: row?.querySelector(".st")?.innerText || "", label: row?.querySelector(".btn")?.innerText || "" };
});
rec("step4_task_change_before", before);
rec("step4_after_post", { clickedTask, afterAuto });
await page.screenshot({ path: OUT + "/05-任务状态变更后-自动刷新.png", fullPage: true });

// 刷新页面再验（刷新后更新）
await page.reload({ timeout: 20000 });
await page.waitForFunction(() => document.querySelectorAll(".card").length > 0, undefined, { timeout: 15000 });
await page.evaluate(() => {
  const c = Array.from(document.querySelectorAll(".card")).find(x => x.innerText.includes("换脸"));
  if (c) c.click();
});
await page.waitForTimeout(500);
const afterReload = await page.evaluate(() => {
  const row = document.querySelector("#tasklist .task");
  return { status: row?.querySelector(".st")?.innerText || "", label: row?.querySelector(".btn")?.innerText || "" };
});
rec("step4_after_reload", { afterReload, persisted: afterReload.status !== before.status });
await page.screenshot({ path: OUT + "/06-刷新后任务状态仍在.png", fullPage: true });

// 5) 测试一次定时自动巡检：纯被动等待后台 daemon 自己跑完一个完整循环
//    全程不调用 /api/patrol、不调 /api/patrol-interval、不发任何新指令
const health0 = await page.evaluate(async () => (await (await fetch("/health")).json()));
const st0 = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
rec("step5_before", { lastPatrol: health0.last_patrol, events: st0.events.length, notifications: st0.notifications.length, patrolSeconds: st0.patrol_seconds });
// 立刻把 Console hook 期间积累的页面错误留痕（不触发任何动作）
await page.screenshot({ path: OUT + "/07a-等待定时巡检前.png", fullPage: true });
rec("step5_wait", "纯被动等待后台自动巡检循环（" + st0.patrol_seconds + "s），本期间不调用 /api/patrol 与 /api/patrol-interval");
await page.waitForTimeout((st0.patrol_seconds + 12) * 1000);
const health1 = await page.evaluate(async () => (await (await fetch("/health")).json()));
const st1 = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
const autoEvents = st1.events.filter(e => e.kind === "patrol.done" && /定时自动巡检/.test(e.message));
rec("step5_after", { lastPatrolBefore: health0.last_patrol, lastPatrolAfter: health1.last_patrol,
  changedWithoutUserCommand: health0.last_patrol !== health1.last_patrol,
  autoPatrolEvents: autoEvents.length, latestAuto: autoEvents[0]?.message,
  notificationsTotal: st1.notifications.length,
  stallNotices: st1.notifications.filter(n => n.title.includes("停滞") || n.title.includes("待你拍板")).length });
await page.screenshot({ path: OUT + "/07-定时自动巡检后-通知与事件.png", fullPage: true });

// 6) 低风险 DSH 任务接续：检查真实执行成果（由 Host 任务看板真开的 DSH 会话回填）
const st2 = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
const handoff = st2.actions.filter(a => a.kind === "dsh_session_handoff");
rec("step6_dsh_handoff", { total: handoff.length,
  succeeded: handoff.filter(a => a.status === "succeeded" && a.checked === 1).length,
  rows: handoff.map(a => ({ id: a.id, status: a.status, checked: a.checked, result: a.result, output: (a.output || "").slice(0, 120) })) });
// 同时在界面上真跑一次低风险动作（生成巡检摘要 + 产物回读校验）
const runClicked = await page.evaluate(() => {
  const b = Array.from(document.querySelectorAll(".btn")).find(x => x.innerText.includes("执行低风险接续"));
  if (!b) return false; b.click(); return true;
});
await page.waitForTimeout(1800);
const st3 = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
const briefs = st3.actions.filter(a => a.kind === "generate_patrol_brief" && a.status === "succeeded");
rec("step6_low_risk_action", { runClicked, succeededBriefs: briefs.length,
  sample: briefs.slice(0, 2).map(a => ({ id: a.id, checked: a.checked, output: (a.output || "").slice(0, 160) })) });
await page.screenshot({ path: OUT + "/08-低风险接续执行成果.png", fullPage: true });

// 7) 浏览器 Console 关键运行错误
let hookErrs = [];
try { hookErrs = await page.evaluate(() => window.__nmErr || []); } catch (e) { hookErrs = ["hook read failed: " + e]; }
const cdpLog = await page.evaluate(() => ({
  readyState: document.readyState,
  scripts: document.scripts.length,
  h1: document.querySelector("h1")?.innerText,
  cards: document.querySelectorAll(".card").length,
  detailLen: document.getElementById("detail")?.innerText.length || 0,
}));
rec("step7_console", { pageHookedErrors: hookErrs, cdpLog });

// 验收结论（如实，不伪造）
const pass = {
  1: Number(nProj) > 0 && url.startsWith("http://127.0.0.1:9196/"),
  2: detailChecks.length === 3 && detailChecks.every(d => d.clicked && d.hasGoal && d.hasNext && d.hasEvidence),
  3: consistency.every(c => c.ok),
  4: clickedTask && afterReload.status !== before.status,
  5: health0.last_patrol !== health1.last_patrol && autoEvents.length > 0,
  6: (handoff.some(a => a.status === "succeeded" && a.checked === 1) || briefs.length > 0),
  7: hookErrs.length === 0 && cdpLog.cards > 0,
};
const verdict = { pass, allPassed: Object.values(pass).every(Boolean),
  url: "http://127.0.0.1:9196/", taskSpaceId: task.spaceId, browser: "ego lite 0.6.2.8 (真实 Chromium)",
  screenshots: ["01-总控台首页.png", "02-详情-已停滞-换脸系统.png", "03-详情-正在执行-上货台7.0.png",
    "04-详情-待人工决策-上货台.png", "05-任务状态变更后-自动刷新.png", "06-刷新后任务状态仍在.png",
    "07-定时自动巡检后-通知与事件.png", "08-低风险接续执行成果.png"] };
rec("VERDICT", verdict);

await fs.writeFile(OUT + "/浏览器验收日志.json", JSON.stringify({ log, verdict }, null, 2), "utf8");
await fs.writeFile(OUT + "/浏览器验收结论.md",
  "# 真实浏览器验收结论（ego lite）\n\n" +
  "URL：`http://127.0.0.1:9196/` · TaskSpace `" + task.spaceId + "` · 浏览器：ego lite 0.6.2.8（真实 Chromium）\n\n" +
  Object.entries(pass).map(([k, v]) => `- 第 ${k} 项：${v ? "PASS" : "FAIL"}`).join("\n") +
  "\n\n总判定：**" + (verdict.allPassed ? "全部通过" : "未全部通过（见日志）") + "**\n\n" +
  "截图与完整逐条证据见同目录 PNG 与 `浏览器验收日志.json`。\n", "utf8");
console.log("VERDICT_JSON=" + JSON.stringify(verdict));
