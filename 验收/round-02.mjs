// round-02 · 真实浏览器强制验收（ego lite 真实 Chromium），修复后完整重跑
// 与 round-01 的差异：修正 consistency 名称匹配（精确按 DOM h2 找项目），其余 7 项原样重跑
const fs = await import("node:fs/promises");
const OUT = "G:/Users/Administrator/Documents/deepseek-harness/default-workspace/nanoMuse项目总管/验收";
await fs.mkdir(OUT, { recursive: true });
const task = await taskSpace(6);                    // 续用 round-01 的同一个 TaskSpace
const page = task.page("p1");
const log = [];
const rec = (k, v) => { log.push({ at: new Date().toISOString(), k, v }); console.log(JSON.stringify({ [k]: v })); };

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

// 1) 打开实际 URL，确认加载
await page.goto("http://127.0.0.1:9196/", { timeout: 30000 });
await page.waitForLoadState("load", { timeout: 15000 });
const title = await page.title(), url = await page.url();
const h1 = await page.evaluate(() => document.querySelector("h1")?.innerText || "");
await page.waitForFunction(() => { const e = document.getElementById("nProj"); return e && e.innerText !== "—"; }, undefined, { timeout: 15000 });
const nProj = Number(await page.evaluate(() => document.getElementById("nProj").innerText));
rec("step1_load", { title, url, h1, nProj, rootRendered: nProj > 0 });
await page.screenshot({ path: OUT + "/01-总控台首页.png", fullPage: true });
const cards = await page.evaluate(() => Array.from(document.querySelectorAll(".card")).map(c => ({
  name: c.querySelector(".t span")?.innerText || "", state: c.querySelector(".badge")?.innerText || "" })));
rec("project_list_dom", { count: cards.length, cards });

// 2)+3) 真实点击 3 个测试项目，核目标/进度/阻塞/下一步/证据
const targets = [
  { key: "换脸与人脸资产系统", name: "换脸与人脸资产系统", expect: "已停滞", file: "02-详情-已停滞-换脸系统.png" },
  { key: "上货台7.0", name: "上货台7.0 AI运营中心", expect: "正常推进", file: "03-详情-正在执行-上货台7.0.png" },
  { key: "上货台", name: "上货台", expect: "待决策", file: "04-详情-待人工决策-上货台.png" },
];
const detailChecks = [];
for (const t of targets) {
  const clicked = await page.evaluate((k) => {
    const c = Array.from(document.querySelectorAll(".card")).find(x => (x.querySelector(".t span")?.innerText || "").includes(k));
    if (!c) return false; c.click(); return true;
  }, t.key === "上货台" ? "上货台" : t.key);
  // 「上货台」要精确避开「上货台7.0」：按完整项目名反查
  if (t.key === "上货台") {
    await page.evaluate(() => {
      const c = Array.from(document.querySelectorAll(".card")).find(x => (x.querySelector(".t span")?.innerText || "").trim() === "上货台");
      if (c) c.click();
    });
  }
  await page.waitForTimeout(500);
  const d = await page.evaluate(() => {
    const el = document.getElementById("detail");
    if (!el) return null;
    return { h2: el.querySelector("h2")?.innerText || "", text: el.innerText,
      hasGoal: el.innerText.includes("目标"), hasNext: el.innerText.includes("下一步任务"),
      hasEvidence: el.innerText.includes("状态判定证据"), hasBlockers: el.innerText.includes("阻塞原因"),
      hasHistory: el.innerText.includes("历史进度"), tasks: el.querySelectorAll("#tasklist .task").length };
  });
  detailChecks.push({ target: t.key, expect: t.expect, clicked, h2: d?.h2,
    stateMatches: !!d && d.h2.includes(t.expect), hasGoal: d?.hasGoal, hasNext: d?.hasNext,
    hasEvidence: d?.hasEvidence, hasBlockers: d?.hasBlockers, hasHistory: d?.hasHistory, tasks: d?.tasks,
    goalSnippet: (d?.text || "").split("目标")[1]?.slice(0, 70).replace(/\n/g, " "),
    nextSnippet: (d?.text || "").split("下一步任务 / 建议")[1]?.slice(0, 90).replace(/\n/g, " ") });
  await page.screenshot({ path: OUT + "/" + t.file, fullPage: true });
}
rec("step2_3_details", detailChecks);

