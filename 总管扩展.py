# -*- coding: utf-8 -*-
"""nanoMuse 9196 · 交互升级扩展（P1 真 AI 对话 / P2 甘特 / P3 主动闭环）

架构：install(ns) 注入主模块 globals；handle_get / handle_post 接管新路由；
      auto_advance_cycle() 由 patrol 调用。主模块仅 4 处插桩，既有逻辑一字不改。

事实来源唯一：data/state.db（9 项目 / snapshots / events / actions / approvals）
模型链路（P0 实测，非推测）：
  · 对话  wb @ http://127.0.0.1:7864/v1  model=cn:hy3   —— 2.6s / finish=stop
    （Host profiles/desktop/cordis.patch.yml 的 agent-default-model 生效档）
  · DSH   dsh --profile headless --patch headless-overlay.yml --json <task>
    —— 真起 DSH 会话，返回 sessionId / type:text / final.text（EXIT=0）
    overlay 是本项目隔离文件，不改任何生产 profile / 凭据 / 配置。
  · 甘特  frappe-gantt 1.2.2 (MIT) 本地静态件；无真实日期一律「待排期」。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

G = None
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "web" / "static"
OVERLAY = ROOT / "headless-overlay.yml"
DSH_CMD = r"F:\Users\Administrator\bin\dsh.cmd"
CRED = Path(os.environ.get("DSH_HOME", r"I:\DSHHome")) / ".credentials.yaml"
LLM_URL = "http://127.0.0.1:7864/v1/chat/completions"
# P0 实测（2026-11-11）：cn:hy3 / hy4std 在长 system 下会把提示词整段复述进正文（LEAK）；
# glm-5.3-flash 实测 7.2s / finish=stop / 175 字 / 不复述规则。故对话用后者。
LLM_MODEL = "glm-5.3-flash"

_auto_lock = threading.Lock()

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, role TEXT, text TEXT,
  project_id TEXT, action_id INTEGER, approval_id INTEGER, model TEXT,
  elapsed_ms INTEGER, context_json TEXT);
CREATE TABLE IF NOT EXISTS gantt_tasks(
  id TEXT PRIMARY KEY, project_id TEXT, name TEXT, start_date TEXT, end_date TEXT,
  progress REAL, dependencies TEXT, state TEXT, scheduled INTEGER DEFAULT 0,
  source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS auto_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT, trigger TEXT,
  decision TEXT, action_id INTEGER, outcome TEXT, detail TEXT, session_id TEXT,
  attempts INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS dsh_sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT, session_id TEXT,
  kind TEXT, task TEXT, outcome TEXT, final_text TEXT, elapsed_ms INTEGER);
"""

HIGH_RISK = re.compile(
    r"删除|删掉|移除|清空|覆盖|重写|发布|上线|部署|付费|支付|购买|重置|回滚|"
    r"drop|delete|push|deploy|publish|overwrite|reset|rm\s-|format", re.I)
ADVANCE = re.compile(
    r"推进|执行|跑一下|跑一跑|帮我做|帮我跑|接续|继续|开始|复核|生成摘要|生成报告|"
    r"调度|派发|核验|检查成果|处理一下|动手", re.I)
QUERY = re.compile(
    r"哪个|哪些|最需要|最急|多少|列出|什么|怎么|如何|进度|状态|阻塞|下一步|"
    r"优先级|汇总|总览|介绍一下|看看|告诉我|为什么|吗|？|\?", re.I)


def install(ns):
    """把主模块的工具函数注入本模块，并幂等建立新表（旧表一字不动）。"""
    global G
    G = ns
    with G["_lock"], G["conn"]() as c:
        c.executescript(EXTRA_SCHEMA)
    G["emit"]("ext.install", "交互升级扩展已挂载：chat / gantt / auto-runs / dsh-headless", "ok")


# ────────────────────────────── 凭据 / 模型 ──────────────────────────────
def _cred(key):
    """只读 .credentials.yaml，按 refs 键取值；绝不写回、绝不打印明文。"""
    try:
        txt = CRED.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"(?m)^\s*" + re.escape(key) + r"\s*:\s*(.+?)\s*$", txt)
        if not m:
            return ""
        v = m.group(1).strip().strip('"').strip("'")
        if v.startswith("{"):
            try:
                j = json.loads(v)
                return str(j.get("api_key") or j.get("key") or j.get("access_token") or v)
            except Exception:
                pass
        return v
    except Exception:
        return ""


LEAK_MARK = re.compile(
    r"我们被问到|根据规则|我是项目总管|禁止输出|需要分析|分析：|严格规则|"
    r"用户问：|本次用户提出|我需要回答用户|不要复述系统提示|不要输出免责声明|"
    r"项目数据库】是唯一事实来源|从9个项目里找出|结合要求写简短中文|"
    r"我们需要分析用户|首先，根据项目数据库|可以更贴合项目数据库", re.I)


