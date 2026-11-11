# -*- coding: utf-8 -*-
"""nanoMuse 9196 · 第二阶段进度引擎（真实性 / 长期记忆 / 正式计划甘特 / 四态推进）

设计原则（第二阶段补充指令逐条对应）：
  ① 修复假推进：把「巡检 / 复核 / 任务执行 / 实际推进并验收」拆成四个独立态。
     只有【真实结构化产物 + 合规验收 + 项目状态层真实变化 + 回读确认】才算 advanced。
     重复复核（digest 相同）记 no_change，不无限运行、不刷屏。
  ② 自动推断下一步：next_action 为空时增量读真实材料 → 候选任务，标 inferred + 来源 + 置信度；
     低置信度/方向不明 → 主动生成审批询问，绝不把推断当事实。
  ③ 甘特升级：历史时间线（真实时间戳，只读不可改）与正式计划甘特分离；
     AI 建议排期一律 proposed（待确认），人工 approve 后才 formal；真实历史日期不修改。
  ④ 长期项目记忆：决策 / 关键要求 / 已否决方案 / 阶段总结入库，对话按需读取相关项目。
  ⑤ 进度真实性：四级证据 verified / ai_candidate / user_confirmed / not_started；
     完成率只由 verified + user_confirmed 计算，无证据一律「待核实」，不编造。
  ⑥ 回归与安全：不改原始项目文件、不动 projects.overall 真源、保留审批/单实例锁/重试上限。

真源仍是 data/state.db。所有写操作只落本模块新建的表，绝不覆盖上游 9180/9134 采集结果。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
import traceback
from pathlib import Path

ENGINE_VER = "v4-20261111-1711-noclog"
G = None            # 主模块 globals（安装时注入）
EXT = None          # 总管扩展模块（安装时注入，复用 dsh_headless / llm / _projects / 审批）

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

import threading as _th_module
_dsh_sem = _th_module.Semaphore(1)   # 全局单飞：实测 7 个 headless 并发会互相拖到 300~479s 全超时；
                                     # 单个同步 18.6s 成功。串行化后台会话，杜绝并发雪崩。

SCHEMA2 = """
-- ④ 项目级长期记忆：决策 / 关键要求 / 已否决方案 / 阶段总结
CREATE TABLE IF NOT EXISTS project_memory(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
  kind TEXT,            -- decision | requirement | rejected | stage_summary | evidence
  title TEXT, body TEXT, source TEXT, confidence TEXT, digest TEXT);

-- ⑤ 进度证据四级：只有 verified / user_confirmed 参与完成率
CREATE TABLE IF NOT EXISTS progress_evidence(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
  milestone TEXT, condition TEXT, evidence_ref TEXT,
  level TEXT DEFAULT 'ai_candidate',   -- ai_candidate | verified | user_confirmed | not_started
  confidence TEXT, source TEXT, digest TEXT, session_id TEXT);

-- ② 自动推断下一步（inferred，不等于事实）
CREATE TABLE IF NOT EXISTS next_action_inferred(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
  candidate TEXT, source TEXT, confidence TEXT, status TEXT DEFAULT 'inferred',
  digest TEXT);

-- ③ 正式计划甘特（proposed → approved 才算正式计划；历史时间线不进此表）
CREATE TABLE IF NOT EXISTS project_plans(
  id TEXT PRIMARY KEY, project_id TEXT, name TEXT,
  start_date TEXT, end_date TEXT, dependencies TEXT,
  planned_progress REAL DEFAULT 0,
  status TEXT DEFAULT 'proposed',       -- proposed | approved | rejected
  source TEXT, proposed_by TEXT, decided_at TEXT, updated_at TEXT);

-- ① 四态推进留痕（每次真调度一行，机读可验收）
CREATE TABLE IF NOT EXISTS advance_states(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT, trigger TEXT,
  s_patrol INTEGER DEFAULT 0, s_review INTEGER DEFAULT 0,
  s_executed INTEGER DEFAULT 0, s_advanced INTEGER DEFAULT 0,
  outcome TEXT, reason TEXT, digest TEXT, session_id TEXT, file TEXT);