// 一致性：按 DOM h2 里的完整项目名精确匹配 api，杜绝「上货台」误配「上货台7.0」
const apiState = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
const LBL = { active: "正常推进", stalled: "已停滞", awaiting_decision: "待决策", pending_acceptance: "待验收" };
const consistency = detailChecks.map(d => {
  const p = apiState.projects.find(x => d.h2.includes(x.name));
  if (!p) return { key: d.key, ok: false, why: "api 无对应项目" };
  return { key: d.key, api_id: p.id, api_name: p.name, api_state: p.state, api_state_label: LBL[p.state],
    api_overall: p.overall_verified ? p.overall : "待核实", api_evidence: (p.evidence || []).length,
    api_next: (p.next_action || "").slice(0, 60), dom_state: d.h2,
    ok: d.stateMatches && d.h2.includes(LBL[p.state]) };
});
rec("step3_consistency", consistency);

// 4) 实际任务状态变更 → 自动刷新 → 刷新后仍在
await page.evaluate(() => {
  const c = Array.from(document.querySelectorAll(".card")).find(x => (x.querySelector(".t span")?.innerText || "").includes("换脸"));
  if (c) c.click();
});
await page.waitForTimeout(400);
const before = await page.evaluate(() => {
  const b = document.querySelector("#tasklist .task .btn"), row = b?.closest(".task");
  return { onclick: b?.getAttribute("onclick") || "", status: row?.querySelector(".st")?.innerText || "", label: b?.innerText || "" };
});
const clickedTask = await page.evaluate(() => { const b = document.querySelector("#tasklist .task .btn"); if (!b) return false; b.click(); return true; });
await page.waitForFunction((prev) => {
  const row = document.querySelector("#tasklist .task");
  return row && (row.querySelector(".st")?.innerText || "") !== prev;
}, before.status, { timeout: 12000 }).catch(e => rec("step4_wait_nonfatal", String(e)));
const afterAuto = await page.evaluate(() => {
  const row = document.querySelector("#tasklist .task");
  return { status: row?.querySelector(".st")?.innerText || "", label: row?.querySelector(".btn")?.innerText || "" };
});
rec("step4_before", before); rec("step4_after_post", { clickedTask, afterAuto, autoRefreshed: afterAuto.status !== before.status });
await page.screenshot({ path: OUT + "/05-任务状态变更后-自动刷新.png", fullPage: true });
await page.reload({ timeout: 20000 });
await page.waitForFunction(() => document.querySelectorAll(".card").length > 0, undefined, { timeout: 15000 });
await page.evaluate(() => {
  const c = Array.from(document.querySelectorAll(".card")).find(x => (x.querySelector(".t span")?.innerText || "").includes("换脸")); if (c) c.click();
});
await page.waitForTimeout(500);
const afterReload = await page.evaluate(() => {
  const row = document.querySelector("#tasklist .task");
  return { status: row?.querySelector(".st")?.innerText || "", label: row?.querySelector(".btn")?.innerText || "" };
});
rec("step4_after_reload", { afterReload, persisted: afterReload.status === afterAuto.status && afterAuto.status !== before.status });
await page.screenshot({ path: OUT + "/06-刷新后任务状态仍在.png", fullPage: true });

// 5) 定时自动巡检：纯被动等待一个完整 60s 循环，不发任何指令
const health0 = await page.evaluate(async () => (await (await fetch("/health")).json()));
const st0 = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
rec("step5_before", { lastPatrol: health0.last_patrol, patrolSeconds: st0.patrol_seconds, notifications: st0.notifications.length });
await page.screenshot({ path: OUT + "/07a-等待定时巡检前.png", fullPage: true });
rec("step5_wait", "纯被动等待后台 daemon 自动巡检（" + st0.patrol_seconds + "s）；不调用 /api/patrol 与 /api/patrol-interval");
await page.waitForTimeout((st0.patrol_seconds + 12) * 1000);
const health1 = await page.evaluate(async () => (await (await fetch("/health")).json()));
const st1 = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
const autoEvents = st1.events.filter(e => e.kind === "patrol.done" && /定时自动巡检/.test(e.message));
const newAuto = autoEvents.filter(e => e.at > health0.last_patrol);
rec("step5_after", { lastPatrolBefore: health0.last_patrol, lastPatrolAfter: health1.last_patrol,
  changedWithoutUserCommand: health0.last_patrol !== health1.last_patrol,
  autoPatrolEventsTotal: autoEvents.length, newAutoDuringWait: newAuto.length,
  latestAuto: autoEvents[0]?.message, notificationsTotal: st1.notifications.length,
  stallNotices: st1.notifications.filter(n => n.title.includes("停滞") || n.title.includes("待你拍板")).length });