def _clean_llm_text(text):
    """模型偶尔把 system 提示词/思考过程吐进正文：按行剔除泄漏行，保留真实结论。

    若整段都是泄漏（无有效行），返回空串 → 上层走规则引擎兜底，绝不把泄漏内容交给用户。
    """
    if not text:
        return ""
    lines = [ln for ln in str(text).splitlines() if not LEAK_MARK.search(ln)]
    out = "\n".join(lines).strip()
    if len(out) < 8:
        return ""
    return out


def llm(messages, max_tokens=1200, timeout=45):
    """真模型调用：Host 当前生效的 wb 本机代理。返回可校验的结构。"""
    key = _cred("WB_API_KEY")
    if not key:
        return {"ok": False, "text": "", "error": "WB_API_KEY 未取到"}
    body = json.dumps({"model": LLM_MODEL, "messages": messages,
                       "max_tokens": max_tokens, "temperature": 0.3,
                       "stream": False}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        LLM_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key,
                 "User-Agent": "nanoMuse-project-steward/2.0"},
        method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        ch = (d.get("choices") or [{}])[0]
        text = _clean_llm_text((ch.get("message") or {}).get("content") or "")
        return {"ok": bool(text.strip()), "text": text.strip(),
                "finish": ch.get("finish_reason"), "model": LLM_MODEL,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": "", "error": "HTTP %s %s" % (
            e.code, e.read()[:160].decode("utf-8", "replace")),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "text": "", "error": "%s %s" % (type(e).__name__, str(e)[:160]),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000)}


# ────────────────────────────── DSH 原会话调度 ──────────────────────────────
def dsh_headless(task, session_id=None, timeout=150):
    """真起 DSH headless 会话；--json 逐行解析 NDJSON 事件流。

    session_id 非空时优先 --session-id 续原会话；若 unknown id 报错，
    自动回退到新会话并把原因写进 fallback（不伪造续接成功）。
    """
    def _run(sid):
        args = ["cmd", "/c", DSH_CMD, "--profile", "headless", "--patch", str(OVERLAY), "--json"]
        if sid:
            args += ["--session-id", sid]
        args += [task]
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("DSH_HOME", r"I:\DSHHome")
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=G["NOWIN"], cwd=str(G["WORKSPACE"]),
                           text=True, encoding="utf-8", errors="replace", env=env)
        return p

    t0 = time.perf_counter()
    fallback = ""
    try:
        p = _run(session_id)
        if session_id and p.returncode != 0 and re.search(r"unknown|not found|error", (p.stdout or "") + (p.stderr or ""), re.I):
            fallback = "原会话不可续（%s），已回退新会话" % str(p.stderr or "")[-90:].replace("\n", " ")
            p = _run(None)
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "session_id": "", "error": "DSH 会话超时 %ss" % timeout,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "text": "", "session_id": "", "error": "%s %s" % (type(e).__name__, str(e)[:180]),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000)}

    sid, final, texts, events = "", "", [], []
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        events.append({"type": e.get("type"), "phase": e.get("phase")})
        if e.get("type") == "session":
            sid = e.get("sessionId") or sid
        elif e.get("type") == "text":
            texts.append(str(e.get("text") or ""))
        elif e.get("type") == "final":
            final = str(e.get("text") or "") or final
    if not final and texts:
        final = "".join(texts)
    ok = p.returncode == 0 and bool(final.strip())
    return {"ok": ok, "text": final.strip(), "session_id": sid,
            "rc": p.returncode, "events": events[-12:], "fallback": fallback,
            "stderr": (p.stderr or "")[-260:],
            "error": "" if ok else ("DSH rc=%s stderr=%s" % (p.returncode, (p.stderr or "")[-160:])),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000)}


def _session_of_project(p):
    """从项目 sessions 证据里提取原 DSH 会话 id（用于优先接续）。"""
    try:
        ss = json.loads(p.get("sessions") or "[]")
    except Exception:
        ss = []
    for s in ss[:3]:
        f = str(s.get("file") or "")
        m = re.search(r"(session-[0-9a-f\-]{8,})", f)
        if m:
            return m.group(1)
    return ""