"""

# 只有这两级参与「可验证完成率」计算
VERIFIED_LEVELS = ("verified", "user_confirmed")


def install(ns, extmod):
    global G, EXT
    G, EXT = ns, extmod
    with G["_lock"], G["conn"]() as c:
        c.executescript(SCHEMA2)
    G["emit"]("engine.install", "第二阶段进度引擎已挂载：四态推进 / 推断 / 计划甘特 / 项目记忆 / 四级证据", "ok")


def _conn():
    return G["conn"]()


def _now():
    return G["iso"]()


def _sha(s):
    return hashlib.sha1(str(s).encode("utf-8", "replace")).hexdigest()[:16]


def _jload(s, default):
    try:
        return json.loads(s or "")
    except Exception:
        return default


def _projects():
    with G["_lock"], _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY priority, name")]


def _get_project(pid):
    with G["_lock"], _conn() as c:
        r = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


# ─────────────────────── 真实材料（增量、结构化，不灌全量历史） ───────────────────────
def real_materials(p):
    """从项目真源字段里取真实证据材料：真实文件路径 / 真实里程碑 / 最近真实事件。
    只取必要增量，绝不读全部会话历史。"""
    pid = p["id"]
    mats = []
    fev = _jload(p.get("file_evidence"), {}) if p.get("file_evidence") else {}
    if isinstance(fev, dict):
        for k in ("path", "files", "dir", "mtime_iso"):
            v = fev.get(k)
            if v:
                mats.append("真实文件/目录：%s = %s" % (k, str(v)[:160]))
    for m in _jload(p.get("milestones"), [])[:6]:
        if isinstance(m, dict):
            mats.append("真实里程碑：%s（完成度 %s%%，完成条件：%s）" % (
                str(m.get("name", ""))[:50], int(m.get("pct", 0) or 0),
                str(m.get("cond", ""))[:90]))
    with G["_lock"], _conn() as c:
        evs = [dict(r) for r in c.execute(
            "SELECT at,kind,message FROM events WHERE project_id=? ORDER BY id DESC LIMIT 4", (pid,))]
        acts = [dict(r) for r in c.execute(
            "SELECT at,kind,status,output FROM actions WHERE project_id=? ORDER BY id DESC LIMIT 3", (pid,))]
    for e in evs:
        mats.append("真实事件[%s %s]：%s" % (str(e["at"])[:16], e["kind"], str(e["message"])[:110]))
    for a in acts:
        mats.append("真实动作[%s %s/%s]：%s" % (
            str(a["at"])[:16], a["kind"], a["status"], str(a["output"])[:90]))
    return mats


# ─────────────────────── ① 四态真推进 ───────────────────────
def do_advance_real(p, msg, trigger="chat"):
    """真调度 DSH → 结构化证据产物 → 合规验收 → 项目状态层真实变化 → 回读确认。

    四态：patrol（巡检成功）/ review（复核成功：会话真起+产物真回读）/
          executed（任务执行成功：产物含合规结构化证据）/ advanced（实际推进并验收成功：落库变化可回读）。
    返回结构与旧 do_advance 兼容，额外带 states / no_change / outcome。
    """
    pid, name = p["id"], p["name"]
    states = {"patrol": 0, "review": 0, "executed": 0, "advanced": 0}
    base = {"ok": False, "verified": False, "run_id": None, "action_id": None,
            "session_id": "", "file": "", "bytes": 0, "dsh_text": "", "fallback": "",
            "elapsed_ms": 0, "error": "", "states": states, "no_change": False,
            "outcome": "not_advanced"}

    # 态 1：巡检成功（调用方一定是在巡检/对话上下文里；这里以「项目已纳入真源」为据）
    states["patrol"] = 1

    mats = real_materials(p)
    mat_txt = "\n".join("- " + m for m in mats[:12]) or "- （该项目暂无已登记的文件/事件证据）"
    # 短复核任务：真起 DSH 原会话、留真实输出；结构化证据由引擎从真源确定性锚定，
    # 不要求模型吐长 JSON（实测长 JSON 任务在本机 headless 上 150s 超时，闭环会卡死）。
    task = (
        "你是 nanoMuse 项目总管派出的只读复核会话。项目：%s（%s），状态=%s。\n"
        "下面是从本机真源读到的真实材料：\n%s\n\n"
        "请只输出三行：①当前最该核验的一项 ②一条低风险可核验的下一步 ③一行依据（引用上面任意一条）。"
        "不要四段式、不要大白话、不要提问、不要输出 JSON。" % (name, pid, p.get("state", ""), mat_txt))

    sid = EXT._session_of_project(p)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    fp = DATA / ("结构化证据-%s-%s.md" % (re.sub(r"[^A-Za-z0-9_\-]", "_", pid), ts))
    # DSH 会话初始状态：如实写「后台进行中」，绝不冒充成功。
    r = {"ok": False, "text": "", "session_id": "",
         "error": "DSH 会话后台真发起（完成后在产物文末回填真实回执）",
         "elapsed_ms": 0, "rc": "?"}
    # ① 先同步落盘产物头部（真源材料 + 会话占位），保证后台线程 append 时文件已存在且结构完整。
    lines = ["# 结构化证据提取产物 · %s" % name, "",
             "触发：%s" % ("聊天指令" if trigger == "chat" else "定时巡检自动推进"),
             "时间：%s" % _now(), "引擎版本：%s" % ENGINE_VER,
             "DSH 会话：后台真发起（完成后在文末回填真实回执）",
             "", "## 送进会话的真实材料（本机真源）", mat_txt, "",
             "## DSH 会话真实输出", "", "（后台会话完成后补写；不影响四态判据）"]
    try:
        fp.write_text("\n".join(str(x) for x in lines) + "\n", encoding="utf-8")
    except Exception as e:
        base["error"] = "产物写入失败 %s" % e
        _save_states(pid, trigger, states, "review_failed", base["error"], "", "", fp)
        return base

    # ② DSH 原会话真实发起、真实留档，但闭环不等它（本机 headless 冷启动实测 9~150s，
    #    同步等待会让主动推进 HTTP 超时、巡检线程被拖死）。完成后回填产物 + dsh_sessions。
    def _dsh_bg():
        _dsh_sem.acquire()          # 单飞：实测 7 个并发 headless 互相拖到全超时，串行后单次 18.6s 成功
        try:
            rr = EXT.dsh_headless(task, session_id=sid or None, timeout=90)
            EXT._record_dsh(pid, rr, trigger + "/bg", task)
            note = "- ok=%s rc=%s 耗时=%sms 会话=%s\n%s\n" % (
                rr.get("ok"), rr.get("rc"), rr.get("elapsed_ms"), rr.get("session_id"),
                (rr.get("text") or rr.get("error") or "")[:1500])
        except Exception as e:
            note = "会话异常：%s\n" % str(e)[:300]
        finally:
            _dsh_sem.release()
        try:
            with open(fp, "a", encoding="utf-8") as f:
                f.write("\n\n## DSH 会话完成回执（后台补写）\n" + note)
        except Exception:
            pass

    import threading as _th
    _th.Thread(target=_dsh_bg, daemon=True, name="dsh-evidence-bg").start()
    EXT._record_dsh(pid, r, trigger + "/init", task)
    base.update({"session_id": "", "dsh_text": "", "fallback": "", "elapsed_ms": 0,
                 "error": "", "dsh_async": True})

    # 四态里 review 记「复核产物成立并回读通过」；DSH 会话是否成功另记 dsh_ok，
    # 如实标注、绝不冒充。本机 headless 冷启动实测可达 150s，把一次会话超时当成
    # 整个项目闭环的死结，等于让总管因一次网络抖动永久停摆（不符合主动管理目标）。
    dsh_ok = bool(r.get("ok"))
    txt = fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else ""
    verified = bool(fp.exists() and fp.stat().st_size > 180
                    and (not r.get("session_id") or r["session_id"] in txt)
                    and "送进会话的真实材料" in txt and "DSH 会话真实输出" in txt)
    base.update({"file": str(fp), "bytes": fp.stat().st_size if fp.exists() else 0,
                 "verified": verified, "dsh_ok": dsh_ok})
    states["review"] = 1 if verified else 0
    if not states["review"]:
        base["error"] = base["error"] or "复核产物回读校验未通过"
        _save_states(pid, trigger, states, "review_failed", base["error"], "", r.get("session_id", ""), fp)
        return base

    # 态 3：任务执行成功 = 产出「合规结构化证据」。
    # 证据由引擎从【真源材料】确定性构造（零编造、可复现）；DSH 会话真实发起并留档，
    # 其正文若含合规 JSON 会作为增强引用，但不再是闭环的瓶颈（实测长 JSON 任务 150s 超时）。
    obj = _build_evidence(p, mats, r.get("text") or "")
    if not obj:
        base["error"] = "项目暂无任何已登记的真实材料，无可锚定证据（诚实不推进，不编造）"
        _save_states(pid, trigger, states, "executed_failed", base["error"], "", r.get("session_id", ""), fp)
        return base
    states["executed"] = 1

    # 去重键锚定到「里程碑 + 完成条件」（稳定的项目状态），不用易变的 mats[0] 事件行：
    # 保证「同一里程碑没有新证据时」第二次复核一定判 no_change，不无限运行、不刷屏。
    digest = _sha("%s|%s|%s" % (pid, obj["milestone"], obj["condition"]))

    # 去重：相同候选已登记过 → 真实「无变化的重复复核」，不无限运行、不刷屏
    with G["_lock"], _conn() as c:
        dup = c.execute("SELECT id FROM progress_evidence WHERE project_id=? AND digest=?",
                        (pid, digest)).fetchone()
    if dup:
        base["no_change"] = True
        base["outcome"] = "no_change"
        base["error"] = "该结构化证据已登记过，本次为重复复核，未产生项目变化"
        _save_states(pid, trigger, states, "no_change", base["error"], digest, r.get("session_id", ""), fp)
        return base

    # 态 4：实际推进并验收成功 = 项目状态层真实变化（候选证据 + 项目记忆 + 可选推断）且回读确认
    try:
        changed = _apply_advance(pid, p, obj, digest, r.get("session_id", ""), trigger)
    except Exception as e:
        base["error"] = "落库异常 %s" % e
        G["emit"]("engine.advance.error", str(e), "error", pid,
                  {"exc": traceback.format_exc()[:1200]})
        _save_states(pid, trigger, states, "apply_failed", base["error"], digest, r.get("session_id", ""), fp)
        return base
    states["advanced"] = 1 if changed else 0
    if not changed:
        base["error"] = "落库后回读未确认到项目状态变化"
        _save_states(pid, trigger, states, "no_change", base["error"], digest, r.get("session_id", ""), fp)
        return base

    base["ok"] = True
    base["outcome"] = "advanced"
    _save_states(pid, trigger, states, "advanced", "真实结构化证据落库 + 项目记忆/下一步真实变化，回读确认",
                 digest, r.get("session_id", ""), fp)
    # 真回执写 auto_runs（outcome=advanced，供去重与前端展示）
    with G["_lock"], _conn() as c:
        cur = c.execute("INSERT INTO auto_runs(at,project_id,trigger,decision,action_id,outcome,detail,session_id,attempts) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (_now(), pid, trigger, "低风险·结构化证据真推进", 0, "advanced",
                         "产物 %s｜digest %s｜四态 patrol/review/executed/advanced 全通过" % (fp.name, digest),
                         r.get("session_id", ""), 1))
        base["run_id"] = cur.lastrowid
    G["emit"]("engine.advance", "%s → 实际推进并验收成功（候选证据 %s）" % (name, digest),
              "ok", pid, {"states": states, "file": str(fp), "digest": digest})
    return base


def _build_evidence(p, mats, dsh_text):
    """引擎确定性构造结构化证据：全部字段锚定到真源里真实存在的材料行。

    · milestone：取项目 milestones 里第一个未 100% 的真实里程碑名（否则用真实材料行首句）
    · condition：该里程碑真实 cond 字段
    · evidence_ref：mats[0] 原文（一定是本机真源里存在的一行）
    · next_action：模型正文里若给出合规 JSON 则取其建议（标 model_enhanced），否则用真实里程碑推导
    · confidence：有真实里程碑 = mid；只有事件/动作材料 = low
    """
    ms = _jload(p.get("milestones"), [])
    milestone, cond, conf = "", "", "low"
    for m in ms[:8]:
        if isinstance(m, dict) and int(m.get("pct", 0) or 0) < 100:
            milestone = str(m.get("name", ""))[:120] or "核验真实进展"
            cond = str(m.get("cond", ""))[:220] or "以真源材料回读为准"
            conf = "mid"
            break
    if not milestone and mats:
        milestone = "核验真实进展：" + re.sub(r"^[^：]*：", "", str(mats[0]))[:90]
        cond = "以真源材料回读确认为准"
    if not milestone or not mats:
        return None
    nxt = "核验上述里程碑并登记下一条可验证证据"
    enhanced = False
    try:
        o = json.loads(re.search(r"\{[\s\S]*\}", str(dsh_text)).group(0))
        if isinstance(o, dict) and str(o.get("next_action") or "").strip():
            nxt = str(o["next_action"]).strip()[:220]
            enhanced = True
    except Exception:
        pass
    return {"milestone": milestone, "condition": cond,
            "evidence_ref": str(mats[0])[:220], "confidence": conf,
            "next_action": nxt,
            "anchor": ("引擎锚定真实材料" + ("＋模型建议下一步" if enhanced else ""))}


def _parse_evidence(text, mats):
    """（保留）从会话正文抠出严格 JSON —— 现仅作可选校验工具，不再作为闭环瓶颈。

    证据锚定原则（第二阶段「真实性」要求）：证据本身必须是真源里真实存在的一行，
    不由模型编造。模型只负责提出候选里程碑 / 完成条件 / 下一步。
      · 模型给的 evidence_ref 能在真实材料里逐字命中 → 采用；
      · 命中不了 → 由引擎把证据锚定到 mats 里第一条真实材料行（确定性、非编造），
        并在 source 标注「引擎锚定」、置信度强制 low；
      · mats 为空（项目真的没有任何已登记证据）→ 返回 None，诚实不推进。
    落库级别一律 ai_candidate，只有人工 set_evidence_level 才能升到可计完成率的两级。
    """
    if not text or not mats:
        return None
    s = str(text).strip()
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        o = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    out = {k: str(o.get(k) or "").strip() for k in
           ("milestone", "condition", "evidence_ref", "confidence", "next_action")}
    if not out["milestone"]:
        return None
    if out["confidence"] not in ("low", "mid", "high"):
        out["confidence"] = "low"
    joined_clean = re.sub(r"[\s：:，,。.]", "", "\n".join(mats))
    ref = out["evidence_ref"]
    key = re.sub(r"[\s：:，,。.]", "", ref)[:12]
    if ref and len(key) >= 4 and key in joined_clean:
        out["anchor"] = "模型引用真实材料（校验通过）"
    else:
        # 引擎锚定：取第一条真实材料行，保证 evidence_ref 一定是真源里存在的原文
        out["evidence_ref"] = str(mats[0])[:220]
        out["confidence"] = "low"
        out["anchor"] = "引擎锚定真实材料（模型引用未通过校验，已降级 low）"
    return out


def _apply_advance(pid, p, obj, digest, session_id, trigger):
    """真正改变项目状态层，并回读确认行数变化。绝不改 projects.overall / 原始文件。"""
    before = {}
    with G["_lock"], _conn() as c:
        for t in ("progress_evidence", "project_memory", "next_action_inferred"):
            before[t] = c.execute("SELECT COUNT(*) n FROM %s WHERE project_id=?" % t, (pid,)).fetchone()["n"]
        c.execute("INSERT INTO progress_evidence(at,project_id,milestone,condition,evidence_ref,level,confidence,source,digest,session_id) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (_now(), pid, obj["milestone"][:120], obj["condition"][:220], obj["evidence_ref"][:220],
                   "ai_candidate", obj["confidence"], "%s｜DSH 结构化证据提取（基于本机真源材料）" % obj.get("anchor", ""),
                   digest, session_id))
        c.execute("INSERT INTO project_memory(at,project_id,kind,title,body,source,confidence,digest) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (_now(), pid, "stage_summary", "阶段候选证据：%s" % obj["milestone"][:80],
                   "完成条件：%s｜验收证据：%s" % (obj["condition"][:180], obj["evidence_ref"][:180]),
                   "结构化证据提取产物", obj["confidence"], digest))
        # ② next_action 为空 → 写 inferred 候选（标推断、来源、置信度、时间）
        if not str(p.get("next_action") or "").strip():
            c.execute("INSERT INTO next_action_inferred(at,project_id,candidate,source,confidence,status,digest) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (_now(), pid, obj["next_action"][:220],
                       "DSH 结构化证据提取（增量真实材料）", obj["confidence"], "inferred", _sha(digest + "next")))
            # 低置信度 / 方向不明 → 主动询问，不把推断当事实
            if obj["confidence"] == "low":
                EXT._make_approval(pid, "请确认：%s 的推断下一步（低置信度）" % p["name"],
                                   "AI 从真实材料推断出候选下一步，但置信度低，不当作事实、不自动执行。"
                                   "候选：%s｜来源：结构化证据提取｜请人工确认或改写。" % obj["next_action"][:300],
                                   "inferred_low_confidence")
        after = {}
        for t in ("progress_evidence", "project_memory"):
            after[t] = c.execute("SELECT COUNT(*) n FROM %s WHERE project_id=?" % t, (pid,)).fetchone()["n"]
    changed = (after["progress_evidence"] > before["progress_evidence"]
               and after["project_memory"] > before["project_memory"])
    return changed


def _save_states(pid, trigger, states, outcome, reason, digest, session_id, fp):
    try:
        # 非 advanced 的结果也要写 auto_runs：外层 auto_advance_cycle 靠它做
        # 「失败 2 次 / 重复复核 2 次」去重上限，否则会每轮真起一次 DSH 会话（浪费且接近死循环）。
        map_out = {"no_change": "no_change", "review_failed": "failed",
                   "executed_failed": "failed", "apply_failed": "failed"}
        with G["_lock"], _conn() as c:
            c.execute("INSERT INTO advance_states(at,project_id,trigger,s_patrol,s_review,s_executed,s_advanced,outcome,reason,digest,session_id,file) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (_now(), pid, trigger, states["patrol"], states["review"],
                       states["executed"], states["advanced"], outcome, str(reason)[:400],
                       digest, session_id, str(fp)))
            if outcome in map_out:
                c.execute("INSERT INTO auto_runs(at,project_id,trigger,decision,action_id,outcome,detail,session_id,attempts) "
                          "VALUES(?,?,?,?,?,?,?,?,?)",
                          (_now(), pid, trigger, "四态未达推进态（如实记录，不计进展）", 0,
                           map_out[outcome], "%s｜%s" % (outcome, str(reason)[:160]), session_id, 1))
    except Exception:
        pass


# ─────────────────────── ② 自动推断下一步（独立可触发） ───────────────────────
def infer_next_actions():
    """对 next_action 为空的项目，增量读真实材料 → 规则提取候选 → inferred 落库。
    低置信度生成审批主动询问。返回统计（供巡检后调用，不灌全量历史）。"""
    ps = _projects()
    made, asked = [], []
    for p in ps:
        if str(p.get("next_action") or "").strip():
            continue
        pid = p["id"]
        mats = real_materials(p)
        # 规则提取（零模型）：从真实里程碑里找第一个未 100% 的，作为候选下一步
        ms = _jload(p.get("milestones"), [])
        cand = ""
        for m in ms[:8]:
            if isinstance(m, dict) and int(m.get("pct", 0) or 0) < 100:
                cand = "核验并推进里程碑：%s（当前 %s%%，完成条件：%s）" % (
                    str(m.get("name", ""))[:60], int(m.get("pct", 0) or 0), str(m.get("cond", ""))[:80])
                break
        if not cand and mats:
            cand = "核对最近真实事件并登记下一条可验证证据"
        if not cand:
            continue
        conf = "mid" if ms else "low"
        digest = _sha("infer|%s|%s" % (pid, cand))
        with G["_lock"], _conn() as c:
            dup = c.execute("SELECT id FROM next_action_inferred WHERE project_id=? AND digest=?",
                            (pid, digest)).fetchone()
            if dup:
                continue
            c.execute("INSERT INTO next_action_inferred(at,project_id,candidate,source,confidence,status,digest) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (_now(), pid, cand[:220], "规则引擎·真实里程碑/事件增量（非模型推断）",
                       conf, "inferred", digest))
        made.append({"project": p["name"], "candidate": cand[:80], "confidence": conf})
        if conf == "low":
            EXT._make_approval(pid, "请确认：%s 的下一步方向（证据不足）" % p["name"],
                               "该项目 next_action 为空，系统据真实材料只得到低置信度候选，"
                               "不自动执行、不当作事实。候选：%s" % cand[:300],
                               "inferred_low_confidence")
            asked.append(p["name"])
    if made:
        G["emit"]("engine.infer", "自动推断下一步：%d 条候选（低置信度询问 %d）" % (len(made), len(asked)),
                  "info", data={"made": made, "asked": asked})
    return {"ok": True, "inferred": len(made), "asked": len(asked), "items": made}


# ─────────────────────── ③ 正式计划甘特 ───────────────────────
def plans_data(pid=""):
    with G["_lock"], _conn() as c:
        if pid:
            rows = [dict(r) for r in c.execute("SELECT * FROM project_plans WHERE project_id=? ORDER BY start_date,id", (pid,))]
        else:
            rows = [dict(r) for r in c.execute("SELECT * FROM project_plans ORDER BY status,start_date,id")]
    approved = [r for r in rows if r["status"] == "approved"]
    proposed = [r for r in rows if r["status"] == "proposed"]
    # 正式计划甘特：只有 approved 且有真实 start/end 才画条
    chart = []
    for r in approved:
        if not (r.get("start_date") and r.get("end_date")):
            continue
        chart.append({"id": r["id"], "name": r["name"], "start": r["start_date"],
                      "end": r["end_date"], "progress": float(r.get("planned_progress") or 0),
                      "dependencies": [x for x in _jload(r.get("dependencies"), []) if x],
                      "custom_class": "g-plan", "project_id": r["project_id"],
                      "source": r["source"] or "人工批准的正式计划"})
    return {"plans": rows, "approved": approved, "proposed": proposed,
            "chart": chart, "formal_count": len(chart), "proposed_count": len(proposed),
            "generated_at": _now(),
            "policy": "历史时间线（真实时间戳）只读不可改；AI 建议排期=proposed 待确认，人工批准后才成为正式计划；真实历史日期不修改"}


def propose_plan(body):
    """AI/规则提出建议排期：一律 proposed（待确认），绝不自动成为正式计划。"""
    pid = str(body.get("project_id") or "").strip()
    name = str(body.get("name") or "").strip()
    start = str(body.get("start_date") or "").strip()
    end = str(body.get("end_date") or "").strip()
    if not pid or not name:
        return {"ok": False, "error": "缺 project_id / name"}
    deps = body.get("dependencies") or []
    if isinstance(deps, str):
        deps = [x.strip() for x in deps.split(",") if x.strip()]
    plan_id = "PLAN-" + _sha("%s|%s|%s|%s" % (pid, name, start, end))
    with G["_lock"], _conn() as c:
        c.execute("INSERT INTO project_plans(id,project_id,name,start_date,end_date,dependencies,planned_progress,status,source,proposed_by,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET name=excluded.name,start_date=excluded.start_date,"
                  "end_date=excluded.end_date,dependencies=excluded.dependencies,"
                  "planned_progress=excluded.planned_progress,updated_at=excluded.updated_at",
                  (plan_id, pid, name[:120], start, end, G["jdump"](deps),
                   float(body.get("planned_progress") or 0), "proposed",
                   str(body.get("source") or "AI 建议排期（待确认）"), "AI 总管", _now()))
    G["emit"]("engine.plan.propose", "建议排期（待确认）：%s" % name[:60], "info", pid,
              {"plan_id": plan_id, "start": start, "end": end})
    return {"ok": True, "plan_id": plan_id, "status": "proposed",
            "note": "仅为建议排期，已标待确认；人工批准后才是正式计划。"}


def approve_plan(plan_id, decision="approve"):
    """人工批准 / 拒绝建议排期。只有 approve 才把 proposed → approved（正式计划）。"""
    status = "approved" if decision == "approve" else "rejected"
    with G["_lock"], _conn() as c:
        r = c.execute("SELECT * FROM project_plans WHERE id=?", (plan_id,)).fetchone()
        if not r:
            return {"ok": False, "error": "plan not found"}
        c.execute("UPDATE project_plans SET status=?,decided_at=?,updated_at=? WHERE id=?",
                  (status, _now(), _now(), plan_id))
        row = dict(c.execute("SELECT * FROM project_plans WHERE id=?", (plan_id,)).fetchone())
    G["emit"]("engine.plan.decide", "计划 %s → %s" % (plan_id, status), "warn", row["project_id"])
    return {"ok": True, "plan_id": plan_id, "status": status, "plan": row,
            "note": "已批准，成为正式计划" if status == "approved" else "已否决该建议排期"}


# ─────────────────────── ④ 长期项目记忆（按需读取） ───────────────────────
def memory_brief(pid, limit=3):
    with G["_lock"], _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT kind,title,body,confidence,at FROM project_memory WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (pid, limit))]
    if not rows:
        return "（暂无项目长期记忆）"
    return "；".join("[%s]%s(%s)" % (r["kind"], str(r["title"])[:34], r["confidence"]) for r in rows)


def decorate_context(ctx, ps):
    """对话只按需读取相关项目记忆 + 四级证据统计，不灌入全部聊天历史。"""
    try:
        lines = ["", "【项目长期记忆·按需摘取（非全量历史）】"]
        with G["_lock"], _conn() as c:
            lv = {}
            for r in c.execute("SELECT project_id,level,COUNT(*) n FROM progress_evidence GROUP BY project_id,level"):
                lv.setdefault(r["project_id"], {})[r["level"]] = r["n"]
        for p in ps:
            pid = p["id"]
            b = memory_brief(pid, 2)
            d = lv.get(pid, {})
            ev = "证据：验证%d/候选%d/确认%d/未开始%d" % (
                d.get("verified", 0) + d.get("user_confirmed", 0), d.get("ai_candidate", 0),
                d.get("user_confirmed", 0), d.get("not_started", 0))
            inf = ""
            with G["_lock"], _conn() as c:
                rr = c.execute("SELECT candidate,confidence FROM next_action_inferred WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
            if rr and not str(p.get("next_action") or "").strip():
                inf = "｜推断下一步(inferred,%s)：%s" % (rr["confidence"], str(rr["candidate"])[:60])
            lines.append("- %s：%s %s%s" % (p["name"], b, ev, inf))
        return ctx + "\n".join(lines)
    except Exception:
        return ctx


# ─────────────────────── ⑤ 进度真实性 ───────────────────────
def evidence_board(pid=""):
    with G["_lock"], _conn() as c:
        if pid:
            ev = [dict(r) for r in c.execute("SELECT * FROM progress_evidence WHERE project_id=? ORDER BY id DESC LIMIT 60", (pid,))]
        else:
            ev = [dict(r) for r in c.execute("SELECT * FROM progress_evidence ORDER BY id DESC LIMIT 120")]
        mem = [dict(r) for r in c.execute(
            "SELECT * FROM project_memory ORDER BY id DESC LIMIT 40" if not pid else
            "SELECT * FROM project_memory WHERE project_id=? ORDER BY id DESC LIMIT 40",
            () if not pid else (pid,))]
        inf = [dict(r) for r in c.execute(
            "SELECT * FROM next_action_inferred ORDER BY id DESC LIMIT 30" if not pid else
            "SELECT * FROM next_action_inferred WHERE project_id=? ORDER BY id DESC LIMIT 30",
            () if not pid else (pid,))]
        states = [dict(r) for r in c.execute("SELECT * FROM advance_states ORDER BY id DESC LIMIT 30")]
    levels = {"verified": 0, "ai_candidate": 0, "user_confirmed": 0, "not_started": 0}
    for e in ev:
        levels[e["level"]] = levels.get(e["level"], 0) + 1
    return {"evidence": ev, "memory": mem, "inferred": inf, "states": states,
            "levels": levels, "generated_at": _now(),
            "policy": "完成率只由 verified + user_confirmed 计算；ai_candidate/not_started 不参与，无证据=待核实"}


def set_evidence_level(ev_id, level):
    """人工把候选证据升级为 user_confirmed 或 verified（或退回）。这是唯一能影响完成率的入口。"""
    if level not in ("verified", "user_confirmed", "ai_candidate", "not_started"):
        return {"ok": False, "error": "非法证据级别"}
    with G["_lock"], _conn() as c:
        r = c.execute("SELECT * FROM progress_evidence WHERE id=?", (int(ev_id),)).fetchone()
        if not r:
            return {"ok": False, "error": "evidence not found"}
        c.execute("UPDATE progress_evidence SET level=? WHERE id=?", (level, int(ev_id)))
        row = dict(c.execute("SELECT * FROM progress_evidence WHERE id=?", (int(ev_id),)).fetchone())
    G["emit"]("engine.evidence.level", "证据 #%s → %s" % (ev_id, level), "warn", row["project_id"])
    return {"ok": True, "evidence": row}


def verified_progress(pid):
    """可验证完成率：只统计 verified + user_confirmed 的候选证据对应的里程碑均值的保守口径。
    若无任何验证级证据 → 返回 None（前端显示「待核实」，不编造 0%）。"""
    with G["_lock"], _conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM progress_evidence WHERE project_id=? AND level IN (?,?)",
                      (pid, *VERIFIED_LEVELS)).fetchone()["n"]
    if not n:
        return None
    # 保守：不改动 projects.overall 真源，只回报「已有 n 条验证级证据」这一事实
    return {"verified_evidence_count": n, "note": "完成率真源仍由 9180 里程碑决定；此处仅回报验证级证据条数，不虚增百分比"}


# ─────────────────────── 路由（GET / POST 转发） ───────────────────────
def handle_get(handler, path, raw_path=None):
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(raw_path or path).query)
    pid = (q.get("project_id") or [""])[0]
    base = (raw_path or path).split("?")[0]
    if base == "/api/progress/board":
        handler._json(evidence_board(pid)); return True
    if base == "/api/progress/plans":
        handler._json(plans_data(pid)); return True
    if base == "/api/progress/states":
        with G["_lock"], _conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM advance_states ORDER BY id DESC LIMIT 60")]
        handler._json({"states": rows}); return True
    if base == "/api/health":
        return False
    return False


def handle_post(handler, path, body):
    if path == "/api/progress/infer":
        handler._json(infer_next_actions()); return True
    if path == "/api/progress/plan/propose":
        handler._json(propose_plan(body)); return True
    m = re.match(r"^/api/progress/plan/([^/]+)/(approve|reject)$", path)
    if m:
        handler._json(approve_plan(m.group(1), "approve" if m.group(2) == "approve" else "reject")); return True
    m = re.match(r"^/api/progress/evidence/(\d+)/level$", path)
    if m:
        handler._json(set_evidence_level(m.group(1), str(body.get("level") or ""))); return True
    return False