await page.screenshot({ path: OUT + "/07-定时自动巡检后-通知与事件.png", fullPage: true });

// 6) 低风险 DSH 接续成果 + 界面真跑一次低风险动作
const st2 = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
const handoff = st2.actions.filter(a => a.kind === "dsh_session_handoff");
rec("step6_dsh_handoff", { total: handoff.length,
  succeeded: handoff.filter(a => a.status === "succeeded" && a.checked === 1).length,
  rows: handoff.filter(a => a.status === "succeeded").map(a => ({ id: a.id, checked: a.checked, output: (a.output || "").slice(0, 150) })) });
const runClicked = await page.evaluate(() => {
  const b = Array.from(document.querySelectorAll(".btn")).find(x => x.innerText.includes("执行低风险接续")); if (!b) return false; b.click(); return true;
});
await page.waitForTimeout(2000);
const st3 = await page.evaluate(async () => (await (await fetch("/api/state")).json()));
const briefs = st3.actions.filter(a => a.kind === "generate_patrol_brief" && a.status === "succeeded");
rec("step6_low_risk_action", { runClicked, succeededBriefs: briefs.length,
  checkedBriefs: briefs.filter(a => a.checked === 1).length,
  sample: briefs.slice(0, 2).map(a => ({ id: a.id, checked: a.checked, output: (a.output || "").slice(0, 150) })) });
await page.screenshot({ path: OUT + "/08-低风险接续执行成果.png", fullPage: true });

// 7) Console 关键错误
let hookErrs = [];
try { hookErrs = await page.evaluate(() => window.__nmErr || []); } catch (e) { hookErrs = ["hook read failed: " + e]; }
const cdpLog = await page.evaluate(() => ({ readyState: document.readyState, scripts: document.scripts.length,
  h1: document.querySelector("h1")?.innerText, cards: document.querySelectorAll(".card").length,
  detailLen: document.getElementById("detail")?.innerText.length || 0 }));
rec("step7_console", { pageHookedErrors: hookErrs, cdpLog });

const pass = {
  1: nProj > 0 && url.startsWith("http://127.0.0.1:9196/"),
  2: detailChecks.length === 3 && detailChecks.every(d => d.clicked && d.hasGoal && d.hasNext && d.hasEvidence && d.hasBlockers),
  3: consistency.every(c => c.ok),
  4: clickedTask && afterAuto.status !== before.status && afterReload.status === afterAuto.status,
  5: health0.last_patrol !== health1.last_patrol && newAuto.length > 0,
  6: handoff.some(a => a.status === "succeeded" && a.checked === 1) && briefs.some(a => a.checked === 1),
  7: hookErrs.length === 0 && cdpLog.cards > 0,
};
const verdict = { pass, allPassed: Object.values(pass).every(Boolean),
  url: "http://127.0.0.1:9196/", taskSpaceId: task.spaceId, browser: "ego lite 0.6.2.8 (真实 Chromium)",
  screenshots: ["01-总控台首页.png", "02-详情-已停滞-换脸系统.png", "03-详情-正在执行-上货台7.0.png",
    "04-详情-待人工决策-上货台.png", "05-任务状态变更后-自动刷新.png", "06-刷新后任务状态仍在.png",
    "07a-等待定时巡检前.png", "07-定时自动巡检后-通知与事件.png", "08-低风险接续执行成果.png"] };
rec("VERDICT", verdict);
await fs.writeFile(OUT + "/浏览器验收日志.json", JSON.stringify({ log, verdict }, null, 2), "utf8");
await fs.writeFile(OUT + "/浏览器验收结论.md",
  "# 真实浏览器验收结论（ego lite · round-02）\n\n" +
  "URL：`http://127.0.0.1:9196/` · TaskSpace `" + task.spaceId + "` · 浏览器：ego lite 0.6.2.8（真实 Chromium）\n\n" +
  Object.entries(pass).map(([k, v]) => `- 第 ${k} 项：${v ? "PASS" : "FAIL"}`).join("\n") +
  "\n\n总判定：**" + (verdict.allPassed ? "全部通过" : "未全部通过（见日志）") + "**\n\n" +
  "逐条证据见 `浏览器验收日志.json`，实际操作截图见同目录 PNG。\n", "utf8");
console.log("VERDICT_JSON=" + JSON.stringify(verdict));