# ────────────────────────────── P1 真 AI 对话 ──────────────────────────────
def _projects():
    with G["_lock"], G["conn"]() as c:
        return [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY priority, name")]


def _jload(s, default):
    try:
        return json.loads(s or "")
    except Exception:
        return default


def build_context(ps):
    """结构化摘要：每项目 ~250 字，只取必要字段，绝不灌全量会话历史。"""
    LBL = {"active": "正常推进", "stalled": "已停滞",
           "awaiting_decision": "待人工决策", "pending_acceptance": "待验收"}
    cnt = {}
    for p in ps:
        cnt[p["state"]] = cnt.get(p["state"], 0) + 1
    lines = ["【项目数据库·唯一事实来源】共 %d 个项目；状态分布 %s；时间 %s" % (
        len(ps), json.dumps(cnt, ensure_ascii=False), G["iso"]())]
    for p in ps:
        pct = ("%.1f%%" % float(p["overall"])) if p["overall_verified"] else "待核实"
        bl = _jload(p.get("blockers"), [])
        pd = _jload(p.get("pending_decisions"), [])
        ms = _jload(p.get("milestones"), [])
        ms_txt = "；".join("%s(%s%%)" % (str(m.get("name", ""))[:26], int(m.get("pct", 0) or 0)) for m in ms[:4]) or "无可验证里程碑"
        lines.append(
            "- [%s] %s｜状态=%s｜进度=%s｜优先级=%s｜下一步=%s｜阻塞=%d 条｜待决策=%d 条｜里程碑：%s" % (
                p["id"], p["name"], LBL.get(p["state"], p["state"]), pct, p["priority"],
                (str(p.get("next_action") or "（空）")[:120].replace("\n", " ")), len(bl), len(pd), ms_txt))
    return "\n".join(lines)


def pick_project(msg, ps):
    """最长名优先匹配，避免「上货台」抢配「上货台7.0 AI运营中心」（上轮踩过的坑）。"""
    cands = [p for p in ps if p["name"] and p["name"] in msg]
    if cands:
        return sorted(cands, key=lambda x: len(x["name"]), reverse=True)[0]
    # 关键词回退
    kws = {"换脸": "换脸", "人脸": "换脸", "导航": "项目导航", "9180": "项目导航",
           "抖音": "抖音知识库", "知识库": "抖音知识库", "上货台": "上货台",
           "写作": "WriterX", "inkos": "WriterX", "拼多多": "拼多多",
           "显卡": "模型与显卡", "lora": "视频 LoRa", "插画": "视频 LoRa"}
    for k, v in kws.items():
        if k.lower() in msg.lower():
            hit = [p for p in ps if v in p["name"]]
            if hit:
                return hit[0]
    return None


def detect_intent(msg):
    if HIGH_RISK.search(msg):
        return "approval"
    # 问句优先（「哪个项目最需要推进」含「推进」但是查询，不是执行）
    if QUERY.search(msg) and not re.search(r"帮我|请执行|现在就|立刻|马上|动手|跑一次", msg):
        return "query"
    if ADVANCE.search(msg):
        return "advance"
    return "query"


def _chat_save(role, text, project_id="", action_id=None, approval_id=None,
               model="", elapsed_ms=0, context=None):
    with G["_lock"], G["conn"]() as c:
        cur = c.execute(
            "INSERT INTO chat_messages(at,role,text,project_id,action_id,approval_id,model,elapsed_ms,context_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (G["iso"](), role, text, project_id, action_id, approval_id, model,
             elapsed_ms, G["jdump"](context or {})))
        return cur.lastrowid


def _record_dsh(project_id, r, kind, task):
    with G["_lock"], G["conn"]() as c:
        c.execute("INSERT INTO dsh_sessions(at,project_id,session_id,kind,task,outcome,final_text,elapsed_ms) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (G["iso"](), project_id, r.get("session_id", ""), kind, task[:500],
                   "succeeded" if r.get("ok") else "failed", (r.get("text") or "")[:2000],
                   r.get("elapsed_ms", 0)))


def _make_approval(project_id, title, detail, kind="chat_high_risk"):
    with G["_lock"], G["conn"]() as c:
        ex = c.execute("SELECT id FROM approvals WHERE project_id=? AND kind=? AND status='pending' AND title=?",
                       (project_id, kind, title)).fetchone()
        if ex:
            return ex["id"], False
        cur = c.execute("INSERT INTO approvals(at,project_id,kind,title,detail,status) VALUES(?,?,?,?,?,?)",
                        (G["iso"](), project_id, kind, title, detail, "pending"))
        return cur.lastrowid, True


