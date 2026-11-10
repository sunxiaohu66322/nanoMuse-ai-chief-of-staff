# -*- coding: utf-8 -*-
"""nanoMuse 主动型 AI 项目总管 · 主服务（stdlib only）

设计依据（P0 核验结论）：
  nanoMuse 1.0.9 Tiller / GPL-3.0-or-later，官方 harness/dsh-nanomuse bundle 硬绑
  dsh 0.2.0-rc.2 + Node>=22。本机 dsh 核心包正是 0.2.0-rc.2、Node 25.2.1，
  版本可载；但该 bundle 的 cordis.patch.yml 会覆盖 system-prompt / agent-preset /
  ui-sidebar / ui-brand-official / ui-settings-general —— 与本机 40+ bundle 定制冲突。
  故「集成 nanoMuse」= 复用其主动能力契约（goals / routines / rooms / feed / nudges /
  sentinel 三档审批 / 关掉 app 也继续跑），落在 DSH 现有真源之上，不改 DSH 核心文件。

复用的现有设施（不重造）：
  9180 项目导航 /api/public/projects + {status,roadmap,history,digest}
  9134 任务图谱 /api/graph
  DSH 会话真源 $DSH_HOME/storages/session_projcache/sessions/*.json
  Host task-board ledger-v2.json
  日志中心/logkit.py（双轨日志）
  项目门牌/plate.py（llms.txt + well-known）
  dsh-notify-center 同款 Windows Toast
  ego lite（验收浏览器）
  9195 日志面板（iframe 嵌入）

端口 9196。持久化：data/state.db（SQLite，重启恢复）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
WEB = ROOT / "web"
DATA.mkdir(exist_ok=True)
DB = DATA / "state.db"
PORT = int(os.environ.get("NM_PORT", "9196"))
PATROL_SECONDS = int(os.environ.get("NM_PATROL_SECONDS", "60"))
STALE_HOURS = float(os.environ.get("NM_STALE_HOURS", "72"))
DSH_HOME = Path(os.environ.get("DSH_HOME", r"I:\DSHHome"))
WORKSPACE = Path(r"G:\Users\Administrator\Documents\deepseek-harness\default-workspace")
SESSIONS = DSH_HOME / "storages" / "session_projcache" / "sessions"
NAV = "http://127.0.0.1:9180/api/public/projects"
GRAPH = "http://127.0.0.1:9134/api/graph"
BOARD = DSH_HOME / "task-board" / "ledger-v2.json"

# 双轨日志（铁律 20）
sys.path.insert(0, str(WORKSPACE / "日志中心"))
from logkit import Log  # noqa: E402

log = Log("nanoMuse项目总管")
log.install_excepthook()

# 门牌（端口门牌协议）
try:
    sys.path.insert(0, str(WORKSPACE / "项目门牌"))
    import plate
    plate.mount(PORT)
except Exception as e:
    log.warn("门牌挂载失败（不影响服务）", data={"err": str(e)})

NOWIN = 0x08000000 if os.name == "nt" else 0
_lock = threading.RLock()

# 交互升级扩展（P1 真 AI 对话 / P2 甘特 / P3 主动闭环）——独立模块，主文件只做插桩
sys.path.insert(0, str(ROOT))
import 总管扩展 as ext  # noqa: E402


def _auto_bg():
    """定时巡检完成后在后台线程跑主动闭环，不阻塞 60s 巡检节奏。"""
    try:
        ext.auto_advance_cycle(trigger="patrol")
    except Exception as e:
        emit("auto.error", f"后台主动闭环异常：{e}", "error")


# ────────────────────────────── 持久化 ──────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY, name TEXT, source TEXT, category TEXT,
 goal TEXT, overall REAL, overall_verified INTEGER DEFAULT 0,
 state TEXT, flags TEXT, blockers TEXT, next_action TEXT,
 pending_decisions TEXT, milestones TEXT, evidence TEXT,
 owner TEXT, priority TEXT, auto_advance INTEGER DEFAULT 1,
 last_change TEXT, last_patrol TEXT, created_at TEXT, meta TEXT,
 sessions TEXT, file_evidence TEXT, graph_nodes INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS snapshots(
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, at TEXT,
 overall REAL, revision TEXT, state TEXT, turn_delta INTEGER, file_mtime REAL,
 digest TEXT);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, level TEXT, kind TEXT,
 project_id TEXT, message TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 title TEXT, body TEXT, level TEXT, read INTEGER DEFAULT 0, via TEXT);
CREATE TABLE IF NOT EXISTS approvals(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 kind TEXT, title TEXT, detail TEXT, status TEXT DEFAULT 'pending',
 decided_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS actions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 kind TEXT, title TEXT, risk TEXT, status TEXT, attempts INTEGER DEFAULT 0,
 output TEXT, result TEXT, checked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, project_id TEXT, title TEXT, status TEXT,
 note TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""


def conn():
    c = sqlite3.connect(str(DB), timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        # 老库兼容（幂等加列，不覆盖数据）
        cols = {r["name"] for r in c.execute("PRAGMA table_info(projects)")}
        for col, ddl in (("sessions", "TEXT"), ("file_evidence", "TEXT"), ("graph_nodes", "INTEGER DEFAULT 0")):
            if col not in cols:
                c.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")


def kv_get(k, default=""):
    with _lock, conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(k, v):
    with _lock, conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def emit(kind, message, level="info", project_id="", data=None):
    now = iso()
    with _lock, conn() as c:
        c.execute("INSERT INTO events(at,level,kind,project_id,message,data) VALUES(?,?,?,?,?,?)",
                  (now, level, kind, project_id, message, json.dumps(data or {}, ensure_ascii=False)))
    getattr(log, level if level in ("info", "warn", "error", "ok", "debug") else "info")(
        f"[{kind}] {message}", event=kind, data={"project": project_id})


def iso(t=None):
    return (t or dt.datetime.now().astimezone()).isoformat(timespec="seconds")


def jdump(x):
    return json.dumps(x, ensure_ascii=False, default=str)


# ────────────────────────────── 采集层 ──────────────────────────────
def http_json(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log.debug(f"取数失败 {url}", data={"err": str(e)[:160]})
        return None


def nav_projects():
    d = http_json(NAV)
    return (d or {}).get("projects", [])


def nav_detail(pid, ep):
    return http_json(f"{NAV}/{pid}/{ep}") or {}


def graph_nodes():
    d = http_json(GRAPH, timeout=6)
    return (d or {}).get("nodes", [])


def board_tasks():
    try:
        d = json.loads(BOARD.read_text(encoding="utf-8"))
        return d.get("tasks", [])
    except Exception:
        return []


def session_index():
    """只读增量：按 (path,size,mtime) 变化判断，避免反复解析 777 份全量历史。"""
    seen = {}
    try:
        seen = json.loads(kv_get("sessions_seen", "{}") or "{}")
    except Exception:
        pass
    out, changed = [], 0
    try:
        files = sorted(SESSIONS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:220]
    except Exception:
        return [], 0, seen
    for f in files:
        try:
            st = f.stat()
            key = f.name
            sig = f"{st.st_size}:{int(st.st_mtime)}"
            rec = seen.get(key)
            if rec and rec.get("sig") == sig:
                out.append(rec["meta"])
                continue
            changed += 1
            j = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            rows = (j.get("record") or {}).get("rows") or {}
            turns = ((rows.get("turnOutline") or {}).get("val") or {}).get("turns") or []
            title = ((rows.get("title") or {}).get("val") or "")
            stats = ((rows.get("sessionStats") or {}).get("val") or {})
            todos = ((rows.get("todos") or {}).get("val") or [])
            meta = {
                "file": f.name,
                "path": str(f),
                "title": str(title)[:160],
                "norm": normalize_title(str(title)),
                "turns": len(turns),
                "mtime": st.st_mtime,
                "mtime_iso": iso(dt.datetime.fromtimestamp(st.st_mtime).astimezone()),
                "last_prompt": str((turns[-1] or {}).get("prompt", ""))[:300] if turns else "",
                "last_response": str((turns[-1] or {}).get("response", ""))[:400] if turns else "",
                "pending_todos": sum(1 for t in todos if isinstance(t, dict) and t.get("status") not in ("completed", "done")),
                "stats_turns": stats.get("turns"),
            }
            seen[key] = {"sig": sig, "meta": meta}
            out.append(meta)
        except Exception as e:
            log.debug("会话解析跳过", data={"file": f.name, "err": str(e)[:120]})
    kv_set("sessions_seen", jdump(seen))
    return out, changed, seen


EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]")
PREFIX = re.compile(r"^[\s\-]*([A-Z]{1,5}-?[A-Z]?\d{0,3}|[A-Z]{1,4}\d{1,3})[\-\s:：]+")


def normalize_title(t):
    t = EMOJI.sub("", t or "").strip()
    t = re.sub(r"^#+\s*", "", t)
    t = re.sub(r"^=+\s*", "", t)
    t = PREFIX.sub("", t).strip()
    t = re.sub(r"^(DSH|工程指令|智能体自动调度|任务)[｜|:：\s]+", "", t).strip()
    return re.sub(r"\s+", " ", t)[:120]


KEYWORDS = {
    "p-007c76a0": ["上货台", "微信小店", "抖店模板", "商品精筛"],
    "p-b0e5dcc0": ["项目导航", "门牌", "公网发布", "导航系统"],
    "p-0a83efab": ["抖音知识库", "抖音收藏", "收藏夹", "stt", "转写"],
    "p-821a9073": ["换脸", "人脸资产", "预检接入"],
}
# 额外自动发现的会话簇（非 9180 正式项目，靠会话标题自动归类，无需人工重命名）
CLUSTERS = [
    ("上货台7.0 AI运营中心", ["上货台7.0", "ai运营中心", "8.0", "AI 运营中心"]),
    ("WriterX / InkOS 写作系统", ["writerx", "inkos", "合并书库", "拆书车间", "写书"]),
    ("拼多多自动采集", ["拼多多", "pddsrc", "自动采集"]),
    ("模型与显卡管理", ["模型与显卡", "显卡", "ollama", "llama.cpp"]),
    ("视频 LoRa 与插画台", ["lora", "插画取图", "视频生成"]),
]


def classify_sessions(sessions, pid, name):
    words = [w.lower() for w in (KEYWORDS.get(pid, []) + [name, name.replace("系统", "")])]
    hit = []
    for s in sessions:
        blob = (s["norm"] + " " + s["last_prompt"][:80]).lower()
        if any(w.lower() in blob for w in words if len(w) > 1):
            hit.append(s)
    return hit


def dir_evidence(name, pid):
    """项目目录的文件证据：最新 mtime + 文件数（只读 stat，不读内容）。"""
    cands = [WORKSPACE / name]
    for p in WORKSPACE.iterdir() if WORKSPACE.exists() else []:
        if p.is_dir() and name[:4] and name[:4] in p.name:
            cands.append(p)
    best = {"path": "", "mtime": 0.0, "mtime_iso": "", "files": 0}
    for d in dict.fromkeys(cands):
        try:
            if not d.exists():
                continue
            mx, n = 0.0, 0
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        n += 1
                        mx = max(mx, f.stat().st_mtime)
                except Exception:
                    pass
                if n > 4000:
                    break
            if mx > best["mtime"]:
                best = {"path": str(d), "mtime": mx, "mtime_iso": iso(dt.datetime.fromtimestamp(mx).astimezone()), "files": n}
        except Exception:
            pass
    return best


# ────────────────────────────── 状态识别 ──────────────────────────────
def score_milestones(roadmap):
    stages = roadmap.get("stages", []) or []
    if not stages:
        return None, []
    tw = sum(float(s.get("weight", 1) or 1) for s in stages) or 1.0
    pct = sum(float(s.get("pct", 0) or 0) * float(s.get("weight", 1) or 1) for s in stages) / tw
    ms = [{"no": s.get("no", i + 1), "name": s.get("name", ""), "pct": float(s.get("pct", 0) or 0),
           "cond": s.get("cond", ""), "weight": float(s.get("weight", 1) or 1)}
          for i, s in enumerate(stages)]
    return round(pct, 2), ms


def decide_state(p, sess, fev, prev, nodes):
    """多维证据判状态。会话 mtime 只是其中一路，不是唯一判据。

    停滞识别两条独立链路（用户 P1 硬要求：不是只看会话最后更新时间）：
      A 静态里程碑冻结：9180 history 的 overall 序列长期不变 + revision 停滞 + freshness.stale
      B 动态增量冻结：本项目自身 snapshots 连续两轮进度不变，且会话轮次/文件 mtime 均无增量
    """
    now = time.time()
    status = p.get("status") or {}
    roadmap = p.get("roadmap") or {}
    digest = p.get("digest") or ""
    history = (p.get("history") or {}).get("history", []) or []
    blockers = status.get("blockers") or []
    decisions = status.get("pending_decisions") or []
    next_action = str(status.get("next_action") or "").strip()
    overall, ms = score_milestones(roadmap)
    rev = str(status.get("revision") or roadmap.get("revision") or "")

    ev = []
    flags = []

    # 1a 里程碑冻结（动态：本项目自己的连续快照）
    frozen = False
    if prev and overall is not None:
        try:
            if abs(float(prev["overall"] or 0) - overall) < 0.01 and (now - dt.datetime.fromisoformat(prev["at"]).timestamp()) > 3600:
                frozen = True
        except Exception:
            pass

    # 1b 里程碑冻结（静态：9180 真实历史序列，首轮就能判，不依赖本系统先跑两轮）
    hist_frozen = False
    hist_days = 0.0
    try:
        ov = [float(h.get("overall", 0) or 0) for h in history]
        if len(ov) >= 2 and max(ov) - min(ov) < 0.01:
            last = max(dt.datetime.fromisoformat(h["at"].replace("Z", "+00:00")) for h in history if h.get("at"))
            hist_days = (dt.datetime.now().astimezone() - last.astimezone()).total_seconds() / 86400
            hist_frozen = hist_days > 3
    except Exception:
        pass
    if hist_frozen:
        ev.append(f"里程碑历史 {len(history)} 次修订进度恒为 {overall}%，已 {hist_days:.1f} 天无变化")
        flags.append("milestone_frozen_history")
    if frozen:
        ev.append("本项目连续快照加权进度未变")

    # 1c 导航官方自报陈旧（带真实小时数）
    freshness = status.get("freshness") or {}
    if isinstance(freshness, dict) and freshness.get("stale"):
        ev.append(f"导航 official freshness.stale=True（{freshness.get('age_hours','?')}h / {freshness.get('stale_reason','')}）")
        flags.append("nav_official_stale")

    # 2 会话增量（turn 数变化，而非单纯 mtime）
    s_turns = sum(int(s.get("turns", 0)) for s in sess)
    prev_turns = int((json.loads(prev["digest"] or "{}").get("turns", 0)) if prev else 0)
    session_delta = s_turns - prev_turns if prev else 0
    if sess and max(s["mtime"] for s in sess) > now - 24 * 3600:
        ev.append(f"关联会话 {len(sess)} 个，最近 24h 内有活动")
    if session_delta > 0:
        ev.append(f"会话轮次新增 {session_delta} 轮")

    # 3 文件证据增量
    fm = float(fev.get("mtime", 0))
    prev_fm = float(prev["file_mtime"] or 0) if prev else 0
    file_delta = fm > prev_fm + 1 if prev else False
    if fm and fm > now - 24 * 3600:
        ev.append(f"项目文件 24h 内更新（{fev.get('mtime_iso','')[:16]}）")
    if file_delta:
        ev.append("项目文件证据有新增")

    # 4 阻塞 / 待决策
    if decisions:
        flags.append("awaiting_decision")
        ev.append(f"待人工决策 {len(decisions)} 条")
    for b in blockers:
        txt = b if isinstance(b, str) else b.get("what", "")
        if re.search(r"需主人|待批准|待确认|未确认|需要主人|需用户|请主人", str(txt)):
            flags.append("awaiting_decision")
            ev.append(f"阻塞项待人工：{str(txt)[:48]}")
    if blockers:
        ev.append(f"阻塞 {len(blockers)} 条")

    # 5 导航自报陈旧（只作旁证）
    if "数据陈旧" in digest:
        m = re.search(r"已\s*([\d.]+)\s*小时未更新", digest)
        if m:
            ev.append(f"导航自报 {m.group(1)}h 未更新")
        flags.append("nav_stale")

    # 6 next_action 空
    if not next_action:
        ev.append("下一步任务为空")

    # 7 图谱关联节点
    rel = [n for n in nodes if p.get("name", "")[:4] and p["name"][:4] in str(n.get("title", ""))]
    if rel:
        ev.append(f"任务图谱关联节点 {len(rel)} 个")

    # 8 近期真实增量（只有「轮次/文件真实新增」算活动；裸 mtime 不算）
    # 裸 mtime 会被系统写元数据触发，不等于项目有进展 —— 用户明确要求不能只看会话最后更新时间
    sess_recent = bool(sess) and max(s["mtime"] for s in sess) > now - 24 * 3600
    files_recent = bool(fm and fm > now - 24 * 3600)
    has_increment = (session_delta > 0 or file_delta)
    if has_increment:
        ev.append("会话轮次或项目文件有真实增量（非仅 mtime）")
        flags.append("real_increment")
    elif sess_recent or files_recent:
        ev.append("会话/文件 mtime 24h 内被写过（旁证，非进度证据）")

    is_nav = bool((roadmap.get("stages") or []))  # 9180 正式项目才有可验证里程碑
    # 结构性停滞：里程碑长期冻结 + 导航官方陈旧 + 无下一步 / 进度为 0
    structural_stall = bool(
        hist_frozen
        and ("nav_official_stale" in flags or not next_action)
        and (not next_action or (overall is not None and overall < 1))
        and not has_increment
    )
    if structural_stall:
        ev.append("结构化判据：里程碑冻结 + 官方陈旧 + 无有效推进 → 已停滞（非仅靠会话时间）")

    # ── 主状态裁决 ──
    if "awaiting_decision" in flags:
        state = "awaiting_decision"
    elif structural_stall or (frozen and not has_increment and not (is_nav is False and (sess_recent or files_recent))):
        state = "stalled"
        flags.append("stalled_by_milestone")
    elif (has_increment or (not is_nav and (sess_recent or files_recent))) and next_action:
        state = "active"
    elif overall is not None and overall >= 95:
        state = "pending_acceptance"
        flags.append("near_done_unverified")
    elif not sess and not fev.get("path") and not next_action:
        state = "stalled"
        flags.append("no_evidence_at_all")
    elif is_nav and not has_increment:
        # 正式项目无真实增量：不臆断为 active
        state = "pending_acceptance"
    else:
        state = "active"

    if state == "stalled":
        # 硬门槛：≥2 条独立证据，且至少一条是「里程碑/进度」类（非时间类）
        non_time = [e for e in ev if not (e.startswith("导航自报") or e.startswith("关联会话") or e.startswith("项目文件") or e.startswith("会话/文件 mtime"))]
        milestone = any(("里程碑" in e or "freshness" in e or "快照" in e or "下一步" in e or "阻塞" in e or "待人工" in e or "结构化判据" in e) for e in non_time)
        if len(non_time) < 2 or not milestone:
            state = "pending_acceptance"
            flags = [f for f in flags if f != "stalled_by_milestone"]

    digest_obj = {"turns": s_turns, "sessions": len(sess), "file_mtime": fm,
                  "revision": rev, "blockers": len(blockers), "decisions": len(decisions)}
    return {
        "state": state, "flags": sorted(set(flags)), "evidence": ev,
        "overall": overall, "overall_verified": 1 if overall is not None else 0,
        "milestones": ms, "blockers": blockers, "next_action": next_action,
        "pending_decisions": decisions, "revision": rev,
        "sessions": [{"file": s["file"], "title": s["norm"], "turns": s["turns"],
                      "mtime_iso": s["mtime_iso"], "last_response": s["last_response"][:180]} for s in sess[:8]],
        "file_evidence": fev, "graph_nodes": len(rel),
        "turn_delta": session_delta, "file_delta": file_delta,
        "digest_obj": digest_obj,
    }


def upsert_project(pid, name, source, decided, status, roadmap, digest):
    now = iso()
    with _lock, conn() as c:
        old = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        prev_snap = c.execute("SELECT * FROM snapshots WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        # 分类（自动，无需人工重命名）
        cat = "正式项目" if source == "nav9180" else "会话簇（自动归类）"
        goal = str((digest.split("## 最终目标", 1)[-1].split("##")[0].strip()[:700]) if "## 最终目标" in digest else "")
        if not goal:
            goal = str(status.get("next_action") or "自动发现，目标待核实")
        prio = "P0" if decided["state"] in ("stalled", "awaiting_decision") else (
            "P1" if decided["state"] == "pending_acceptance" else "P2")
        vals = (name, source, cat, goal, decided["overall"], decided["overall_verified"],
                decided["state"], jdump(decided["flags"]), jdump(decided["blockers"]),
                decided["next_action"], jdump(decided["pending_decisions"]),
                jdump(decided["milestones"]), jdump(decided["evidence"]),
                "AI 项目总管", prio,
                1 if old is None else int(old["auto_advance"]),
                now if (old is None or old["state"] != decided["state"]) else old["last_change"],
                now, old["created_at"] if old else now,
                jdump({"revision": decided["revision"], "status": status, "digest_head": digest[:400]}),
                jdump(decided["sessions"]), jdump(decided["file_evidence"]), decided["graph_nodes"])
        if old is None:
            c.execute("""INSERT INTO projects(id,name,source,category,goal,overall,overall_verified,
                state,flags,blockers,next_action,pending_decisions,milestones,evidence,owner,priority,
                auto_advance,last_change,last_patrol,created_at,meta,sessions,file_evidence,graph_nodes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (pid,) + vals)
        else:
            c.execute("""UPDATE projects SET name=?,source=?,category=?,goal=?,overall=?,overall_verified=?,
                state=?,flags=?,blockers=?,next_action=?,pending_decisions=?,milestones=?,evidence=?,
                owner=?,priority=?,auto_advance=?,last_change=?,last_patrol=?,created_at=?,meta=?,
                sessions=?,file_evidence=?,graph_nodes=? WHERE id=?""",
                      vals + (pid,))
        c.execute("""INSERT INTO snapshots(project_id,at,overall,revision,state,turn_delta,file_mtime,digest)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (pid, now, decided["overall"] or 0, decided["revision"], decided["state"],
                   decided["turn_delta"], decided["file_evidence"].get("mtime", 0), jdump(decided["digest_obj"])))
        # 预置可操作任务（供界面真实状态变更验收）
        for tid, title in ((f"{pid}:t1", "核对里程碑证据与进度口径"),
                           (f"{pid}:t2", "复核阻塞项与待决策事项"),
                           (f"{pid}:t3", "核实下一步任务是否可执行")):
            c.execute("INSERT OR IGNORE INTO tasks(id,project_id,title,status,note,updated_at) VALUES(?,?,?,?,?,?)",
                      (tid, pid, title, "pending", "", now))
        # 状态变化 => 事件 + 通知
        if old and old["state"] != decided["state"]:
            emit("state.changed", f"{name}: {old['state']} → {decided['state']}", "warn", pid,
                 {"evidence": decided["evidence"]})
            notify(pid, f"状态变化 · {name}", f"{old['state']} → {decided['state']}｜{decided['next_action'][:70]}", "warn")
        return old["state"] if old else None, prev_snap


def notify(pid, title, body, level="info"):
    now = iso()
    with _lock, conn() as c:
        # 限频：同项目同标题 30 分钟内不重复（通知表仍记）
        recent = c.execute("SELECT at FROM notifications WHERE project_id=? AND title=? ORDER BY id DESC LIMIT 1",
                           (pid, title)).fetchone()
        fresh = True
        if recent:
            try:
                fresh = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(recent["at"])).total_seconds() > 1800
            except Exception:
                pass
        c.execute("INSERT INTO notifications(at,project_id,title,body,level,via) VALUES(?,?,?,?,?,?)",
                  (now, pid, title, body, level, "console+toast" if fresh else "console"))
    log.info(f"[通知] {title} · {body[:80]}", event="notify", data={"project": pid})
    if fresh:
        toast(title, body)


def toast(title, body):
    """Windows 主动通知：NotifyIcon 气泡；spawn 带 CREATE_NO_WINDOW（铁律 19）。"""
    if os.name != "nt":
        return
    safe_t = str(title).replace("'", "''").replace("\n", " ")[:120]
    safe_b = str(body).replace("'", "''").replace("\n", " ")[:200]
    ps = ("Add-Type -AssemblyName System.Windows.Forms; "
          "$n=New-Object System.Windows.Forms.NotifyIcon; "
          "$n.Icon=[System.Drawing.SystemIcons]::Information; "
          "$n.BalloonTipTitle='" + safe_t + "'; $n.BalloonTipText='" + safe_b + "'; "
          "$n.Visible=$true; $n.ShowBalloonTip(8000); Start-Sleep -Seconds 6; "
          "$n.Dispose()")
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         creationflags=NOWIN, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warn("Toast 派发失败", data={"err": str(e)[:120]})


# ────────────────────────────── 巡检 ──────────────────────────────
def patrol(manual=False):
    t0 = time.perf_counter()
    tag = "手动巡检" if manual else "定时自动巡检"
    with log.step(1, 4, tag):
        pass
    emit("patrol.start", f"{tag}开始", "info", data={"manual": manual})
    sessions, changed, _ = session_index()
    nodes = graph_nodes()
    projects = nav_projects()
    results = []
    with log.step(2, 4, "采集真源并识别状态"):
        for p in projects:
            pid, name = p.get("id"), p.get("name", "未命名")
            status = nav_detail(pid, "status")
            roadmap = nav_detail(pid, "roadmap")
            history = nav_detail(pid, "history")
            d = nav_detail(pid, "digest")
            digest = d.get("digest", "") if isinstance(d, dict) else str(d or "")
            sess = classify_sessions(sessions, pid, name)
            fev = dir_evidence(name, pid)
            with _lock, conn() as c:
                prev = c.execute("SELECT * FROM snapshots WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
            decided = decide_state(
                {"status": status, "roadmap": roadmap, "history": history,
                 "digest": digest, "name": name},
                sess, fev, prev, nodes)
            old_state, _ = upsert_project(pid, name, "nav9180", decided, status, roadmap, digest)
            results.append({"id": pid, "name": name, "state": decided["state"],
                            "old_state": old_state, "overall": decided["overall"],
                            "evidence_count": len(decided["evidence"])})

        # 自动发现会话簇（不依赖人工重命名）
        for cname, kws in CLUSTERS:
            hit = [s for s in sessions if any(k in (s["norm"] + " " + s["title"]).lower() for k in kws)]
            if not hit:
                continue
            pid = "cluster-" + hashlib.md5(cname.encode()).hexdigest()[:8]
            fev = dir_evidence(cname, pid)
            with _lock, conn() as c:
                prev = c.execute("SELECT * FROM snapshots WHERE project_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
            fake_status = {
                "status": {"next_action": (hit[0]["last_response"][:180] if hit else ""),
                           "blockers": [], "pending_decisions": [], "revision": ""},
                "roadmap": {"stages": []}, "digest": "", "name": cname,
            }
            decided = decide_state(fake_status, hit, fev, prev, nodes)
            old_state, _ = upsert_project(pid, cname, "session-cluster", decided, {}, {}, "")
            results.append({"id": pid, "name": cname, "state": decided["state"],
                            "old_state": old_state, "overall": decided["overall"],
                            "evidence_count": len(decided["evidence"])})

    with log.step(3, 4, "停滞/待决策主动提醒"):
        with _lock, conn() as c:
            rows = c.execute("SELECT id,name,state,next_action,priority FROM projects").fetchall()
        for r in rows:
            if r["state"] in ("stalled", "awaiting_decision"):
                notify(r["id"], f"{'停滞' if r['state']=='stalled' else '待你拍板'} · {r['name']}",
                       r["next_action"][:140] or "无下一步任务，需人工介入", "warn")
            # 待审批闸门：重大动作只登记，不自动执行
            if r["state"] == "awaiting_decision":
                with _lock, conn() as c:
                    ex = c.execute("SELECT id FROM approvals WHERE project_id=? AND status='pending'", (r["id"],)).fetchone()
                    if not ex:
                        c.execute("INSERT INTO approvals(at,project_id,kind,title,detail,status) VALUES(?,?,?,?,?,?)",
                                  (iso(), r["id"], "direction_change",
                                   f"请决定：{r['name']} 的下一步方向",
                                   "项目存在待人工决策事项；方向变更/覆盖/删除/发布/付费一律需批准，系统不自动动手。",
                                   "pending"))

    with log.step(4, 4, "写入巡检心跳"):
        kv_set("last_patrol", iso())
        kv_set("last_patrol_ms", str(round((time.perf_counter() - t0) * 1000, 1)))
        emit("patrol.done", f"{tag}完成：{len(results)} 项目 / 会话变化 {changed} 份",
             "ok", data={"results": results, "manual": manual, "ms": round((time.perf_counter() - t0) * 1000, 1)})
        notify("", f"项目总管 · {tag}完成", f"{len(results)} 个项目已复核，停滞/待决策已主动提醒", "info")
    # P3 主动闭环：巡检后异步跑「发现停滞→风险分级→真调度 DSH→核验→提醒」
    # daemon 线程 + 扩展内单实例锁，绝不拖慢 60s 巡检，也不重复执行。
    if not manual:
        threading.Thread(target=_auto_bg, daemon=True, name="auto-advance").start()
    return {"at": iso(), "manual": manual, "projects": results,
            "sessions_changed": changed, "ms": round((time.perf_counter() - t0) * 1000, 1)}


def patrol_loop():
    while True:
        time.sleep(PATROL_SECONDS)
        try:
            patrol(manual=False)
        except Exception as e:
            emit("patrol.error", f"定时巡检异常：{e}", "error", data={"exc": traceback.format_exc()[:2000]})
            log.error(f"定时巡检异常 {e}", event="patrol.error", exc=traceback.format_exc())


# ────────────────────────────── 低风险主动推进 ──────────────────────────────
RISK_DENY = re.compile(r"删除|覆盖|发布|上线|付费|支付|direction|覆盖生产|drop|delete|push|deploy")


def execute_action(action_id, approve=False):
    """低风险动作真执行 + 真检查产物；重大动作必须 approve=True 且单独批准。"""
    with _lock, conn() as c:
        a = c.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if not a:
            return {"ok": False, "error": "action not found"}
        if a["checked"] and a["status"] == "succeeded":
            return {"ok": True, "cached": True, "output": a["output"]}
        if a["attempts"] >= 2:
            return {"ok": False, "error": "超过有限重试上限（2 次），已升级提醒"}
        c.execute("UPDATE actions SET attempts=attempts+1,status='running' WHERE id=?", (action_id,))
    kind, pid = a["kind"], a["project_id"]
    if RISK_DENY.search(kind + " " + (a["title"] or "")) and not approve:
        # 永不自动动手：改为待批准
        with _lock, conn() as c:
            c.execute("UPDATE actions SET status='approval_required' WHERE id=?", (action_id,))
            c.execute("INSERT INTO approvals(at,project_id,kind,title,detail,status) VALUES(?,?,?,?,?,?)",
                      (iso(), pid, "high_risk", a["title"], "命中重大动作关键词，需人工批准后才可执行", "pending"))
        emit("action.blocked", f"重大动作已拦截待批准：{a['title']}", "warn", pid)
        notify(pid, "需你批准", a["title"], "warn")
        return {"ok": False, "error": "approval_required", "status": "approval_required"}

    out = ""
    ok = False
    try:
        if kind == "generate_patrol_brief":
            with _lock, conn() as c:
                p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
                evs = c.execute("SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT 12", (pid,)).fetchall()
            lines = [f"# 巡检摘要 · {p['name']}", "", f"生成时间：{iso()}",
                     f"状态：{p['state']}｜进度：{p['overall'] if p['overall_verified'] else '待核实'}",
                     f"下一步：{p['next_action'] or '（空）'}", "", "## 证据",
                     *[f"- {e}" for e in json.loads(p["evidence"] or "[]")],
                     "", "## 最近事件",
                     *[f"- {e['at']} [{e['kind']}] {e['message']}" for e in evs]]
            ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            fp = DATA / f"巡检摘要-{pid}-{ts}.md"
            fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # 真检查产物
            ok = fp.exists() and fp.stat().st_size > 80
            out = f"{fp} ({fp.stat().st_size if fp.exists() else 0} bytes)"
        elif kind == "dsh_session_handoff":
            # 生成 DSH 接续卡（由 Host task-board 真跑，回填回执）
            with _lock, conn() as c:
                p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            card = {"project_id": pid, "project": p["name"], "at": iso(),
                    "prompt": f"只读复核项目「{p['name']}」的当前状态与下一步；输出三行：已完成/阻塞/下一步。不得修改任何生产文件。",
                    "risk": "low", "status": "awaiting_dsh"}
            fp = DATA / f"dsh-outbox-{action_id}.json"
            fp.write_text(jdump(card), encoding="utf-8")
            ok = fp.exists() and json.loads(fp.read_text(encoding="utf-8"))["status"] == "awaiting_dsh"
            out = str(fp)
        else:
            out, ok = f"未知动作类型 {kind}", False
    except Exception as e:
        out, ok = f"{e}\n{traceback.format_exc()[:800]}", False
    status = "succeeded" if ok else "failed"
    with _lock, conn() as c:
        c.execute("UPDATE actions SET status=?,output=?,result=?,checked=? WHERE id=?",
                  (status, out, "产物已回读校验" if ok else "产物校验失败", 1 if ok else 0, action_id))
    emit("action.done" if ok else "action.fail", f"{a['title']} → {status}", "ok" if ok else "error", pid,
         {"output": out[:300]})
    if not ok:
        notify(pid, "动作失败 · 已升级提醒", f"{a['title']}：{out[:100]}", "error")
    return {"ok": ok, "status": status, "output": out, "checked": bool(ok)}


def ensure_actions():
    with _lock, conn() as c:
        ps = c.execute("SELECT id,name FROM projects").fetchall()
        for p in ps:
            if not c.execute("SELECT id FROM actions WHERE project_id=? AND kind='generate_patrol_brief'", (p["id"],)).fetchone():
                c.execute("INSERT INTO actions(at,project_id,kind,title,risk,status) VALUES(?,?,?,?,?,?)",
                          (iso(), p["id"], "generate_patrol_brief", f"生成「{p['name']}」巡检摘要（只读+写隔离目录）", "low", "queued"))
            if not c.execute("SELECT id FROM actions WHERE project_id=? AND kind='dsh_session_handoff'", (p["id"],)).fetchone():
                c.execute("INSERT INTO actions(at,project_id,kind,title,risk,status) VALUES(?,?,?,?,?,?)",
                          (iso(), p["id"], "dsh_session_handoff", f"DSH 会话接续：只读复核「{p['name']}」", "low", "queued"))


# ────────────────────────────── HTTP ──────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, ctype, body: bytes, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", jdump(obj).encode("utf-8"))

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _html(self):
        f = WEB / "index.html"
        return f.read_bytes() if f.exists() else b"<h1>index.html missing</h1>"

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if ext.handle_get(self, path):   # 新增：chat 历史 / chat 状态 / gantt / auto-runs / static
                return
            if path in ("/", "/index.html"):
                return self._send(200, "text/html; charset=utf-8", self._html())
            if path == "/health":
                return self._json({"ok": True, "service": "nanoMuse 主动型 AI 项目总管",
                                   "port": PORT, "patrol_seconds": PATROL_SECONDS,
                                   "last_patrol": kv_get("last_patrol"), "db": str(DB),
                                   "persisted": DB.exists(), "time": iso()})
            if path == "/api/state":
                with _lock, conn() as c:
                    ps = [dict(r) for r in c.execute("SELECT * FROM projects ORDER BY priority, name")]
                    for p in ps:
                        for k in ("flags", "blockers", "pending_decisions", "milestones", "evidence", "meta", "sessions", "file_evidence"):
                            try:
                                p[k] = json.loads(p[k] or "[]")
                            except Exception:
                                pass
                        p["tasks"] = [dict(t) for t in c.execute("SELECT * FROM tasks WHERE project_id=?", (p["id"],))]
                        p["snapshots"] = [dict(s) for s in c.execute(
                            "SELECT at,overall,state,turn_delta FROM snapshots WHERE project_id=? ORDER BY id DESC LIMIT 12", (p["id"],))]
                    evs = [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80")]
                    nts = [dict(r) for r in c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 30")]
                    aps = [dict(r) for r in c.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT 30")]
                    acs = [dict(r) for r in c.execute("SELECT * FROM actions ORDER BY id DESC LIMIT 40")]
                counts = {}
                for p in ps:
                    counts[p["state"]] = counts.get(p["state"], 0) + 1
                return self._json({"projects": ps, "events": evs, "notifications": nts,
                                   "approvals": aps, "actions": acs, "counts": counts,
                                   "last_patrol": kv_get("last_patrol"),
                                   "last_patrol_ms": kv_get("last_patrol_ms"),
                                   "patrol_seconds": PATROL_SECONDS,
                                   "server_time": iso(), "revision": kv_get("revision", "1")})
            if path == "/api/projects":
                with _lock, conn() as c:
                    return self._json([dict(r) for r in c.execute("SELECT id,name,state,overall,overall_verified,priority,category FROM projects")])
            if path.startswith("/api/projects/"):
                pid = path.rsplit("/", 1)[-1]
                with _lock, conn() as c:
                    p = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
                    if not p:
                        return self._json({"error": "not found"}, 404)
                    d = dict(p)
                    d["history"] = [dict(s) for s in c.execute("SELECT * FROM snapshots WHERE project_id=? ORDER BY id DESC LIMIT 40", (pid,))]
                    d["tasks"] = [dict(t) for t in c.execute("SELECT * FROM tasks WHERE project_id=?", (pid,))]
                    d["events"] = [dict(e) for e in c.execute("SELECT * FROM events WHERE project_id=? ORDER BY id DESC LIMIT 40", (pid,))]
                return self._json(d)
            if path == "/api/events":
                with _lock, conn() as c:
                    return self._json([dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 200")])
            if path == "/api/log/status":
                return self._json({"ok": True, "project": "nanoMuse项目总管", "viewer": "http://127.0.0.1:9195/?embed=1&project=nanoMuse%E9%A1%B9%E7%9B%AE%E6%80%BB%E7%AE%A1"})
            if path == "/api/log/tail":
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                n = int((q.get("n") or ["40"])[0])
                sys.path.insert(0, str(WORKSPACE / "日志中心"))
                from logkit import tail
                return self._json({"lines": tail("nanoMuse项目总管", n)})
            return self._json({"error": "not found", "path": path}, 404)
        except Exception as e:
            emit("http.error", f"GET {path}: {e}", "error", data={"exc": traceback.format_exc()[:1200]})
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        try:
            if ext.handle_post(self, path, body):   # 新增：/api/chat、gantt rebuild、auto-runs trigger
                return
            if path == "/api/patrol":
                return self._json({"ok": True, "result": patrol(manual=True)})
            if path == "/api/patrol-interval":
                global PATROL_SECONDS
                v = max(20, min(3600, int(body.get("seconds", PATROL_SECONDS))))
                PATROL_SECONDS = v
                emit("config.changed", f"自动巡检间隔改为 {v}s", "warn")
                return self._json({"ok": True, "patrol_seconds": v})
            m = re.match(r"^/api/projects/([^/]+)/(pause|resume)$", path)
            if m:
                pid, act = m.group(1), m.group(2)
                with _lock, conn() as c:
                    c.execute("UPDATE projects SET auto_advance=? WHERE id=?", (1 if act == "resume" else 0, pid))
                emit("project." + act, f"{pid} 自动推进 {act}", "warn", pid)
                return self._json({"ok": True, "id": pid, "auto_advance": 1 if act == "resume" else 0})
            m = re.match(r"^/api/tasks/([^/]+)/status$", path)
            if m:
                tid = m.group(1)
                st = str(body.get("status", "done"))
                with _lock, conn() as c:
                    c.execute("UPDATE tasks SET status=?,note=?,updated_at=? WHERE id=?",
                              (st, str(body.get("note", "")), iso(), tid))
                    row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                if not row:
                    return self._json({"error": "task not found"}, 404)
                emit("task.status", f"{row['title']} → {st}", "ok", row["project_id"])
                return self._json({"ok": True, "task": dict(row)})
            m = re.match(r"^/api/actions/(\d+)/run$", path)
            if m:
                with _lock, conn() as c:
                    p = c.execute("SELECT id,auto_advance FROM projects WHERE id=(SELECT project_id FROM actions WHERE id=?)", (int(m.group(1)),)).fetchone()
                if p and not p["auto_advance"]:
                    return self._json({"ok": False, "error": "该项目已暂停自动推进"}, 409)
                return self._json(execute_action(int(m.group(1))))
            m = re.match(r"^/api/approvals/(\d+)$", path)
            if m:
                aid = int(m.group(1))
                dec = "approved" if body.get("decision") == "approve" else "denied"
                with _lock, conn() as c:
                    c.execute("UPDATE approvals SET status=?,decided_at=?,note=? WHERE id=?",
                              (dec, iso(), str(body.get("note", "")), aid))
                    r = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
                emit("approval.decided", f"{r['title']} → {dec}", "warn", r["project_id"])
                return self._json({"ok": True, "approval": dict(r)})
            if path == "/api/dsh-receipt":
                # Host task-board 真跑完 DSH 会话后的回执回填
                aid = int(body.get("action_id", 0))
                with _lock, conn() as c:
                    c.execute("UPDATE actions SET status=?,result=?,output=?,checked=? WHERE id=?",
                              ("succeeded" if body.get("outcome") == "succeeded" else "failed",
                               str(body.get("outcome", "")), str(body.get("evidence", "")),
                               1 if body.get("verified") else 0, aid))
                emit("dsh.receipt", f"DSH 会话回执 #{aid}: {body.get('outcome')}", "ok",
                     str(body.get("project_id", "")), {"session": body.get("session_id"), "evidence": body.get("evidence")})
                return self._json({"ok": True, "action_id": aid})
            if path == "/api/log/page":
                emit("page." + str(body.get("event", "view")), str(body.get("message", "页面事件")), "info")
                return self._json({"ok": True})
            return self._json({"error": "not found", "path": path}, 404)
        except Exception as e:
            emit("http.error", f"POST {path}: {e}", "error", data={"exc": traceback.format_exc()[:1200]})
            return self._json({"error": str(e)}, 500)


def main():
    init_db()
    ext.install(globals())          # 新增：幂等建 chat/gantt/auto_runs/dsh_sessions 表，不碰旧表
    emit("service.start", f"总管启动 port={PORT} 巡检={PATROL_SECONDS}s 数据库={DB}", "ok")
    if not kv_get("bootstrapped"):
        log.info("首次启动：执行基线发现")
        patrol(manual=True)
        ensure_actions()
        kv_set("bootstrapped", "1")
    else:
        log.info("数据库已存在，直接恢复跟踪（不重跑基线）")
        emit("service.restore", "重启后从持久化状态恢复跟踪", "ok")
        ensure_actions()
    threading.Thread(target=patrol_loop, daemon=True, name="patrol").start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.ok(f"总控台就绪 http://127.0.0.1:{PORT}/")
    print(f"READY http://127.0.0.1:{PORT}/  patrol={PATROL_SECONDS}s", flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