def do_advance(p, msg, trigger="chat"):
    """受控调度 DSH 原会话 → 落真实产物 → 回读校验 → 更新动作/自动记录。"""
    pid, name = p["id"], p["name"]
    reason = "聊天指令：%s" % msg[:120] if trigger == "chat" else "定时巡检发现可推进任务"
    task = (
        "你是 nanoMuse AI 私人总管派出的只读复核会话。只回答，不改任何文件、不装依赖、不联网。"
        "项目：%s（%s）。当前状态：%s，进度：%s。下一步：%s。"
        "请只输出三行：①已完成（依据上面给出的真实状态，不要编造）②阻塞（依据真实阻塞）"
        "③下一步（一条具体、低风险、可核验的建议）。不要四段式、不要大白话、不要提问。"
        % (name, pid, p["state"],
           ("%.1f%%" % float(p["overall"])) if p["overall_verified"] else "待核实",
           str(p.get("next_action") or "（空）")[:220].replace("\n", " ")))

    sid = _session_of_project(p)
    r = dsh_headless(task, session_id=sid or None)
    _record_dsh(pid, r, trigger, task)

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    fp = DATA / ("自动复核-%s-%s.md" % (pid, ts))
    lines = ["# DSH 原会话只读复核 · %s" % name, "",
             "触发：%s" % reason, "时间：%s" % G["iso"](),
             "DSH 会话：%s" % (r.get("session_id") or "（未取得）"),
             "续接原会话：%s" % ("是" if sid else "否（该项目无可用原会话 id）"),
             "会话结果：%s（rc=%s，耗时 %s ms）" % (
                 "succeeded" if r.get("ok") else "failed", r.get("rc", "?"), r.get("elapsed_ms", 0)),
             ("回退原因：%s" % r["fallback"]) if r.get("fallback") else "",
             "", "## 项目真实状态（来自 state.db）",
             "- 状态：%s" % p["state"], "- 进度：%s" % (
                 ("%.1f%%" % float(p["overall"])) if p["overall_verified"] else "待核实"),
             "- 下一步：%s" % (p.get("next_action") or "（空）"),
             "", "## DSH 会话真实输出", "", r.get("text") or ("（无正文）" + str(r.get("error") or ""))]
    try:
        fp.write_text("\n".join(x for x in lines if x is not None) + "\n", encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": "产物写入失败 %s" % e, "dsh": r}

    # 真回读校验：文件存在 + 含真实 sessionId + 含 DSH 正文
    txt = fp.read_text(encoding="utf-8", errors="replace") if fp.exists() else ""
    verified = bool(fp.exists() and fp.stat().st_size > 180
                    and (not r.get("session_id") or r["session_id"] in txt)
                    and (r.get("text") or "无正文") in txt)

    # 更新动作表（真执行结果），没有就补一条
    with G["_lock"], G["conn"]() as c:
        a = c.execute("SELECT id FROM actions WHERE project_id=? AND kind='dsh_session_handoff' ORDER BY id LIMIT 1",
                      (pid,)).fetchone()
        if a:
            aid = a["id"]
            c.execute("UPDATE actions SET status=?,output=?,result=?,checked=?,attempts=attempts+1 WHERE id=?",
                      ("succeeded" if (r.get("ok") and verified) else "failed",
                       "%s (%d bytes)" % (fp, fp.stat().st_size if fp.exists() else 0),
                       "DSH 原会话真执行 + 产物回读校验", 1 if (r.get("ok") and verified) else 0, aid))
        else:
            cur = c.execute("INSERT INTO actions(at,project_id,kind,title,risk,status,attempts,output,result,checked) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (G["iso"](), pid, "dsh_session_handoff", "DSH 会话接续：只读复核「%s」" % name,
                             "low", "succeeded" if (r.get("ok") and verified) else "failed", 1,
                             str(fp), "DSH 原会话真执行 + 产物回读校验", 1 if (r.get("ok") and verified) else 0))
            aid = cur.lastrowid
        cur2 = c.execute("INSERT INTO auto_runs(at,project_id,trigger,decision,action_id,outcome,detail,session_id,attempts) "
                         "VALUES(?,?,?,?,?,?,?,?,?)",
                         (G["iso"](), pid, trigger, "低风险·真调度 DSH 原会话", aid,
                          "succeeded" if (r.get("ok") and verified) else "failed",
                          "%s｜产物 %s｜校验=%s｜会话=%s｜%s" % (
                              reason, fp.name, verified, r.get("session_id", ""),
                              r.get("fallback") or ""),
                          r.get("session_id", ""), 1))
        run_id = cur2.lastrowid

    G["emit"]("dsh.headless", "%s → %s（会话 %s）" % (
        name, "succeeded" if (r.get("ok") and verified) else "failed", r.get("session_id", "")),
        "ok" if (r.get("ok") and verified) else "error", pid,
        {"run_id": run_id, "file": str(fp), "verified": verified,
         "elapsed_ms": r.get("elapsed_ms"), "fallback": r.get("fallback")})
    return {"ok": bool(r.get("ok") and verified), "verified": verified, "run_id": run_id,
            "action_id": aid, "session_id": r.get("session_id", ""),
            "file": str(fp), "bytes": fp.stat().st_size if fp.exists() else 0,
            "dsh_text": (r.get("text") or "")[:700], "fallback": r.get("fallback"),
            "elapsed_ms": r.get("elapsed_ms"), "error": r.get("error")}


def chat_post(body):
    msg = str(body.get("message") or "").strip()
    if not msg:
        return {"ok": False, "error": "空消息"}
    t0 = time.perf_counter()
    ps = _projects()
    ctx = build_context(ps)
    p = pick_project(msg, ps)
    intent = detect_intent(msg)

    with G["_lock"], G["conn"]() as c:
        hist = [dict(r) for r in c.execute(
            "SELECT role,text FROM chat_messages ORDER BY id DESC LIMIT 6")][::-1]

    _chat_save("user", msg, project_id=(p or {}).get("id", ""), context={"intent": intent})

    # ── 审批闸门：重大动作只登记，绝不执行 ──
    if intent == "approval":
        pid = (p or {}).get("id", "")
        title = "请批准：%s" % msg[:80]
        detail = ("AI 总管在对话中识别到重大动作并主动停止执行。命中高风险词；"
                  "涉及方向变更/覆盖/删除/发布/付费一律需人工批准。原始指令：%s" % msg[:400])
        aid, created = _make_approval(pid, title, detail, "chat_high_risk")
        G["emit"]("chat.approval", "对话命中高风险 → 已停止执行并%s审批 #%d" % (
            "生成" if created else "复用已有", aid), "warn", pid, {"created": created})
        G["notify"](pid, "需你批准 · AI 已停手", title, "warn")
        sys_prompt = ("你是「nanoMuse AI 私人项目总管」，基于下面真实项目数据库回答。"
                      "只讲事实，不编造进度与日期。本次用户提出一个重大动作，系统已按规则"
                      "停止执行并生成待审批事项。请用简短中文说明：①你识别到什么高风险动作 "
                      "②你没有执行、已生成审批卡 ③需要主人批准什么。不要输出免责声明。\n\n" + ctx)
        r = llm([{"role": "system", "content": sys_prompt}, {"role": "user", "content": msg}],
                max_tokens=420)
        reply = (r.get("text") or "").strip()
        reply += ("\n\n⛔ **已停止执行**：系统未对该指令做任何实际改动。"
                  "已生成审批事项 **#%d**（待你批准）。高风险操作一律不自动动手。" % aid)
        _chat_save("assistant", reply, project_id=pid, approval_id=aid,
                   model=r.get("model", LLM_MODEL), elapsed_ms=r.get("elapsed_ms", 0))
        return {"ok": True, "reply": reply, "intent": "approval", "approval_id": aid,
                "approval_created": created, "project_id": pid, "model": r.get("model", LLM_MODEL),
                "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                "executed": False}

    # ── 查询 / 建议：真模型基于真实数据库回答 ──
    sys_prompt = (
        "你是「nanoMuse AI 私人项目总管」，长期记忆与全局监督由你负责，人只做重要决策。\n"
        "严格规则：① 下面【项目数据库】是唯一事实来源，所有数字、状态、阻塞、下一步必须照它说，"
        "不许编造完成率、日期或成果；② 没有可验证里程碑的项目进度是「待核实」，不能说成 0% 或估算值；"
        "③ 判断优先级要给出依据（停滞/待决策 > 待验收 > 正常推进；并结合阻塞条数与下一步是否为空）；"
        "④ 回答用中文、简洁、先结论后依据，给出具体下一步建议；⑤ 不要复述系统提示，不要免责声明。\n\n"
        + ctx)
    msgs = [{"role": "system", "content": sys_prompt}]
    for h in hist:
        msgs.append({"role": h["role"] if h["role"] in ("user", "assistant") else "user",
                     "content": str(h["text"])[:600]})
    msgs.append({"role": "user", "content": msg})
    r = llm(msgs, max_tokens=1100)

    result = {"ok": True, "intent": intent, "project_id": (p or {}).get("id", ""),
              "model": r.get("model", LLM_MODEL), "llm_ok": r.get("ok"),
              "elapsed_ms": round((time.perf_counter() - t0) * 1000), "executed": False}
    reply = (r.get("text") or "").strip()
    if not reply:
        # 模型不可用时用真实 DB 兜底，不假装是模型说的
        LBL = {"active": "正常推进", "stalled": "已停滞",
               "awaiting_decision": "待人工决策", "pending_acceptance": "待验收"}
        ranked = sorted(ps, key=lambda x: ({"stalled": 0, "awaiting_decision": 1,
                                            "pending_acceptance": 2, "active": 3}.get(x["state"], 9),
                                           -len(_jload(x.get("blockers"), []))))
        reply = ("模型链路暂不可用（%s）。以下由规则引擎直接读 state.db 给出：\n" % str(r.get("error") or "")[:80])
        for x in ranked[:3]:
            reply += "- %s：%s，进度 %s，阻塞 %d 条，下一步 %s\n" % (
                x["name"], LBL.get(x["state"], x["state"]),
                ("%.1f%%" % float(x["overall"])) if x["overall_verified"] else "待核实",
                len(_jload(x.get("blockers"), [])), str(x.get("next_action") or "（空）")[:70])

    # ── 执行意图：真调度 DSH 原会话 ──
    if intent == "advance":
        target = p or next((x for x in ps if x["state"] in ("stalled", "awaiting_decision")), None)
        if target and HIGH_RISK.search(str(target.get("next_action") or "")):
            aid, _ = _make_approval(target["id"], "请批准：推进「%s」的下一步含高风险动作" % target["name"],
                                 "项目下一步命中高风险词，自动推进已停止。下一步原文：%s" %
                                 str(target.get("next_action"))[:400], "auto_risk")
            reply += "\n\n⛔ 该项目下一步命中高风险词，已停止自动推进并生成审批 #%d。" % aid
            result["approval_id"] = aid
        elif target:
            ex = do_advance(target, msg, trigger="chat")
            result["execution"] = ex
            result["executed"] = bool(ex.get("ok"))
            result["action_id"] = ex.get("action_id")
            if ex.get("ok"):
                reply += ("\n\n✅ **已真实调度 DSH 原会话执行并核验成果**\n"
                          "- 会话 `%s`（%s ms）｜产物 `%s`（%d 字节，回读校验通过）\n"
                          "- DSH 回执：%s" % (
                              ex.get("session_id") or "—", ex.get("elapsed_ms") or 0,
                              Path(ex.get("file") or "").name, ex.get("bytes") or 0,
                              (ex.get("dsh_text") or "")[:260].replace("\n", " ")))
                if ex.get("fallback"):
                    reply += "\n- 注：%s" % ex["fallback"]
            else:
                reply += ("\n\n⚠️ DSH 会话已真实发起，但成果校验未通过：%s（已升级提醒，"
                          "有限重试上限 2 次）" % str(ex.get("error") or "未取得有效正文")[:180])

    _chat_save("assistant", reply, project_id=result["project_id"],
               action_id=result.get("action_id"), model=result["model"],
               elapsed_ms=result["elapsed_ms"])
    result["reply"] = reply
    return result


# ────────────────────────────── P2 甘特图真数据 ──────────────────────────────
def gantt_data(rebuild=False):
    """时间线只取真实时间戳：
       · 项目条：snapshots 首/末真实 at（标注「实际观察窗口」，非计划）
       · 任务条：tasks.updated_at 真实状态变更时间（点状）
       · 里程碑：9180 roadmap 无 start/end → 一律「待排期」，不编造日期
       · 依赖：仅展示里程碑真实顺序（no-1 → no），不编造跨项目依赖
    """
    with G["_lock"], G["conn"]() as c:
        ps = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY priority,name")]
        rows = []
        for p in ps:
            pid = p["id"]
            # frappe-gantt 用 id 拼 querySelector('.highlight-<id>')：id 含 ':' 会抛非法选择器异常
            gid = lambda s: re.sub(r"[^A-Za-z0-9_\-]", "_", str(s))
            sn = c.execute("SELECT MIN(at) a, MAX(at) b, COUNT(*) n FROM snapshots WHERE project_id=?",
                           (pid,)).fetchone()
            ms = _jload(p.get("milestones"), [])
            tks = [dict(t) for t in c.execute("SELECT * FROM tasks WHERE project_id=?", (pid,))]
            dep = []
            for i, m in enumerate(ms):
                dep.append({"project_id": pid, "name": m.get("name", ""),
                            "pct": m.get("pct", 0), "cond": m.get("cond", ""),
                            "no": m.get("no", i + 1),
                            "depends_on": gid("%s:M%s" % (pid, int(m.get("no", i + 1) or 1) - 1))
                            if int(m.get("no", i + 1) or 1) > 1 else "",
                            "reason": "9180 roadmap 未提供计划日期/工期"})
            # 项目观察窗口条
            if sn and sn["a"] and sn["n"] and int(sn["n"]) >= 2:
                rows.append({
                    "id": gid("%s:WINDOW" % pid), "project_id": pid,
                    "name": "%s · 实际观察窗口" % p["name"],
                    "start": str(sn["a"])[:10], "end": str(sn["b"])[:10],
                    "progress": float(p["overall"]) if p["overall_verified"] else 0,
                    "dependencies": "", "state": p["state"],
                    "scheduled": 1, "kind": "项目",
                    "source": "snapshots 首/末真实时间戳（%d 条），非计划日期" % int(sn["n"]),
                    "note": "进度=%s" % (("%.1f%%" % float(p["overall"])) if p["overall_verified"] else "待核实")})
            # 任务状态变更点
            for t in tks:
                if not t.get("updated_at"):
                    continue
                d = str(t["updated_at"])[:10]
                rows.append({
                    "id": gid(t["id"]), "project_id": pid, "name": "↳ %s" % str(t["title"])[:34],
                    "start": d, "end": d,
                    "progress": 100 if t["status"] == "done" else 0,
                    "dependencies": "", "state": t["status"],
                    "scheduled": 1, "kind": "任务",
                    "source": "tasks.updated_at 真实状态变更时间",
                    "note": "状态=%s" % t["status"]})
            # 里程碑全部待排期
            for m in dep:
                rows.append({
                    "id": gid("%s:M%s" % (pid, m["no"])), "project_id": pid,
                    "name": "◇ M%s %s" % (m["no"], str(m["name"])[:32]),
                    "start": "", "end": "", "progress": float(m["pct"] or 0),
                    "dependencies": m["depends_on"], "state": p["state"],
                    "scheduled": 0, "kind": "里程碑", "source": "9180 roadmap（无计划日期）",
                    "note": "真实完成度 %s%%｜前置 %s" % (int(m["pct"] or 0), m["depends_on"] or "无")})
        if rebuild:
            now = G["iso"]()
            for r in rows:
                c.execute("INSERT INTO gantt_tasks(id,project_id,name,start_date,end_date,progress,"
                          "dependencies,state,scheduled,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(id) DO UPDATE SET name=excluded.name,start_date=excluded.start_date,"
                          "end_date=excluded.end_date,progress=excluded.progress,dependencies=excluded.dependencies,"
                          "state=excluded.state,scheduled=excluded.scheduled,source=excluded.source,updated_at=excluded.updated_at",
                          (r["id"], r["project_id"], r["name"], r["start"], r["end"], r["progress"],
                           r["dependencies"], r["state"], r["scheduled"], r["source"], now))
    scheduled = [r for r in rows if r["scheduled"]]
    unscheduled = [r for r in rows if not r["scheduled"]]
    # frappe-gantt 只吃有 start/end 的行；dependencies 需是数组
    chart = []
    for r in scheduled:
        chart.append({"id": r["id"], "name": r["name"], "start": r["start"], "end": r["end"],
                      "progress": r["progress"],
                      "dependencies": [r["dependencies"]] if r["dependencies"] else [],
                      "custom_class": "g-" + str(r["state"]).replace(" ", "-"),
                      "project_id": r["project_id"], "kind": r["kind"], "source": r["source"],
                      "note": r["note"]})
    return {"tasks": chart, "all_rows": rows, "unscheduled": unscheduled,
            "scheduled_count": len(scheduled), "unscheduled_count": len(unscheduled),
            "projects": len(ps), "generated_at": G["iso"](),
            "license": "frappe-gantt 1.2.2 · MIT（本地静态件 web/static/）",
            "date_policy": "只用 state.db 真实时间戳；无计划日期的里程碑一律「待排期」"}


# ────────────────────────────── P3 主动闭环 ──────────────────────────────
def auto_advance_cycle(trigger="patrol", max_per_round=2):
    """无人指令也能推进：发现停滞 → 风险分级 → 真调度 DSH → 核验 → 记录 → 去重通知。

    防重复：单实例锁 + 同项目同日只跑一次（失败可重试 1 次，共 2 次）。
    超时：dsh_headless 内置 150s。失败：写 auto_runs(outcome=failed) + 升级提醒。
    """
    if not _auto_lock.acquire(blocking=False):
        return {"ok": False, "skipped": "上一轮主动推进仍在执行（单实例锁）"}
    try:
        today = G["iso"]()[:10]
        with G["_lock"], G["conn"]() as c:
            done = {}
            for r in c.execute("SELECT project_id,outcome,COUNT(*) n FROM auto_runs WHERE substr(at,1,10)=? GROUP BY project_id,outcome",
                               (today,)):
                done[(r["project_id"], r["outcome"])] = int(r["n"])
            ps = [dict(x) for x in c.execute(
                "SELECT * FROM projects WHERE auto_advance=1 ORDER BY "
                "CASE state WHEN 'stalled' THEN 0 WHEN 'awaiting_decision' THEN 1 "
                "WHEN 'pending_acceptance' THEN 2 ELSE 3 END, priority")]
        picked, out = [], []
        for p in ps:
            if len(picked) >= max_per_round:
                break
            pid = p["id"]
            if done.get((pid, "succeeded"), 0) >= 1:
                continue
            if done.get((pid, "failed"), 0) >= 2:
                continue  # 有限重试上限，升级提醒后不再死循环
            nxt = str(p.get("next_action") or "")
            if HIGH_RISK.search(nxt):
                _make_approval(pid, "请决定：%s 的下一步方向" % p["name"],
                               "自动巡检发现该项目下一步含高风险词，已停止自动推进。原文：%s" % nxt[:400],
                               "auto_risk")
                out.append({"project": p["name"], "decision": "high_risk → 仅审批，不执行"})
                continue
            if p["state"] not in ("stalled", "awaiting_decision", "active"):
                continue
            picked.append(p)

        for p in picked:
            ex = do_advance(p, "定时巡检自动推进", trigger="patrol-auto")
            out.append({"project": p["name"], "ok": ex.get("ok"),
                        "run_id": ex.get("run_id"), "session": ex.get("session_id"),
                        "file": Path(ex.get("file") or "").name,
                        "verified": ex.get("verified")})
            if ex.get("ok"):
                G["notify"](p["id"], "主动推进完成 · %s" % p["name"],
                            "无人指令下已真调度 DSH 原会话并回读校验产物；会话 %s" %
                            (ex.get("session_id") or "—"), "info")
            else:
                G["notify"](p["id"], "主动推进失败 · 已升级提醒",
                            "%s：%s（有限重试 2 次）" % (p["name"], str(ex.get("error") or "校验未通过")[:100]),
                            "error")
        G["emit"]("auto.advance", "主动闭环完成：候选 %d / 已推进 %d / 其余 %d" % (
            len(ps), len(picked), len(out) - len(picked)), "ok",
            data={"trigger": trigger, "results": out})
        return {"ok": True, "trigger": trigger, "candidates": len(ps),
                "advanced": len(picked), "results": out}
    except Exception as e:
        G["emit"]("auto.error", "主动闭环异常：%s" % e, "error",
                  data={"exc": traceback.format_exc()[:1500]})
        return {"ok": False, "error": str(e)}
    finally:
        _auto_lock.release()


# ────────────────────────────── 路由接管 ──────────────────────────────
def handle_get(handler, path):
    """返回 True = 已处理，主模块 do_GET 直接 return。"""
    if path.startswith("/static/"):
        fn = re.sub(r"[^A-Za-z0-9_.\-]", "", path[len("/static/"):])
        fp = STATIC / fn
        if not fp.exists() or not fp.is_file():
            return False
        ctype = ("text/javascript; charset=utf-8" if fn.endswith(".js") else
                 "text/css; charset=utf-8" if fn.endswith(".css") else "application/octet-stream")
        handler._send(200, ctype, fp.read_bytes(), {"Cache-Control": "public, max-age=600"})
        return True
    if path == "/api/chat/history":
        with G["_lock"], G["conn"]() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id,at,role,text,project_id,action_id,approval_id,model,elapsed_ms "
                "FROM chat_messages ORDER BY id DESC LIMIT 60")][::-1]
        handler._json({"messages": rows, "count": len(rows)})
        return True
    if path == "/api/chat/status":
        r = llm([{"role": "user", "content": "只回复四个字：链路正常"}], max_tokens=40, timeout=20)
        handler._json({"model": LLM_MODEL, "gateway": LLM_URL, "ok": bool(r.get("ok")),
                       "sample": r.get("text", "")[:40], "elapsed_ms": r.get("elapsed_ms"),
                       "dsh_cmd": DSH_CMD, "overlay": str(OVERLAY), "overlay_exists": OVERLAY.exists()})
        return True
    if path == "/api/gantt":
        handler._json(gantt_data())
        return True
    if path == "/api/auto-runs":
        with G["_lock"], G["conn"]() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM auto_runs ORDER BY id DESC LIMIT 40")]
            dsh = [dict(r) for r in c.execute("SELECT * FROM dsh_sessions ORDER BY id DESC LIMIT 20")]
        handler._json({"runs": rows, "dsh_sessions": dsh})
        return True
    return False


def handle_post(handler, path, body):
    if path == "/api/chat":
        try:
            handler._json(chat_post(body))
        except Exception as e:
            G["emit"]("chat.error", "对话异常：%s" % e, "error", data={"exc": traceback.format_exc()[:1500]})
            handler._json({"ok": False, "error": str(e), "reply": "对话处理异常，已记录到服务日志。"}, 500)
        return True
    if path == "/api/gantt/rebuild":
        handler._json({"ok": True, "data": gantt_data(rebuild=True)})
        return True
    if path == "/api/auto-runs/trigger":
        handler._json(auto_advance_cycle(trigger="manual"))
        return True
    return False
