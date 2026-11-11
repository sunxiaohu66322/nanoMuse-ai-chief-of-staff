# -*- coding: utf-8 -*-
"""nanoMuse 9196 · DSH 原生桥接（P0 接收端 / P1 会话索引 / P2 增量同步 / P3 原会话接续）

架构（与主模块一致）：install(ns) 注入 globals；handle_get / handle_post 接管新路由。
主模块 / 扩展 只各加几行转发，既有逻辑一字不改。

真源纪律：
  · 会话状态 = DSH 官方插件经 session/event + agent/status 主动推来的结构化增量；
    绝不每分钟读全部聊天记录，普通事件零模型调用。
  · 会话↔项目关联 = 优先用 projects.sessions 既有锚点，其次按标题/工作目录推断；
    推断一律 pending，不确定不强归类。
  · 进度依据 = 真实事件与产物；巡检日志/摘要不算推进（由 总管进度引擎.py 判四态）。
  · 原会话接续 = 走官方 ctx.agents.get(sid).followup()/steer()，经桥接控制面下发；
    找不到活跃 Agent 明确回报「不可恢复」，绝不静默新建 headless 冒充。

控制面：桥接插件在 127.0.0.1:9203 起 HTTP（/state /command /open）。
  9196 只把指令转发过去，不自己起 DSH 会话。
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

G = None
ROOT = Path(__file__).resolve().parent
PROJCACHE = Path(os.environ.get("DSH_HOME", r"I:\DSHHome")) / "storages" / "session_projcache" / "sessions"
CONTROL = "http://127.0.0.1:9203"          # 桥接插件控制面
DEEP_LINK_BASE = CONTROL + "/open?session="  # 打开原会话

# 高风险指令必须走人工审批（与 总管扩展.HIGH_RISK 同口径）
HIGH_RISK = re.compile(
    r"删除|删掉|移除|清空|覆盖|重写|发布|上线|部署|付费|支付|购买|重置|回滚|"
    r"drop|delete|push|deploy|publish|overwrite|reset|rm\s-|format", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dsh_session_index(
  session_id TEXT PRIMARY KEY,
  project_id TEXT DEFAULT '',
  link_status TEXT DEFAULT 'pending',      -- linked / pending / unlinked
  link_basis TEXT DEFAULT '',              -- sessions_anchor / title_inferred / none
  title TEXT DEFAULT '', cwd TEXT DEFAULT '', status TEXT DEFAULT '',
  last_event TEXT DEFAULT '', latest_summary TEXT DEFAULT '',
  deep_link TEXT DEFAULT '',
  first_seen TEXT, updated_at TEXT, source TEXT DEFAULT 'bridge');
CREATE TABLE IF NOT EXISTS dsh_bridge_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, session_id TEXT, event_seq INTEGER, event_type TEXT,
  turn INTEGER, status TEXT, payload TEXT, digest TEXT UNIQUE, project_id TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS dsh_bridge_commands(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, session_id TEXT, project_id TEXT, mode TEXT, text TEXT,
  risk TEXT DEFAULT 'low', status TEXT DEFAULT 'queued',
  receipt TEXT DEFAULT '', decided_at TEXT);
CREATE INDEX IF NOT EXISTS idx_be_sid ON dsh_bridge_events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_si_pid ON dsh_session_index(project_id);
"""


def install(ns):
    global G
    G = ns
    with G["_lock"], G["conn"]() as c:
        c.executescript(SCHEMA)
    G["emit"]("bridge.install", "DSH 原生桥接已挂载：event 去重 / 会话索引 / 指令队列 / 控制面转发", "ok")


# ────────────────────────────── 工具 ──────────────────────────────
def _iso():
    return G["iso"]()


def _jload(s, default):
    try:
        return json.loads(s or "")
    except Exception:
        return default


def _digest(*parts):
    return hashlib.md5("|".join(str(x) for x in parts).encode("utf-8")).hexdigest()


def _control(path, payload, timeout=8):
    """转发到桥接插件控制面。返回 (ok, data, http_status)。绝不在此新建 DSH 会话。"""
    req = urllib.request.Request(
        CONTROL + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8", "replace") or "{}"), r.status
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode("utf-8", "replace") or "{}"), e.code
        except Exception:
            return False, {"error": "HTTP %s" % e.code}, e.code
    except Exception as e:
        return False, {"error": "%s: %s" % (type(e).__name__, e)}, 0


# ────────────────────────────── P1 会话↔项目关联 ──────────────────────────────
def _project_anchors():
    """从 projects.sessions（既有真源）建 session 文件→project_id 锚点。"""
    anchors = {}
    with G["_lock"], G["conn"]() as c:
        rows = c.execute("SELECT id,name,sessions FROM projects").fetchall()
    for r in rows:
        pid, name = r["id"], r["name"] or ""
        for s in _jload(r["sessions"], []) or []:
            f = str(s.get("file", "")) if isinstance(s, dict) else ""
            sid = re.sub(r"^session-|\.json$", "", f)
            if sid:
                anchors[sid] = (pid, "sessions_anchor")
    return anchors, [dict(r) for r in rows]


def _norm(s):
    return re.sub(r"[\s\-_#🟥🟨🟩⬜◧◆★（）()\[\]{}.,/\\:：·|]+", "", str(s or "")).lower()


def classify(session_id, title="", cwd=""):
    """返回 (project_id, link_status, link_basis)。不确定 = 待确认，不强归类。"""
    anchors, projects = _project_anchors()
    sid_short = re.sub(r"^session-", "", str(session_id))
    if sid_short in anchors:
        pid, basis = anchors[sid_short]
        return pid, "linked", basis
    # 标题关键词匹配项目名：仅作低置信推断 → pending，需人工确认
    tn = _norm(title)
    best = ""
    for p in projects:
        pn = _norm(p.get("name", ""))
        if not pn or len(pn) < 3:
            continue
        # 项目名去掉"系统/中心/项目"等尾巴后的核心词命中标题即算候选
        core = re.sub(r"(系统|中心|项目|工作台|管理|平台|v?\d+\.?\d*)$", "", pn)
        if core and len(core) >= 2 and (core in tn or pn in tn):
            if len(core) > len(best):
                best = core
                hit = p["id"]
    if best:
        return hit, "pending", "title_inferred"
    return "", "pending", "none"


def upsert_session(session_id, **kw):
    """幂等 upsert 会话索引。只更新非空字段，不覆盖已有明确归属为待确认。"""
    if not session_id:
        return None
    with G["_lock"], G["conn"]() as c:
        row = c.execute("SELECT * FROM dsh_session_index WHERE session_id=?", (session_id,)).fetchone()
        deep = kw.get("deep_link") or (DEEP_LINK_BASE + session_id)
        if not row:
            pid, lstatus, basis = classify(session_id, kw.get("title", ""), kw.get("cwd", ""))
            c.execute(
                "INSERT INTO dsh_session_index(session_id,project_id,link_status,link_basis,title,cwd,status,"
                "last_event,latest_summary,deep_link,first_seen,updated_at,source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, pid, lstatus, basis, kw.get("title", ""), kw.get("cwd", ""),
                 kw.get("status", ""), kw.get("last_event", ""), kw.get("latest_summary", ""),
                 deep, _iso(), _iso(), kw.get("source", "bridge")))
        else:
            fields, vals = [], []
            for k in ("title", "cwd", "status", "last_event", "latest_summary"):
                if kw.get(k):
                    fields.append(k + "=?"); vals.append(kw[k])
            # 已有明确关联不被回退；未关联的每次都重判一次（可能有新锚点）
            if row["link_status"] != "linked":
                pid, lstatus, basis = classify(session_id, kw.get("title", "") or row["title"], kw.get("cwd", "") or row["cwd"])
                fields += ["project_id=?", "link_status=?", "link_basis=?"]
                vals += [pid, lstatus, basis]
            fields.append("updated_at=?"); vals.append(_iso())
            vals.append(session_id)
            c.execute("UPDATE dsh_session_index SET " + ",".join(fields) + " WHERE session_id=?", vals)
        return dict(c.execute("SELECT * FROM dsh_session_index WHERE session_id=?", (session_id,)).fetchone())


def reindex_projcache(incremental=True):
    """P1 首次/增量扫描：只读每个 projcache 文件的元数据与最后一条摘要，不读全部聊天历史。

    增量依据 = 文件 mtime > 上次扫描水位（kv），已处理过的文件不重复读。
    """
    if not PROJCACHE.is_dir():
        return {"ok": False, "error": "projcache 目录不存在", "path": str(PROJCACHE)}
    watermark = float(G["kv_get"]("bridge_scan_watermark", "0") or 0)
    files = glob.glob(str(PROJCACHE / "*.json"))
    scanned = new = linked = pending = 0
    max_mtime = watermark
    for f in files:
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        max_mtime = max(max_mtime, mt)
        if incremental and mt <= watermark:
            continue
        new += 1
        scanned += 1
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                j = json.load(fh)
            rec = j.get("record") or {}
            rows = rec.get("rows") or {}
            title = ((rows.get("title") or {}).get("val") or "")
            cwd = (rec.get("identity") or {}).get("cwd") or ""
            turns = (((rows.get("turnOutline") or {}).get("val") or {}).get("turns") or [])
            summary = ""
            if turns:
                last = turns[-1] or {}
                summary = str(last.get("response") or "")[:240].replace("\n", " ")
            sid = re.sub(r"^session-|\.json$", "", os.path.basename(f))
            r = upsert_session(sid, title=str(title)[:120], cwd=cwd, latest_summary=summary,
                               last_event="reindex", source="projcache")
            if r and r["link_status"] == "linked":
                linked += 1
            elif r:
                pending += 1
        except Exception:
            continue
    G["kv_set"]("bridge_scan_watermark", str(max_mtime))
    return {"ok": True, "files_total": len(files), "new_or_changed": new, "upserted": scanned,
            "linked": linked, "pending": pending, "incremental": incremental,
            "watermark": dt.datetime.fromtimestamp(max_mtime).isoformat(timespec="seconds") if max_mtime else ""}


# ────────────────────────────── P2 事件接收（去重 / 增量 / 零模型） ──────────────────────────────
def ingest_event(body):
    """DSH 桥接插件主动推来的单条结构化事件。普通事件不调用任何模型。"""
    sid = str(body.get("session_id", ""))
    etype = str(body.get("event_type", ""))
    seq = body.get("event_seq")
    if not sid or not etype:
        return {"ok": False, "error": "session_id/event_type 缺失"}
    digest = _digest(sid, seq, etype, body.get("turn", ""))
    with G["_lock"], G["conn"]() as c:
        dup = c.execute("SELECT id FROM dsh_bridge_events WHERE digest=?", (digest,)).fetchone()
        if dup:
            return {"ok": True, "duplicate": True, "id": dup["id"]}
        pid = str(body.get("project_id", "") or "")
        c.execute(
            "INSERT INTO dsh_bridge_events(at,session_id,event_seq,event_type,turn,status,payload,digest,project_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (_iso(), sid, seq if seq is not None else -1, etype, body.get("turn"),
             str(body.get("status", "")), json.dumps(body, ensure_ascii=False)[:4000], digest, pid))
        eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    # 更新会话索引（title/cwd/status/summary 均为结构化字段，无模型）
    upsert_session(sid, title=body.get("title", ""), cwd=body.get("cwd", ""),
                   status=str(body.get("status", "")), last_event=etype,
                   latest_summary=str(body.get("summary", "") or "")[:240], source="event")
    # 关键阶段才向工作台事件流记一笔（turn/end / approval / error），不做模型摘要
    if etype in ("turn/end", "approval/asked", "error", "status"):
        G["emit"]("bridge.event", "DSH 会话 %s · %s · %s" % (sid[:24], etype, body.get("status", "")),
                  "ok" if etype == "turn/end" else "warn", pid,
                  {"session": sid, "turn": body.get("turn"), "reason": body.get("reason")})
    return {"ok": True, "duplicate": False, "id": eid, "digest": digest}


# ────────────────────────────── P3 原会话定位 / 接续 ──────────────────────────────
def send_command(body):
    """从总管向原会话发指令。高风险先落审批；活跃会话走官方 followup/steer；
    已结束会话先验证恢复能力，不可恢复明确提示，绝不静默新建。"""
    sid = str(body.get("session_id", ""))
    text = str(body.get("text", ""))
    mode = str(body.get("mode", "followup"))
    if mode not in ("followup", "steer", "open"):
        mode = "followup"
    if not sid:
        return {"ok": False, "error": "session_id 缺失"}
    with G["_lock"], G["conn"]() as c:
        s = c.execute("SELECT * FROM dsh_session_index WHERE session_id=?", (sid,)).fetchone()
    if not s:
        return {"ok": False, "error": "该会话不在索引中，先做一次会话索引", "session_id": sid}
    pid = s["project_id"] or ""
    risk = "high" if (HIGH_RISK.search(text) and mode != "open") else "low"
    with G["_lock"], G["conn"]() as c:
        cur = c.execute(
            "INSERT INTO dsh_bridge_commands(at,session_id,project_id,mode,text,risk,status) "
            "VALUES(?,?,?,?,?,?,?)", (_iso(), sid, pid, mode, text[:1000], risk,
                                      "needs_approval" if risk == "high" else "queued"))
        cid = cur.lastrowid
    if risk == "high":
        # 大动作：落审批，明确停止执行
        with G["_lock"], G["conn"]() as c:
            c.execute("INSERT INTO approvals(at,project_id,kind,title,detail,status) VALUES(?,?,?,?,?,?)",
                      (_iso(), pid, "bridge_high_risk", "向原会话下发高风险指令",
                       "会话 %s · mode=%s · %s" % (sid, mode, text[:200]), "pending"))
        G["emit"]("bridge.approval", "高风险指令已拦截，等待人工批准：%s" % sid[:24], "warn", pid)
        return {"ok": True, "status": "needs_approval", "command_id": cid,
                "message": "高风险操作已停止执行，已生成审批事项。"}
    # 低风险：转发到桥接控制面；控制面找不到活跃 Agent 会明确回报不可恢复
    ok, data, http = _control("/command", {"session_id": sid, "mode": mode, "text": text}) if mode != "open" \
        else _control("/open", {"session_id": sid})
    # 控制面不可达 = 该会话所属运行面已结束或桌面尚未重启挂载插件。
    # 两种都属「当前不可恢复」，绝不静默新建 headless 冒充原会话接续。
    if not ok and http == 0:
        data = {"recoverable": False,
                "reason": "桥接控制面未运行：该会话已结束，或桌面 DSH 尚未重启加载桥接插件。"
                          "P3 纪律要求不静默新建替代会话；请在桌面打开原会话后再下发。"}
        status = "unrecoverable"
    else:
        status = "sent" if ok and data.get("ok") else ("unrecoverable" if data.get("recoverable") is False else "failed")
    receipt = json.dumps({"http": http, "data": data}, ensure_ascii=False)[:2000]
    with G["_lock"], G["conn"]() as c:
        c.execute("UPDATE dsh_bridge_commands SET status=?,receipt=?,decided_at=? WHERE id=?",
                  (status, receipt, _iso(), cid))
    G["emit"]("bridge.command", "指令 %s → %s · %s" % (mode, sid[:24], status),
              "ok" if status == "sent" else "warn", pid, {"command_id": cid})
    return {"ok": True, "status": status, "command_id": cid, "receipt": data,
            "message": ("已下发到原会话（official %s）" % mode) if status == "sent" else
                       ("该会话当前不可恢复：%s" % data.get("reason", data.get("error", ""))
                        if status == "unrecoverable" else "下发失败：%s" % data.get("error", ""))}


# ────────────────────────────── 查询 ──────────────────────────────
def sessions_view(project_id=""):
    with G["_lock"], G["conn"]() as c:
        if project_id:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM dsh_session_index WHERE project_id=? ORDER BY updated_at DESC", (project_id,))]
        else:
            rows = [dict(r) for r in c.execute("SELECT * FROM dsh_session_index ORDER BY updated_at DESC LIMIT 400")]
        projects = [dict(r) for r in c.execute("SELECT id,name FROM projects")]
        ev = c.execute("SELECT COUNT(*) FROM dsh_bridge_events").fetchone()[0]
        last = c.execute("SELECT at,session_id,event_type FROM dsh_bridge_events ORDER BY id DESC LIMIT 1").fetchone()
        cmds = [dict(r) for r in c.execute("SELECT * FROM dsh_bridge_commands ORDER BY id DESC LIMIT 20")]
    by_proj = {}
    for r in rows:
        by_proj.setdefault(r["project_id"] or "__pending__", []).append(r)
    return {"sessions": rows, "by_project": by_proj, "projects": projects,
            "events_total": ev, "last_event": dict(last) if last else None,
            "commands": cmds, "control": CONTROL,
            "counts": {"total": len(rows),
                       "linked": sum(1 for r in rows if r["link_status"] == "linked"),
                       "pending": sum(1 for r in rows if r["link_status"] == "pending")}}


def bridge_status():
    ok, data, http = _control("/state", {}, timeout=4)
    with G["_lock"], G["conn"]() as c:
        ev = c.execute("SELECT COUNT(*) FROM dsh_bridge_events").fetchone()[0]
        si = c.execute("SELECT COUNT(*) FROM dsh_session_index").fetchone()[0]
        cmds = c.execute("SELECT COUNT(*) FROM dsh_bridge_commands").fetchone()[0]
    return {"ok": True, "control_reachable": ok, "control_http": http, "control_state": data,
            "events_total": ev, "sessions_indexed": si, "commands_total": cmds,
            "projcache": str(PROJCACHE), "projcache_exists": PROJCACHE.is_dir(),
            "watermark": G["kv_get"]("bridge_scan_watermark", ""),
            "last_reindex": _jload(G["kv_get"]("bridge_last_reindex", ""), None)}


# ────────────────────────────── 路由 ──────────────────────────────
def handle_get(handler, path, raw_path=""):
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(raw_path or path).query)
    if path == "/api/bridge/sessions":
        handler._json(sessions_view((q.get("project") or [""])[0])); return True
    if path == "/api/bridge/status":
        handler._json(bridge_status()); return True
    return False


def handle_post(handler, path, body):
    if path == "/api/dsh-bridge/event":
        # 插件可一次推一批（{events:[...]}）或单条
        evs = body.get("events") if isinstance(body.get("events"), list) else [body]
        out = [ingest_event(e) for e in evs if isinstance(e, dict)]
        handler._json({"ok": True, "received": len(out),
                       "inserted": sum(1 for r in out if not r.get("duplicate") and r.get("ok")),
                       "duplicates": sum(1 for r in out if r.get("duplicate"))}); return True
    if path == "/api/bridge/command":
        handler._json(send_command(body)); return True
    if path == "/api/bridge/reindex":
        # 832 份 projcache 全量元数据解析会超过浏览器 60s 超时；
        # 后台线程跑，立刻回执，结果落到 kv 由 /api/bridge/status 读。
        import threading
        inc = bool(body.get("incremental", True))

        def _bg():
            try:
                r = reindex_projcache(incremental=inc)
                G["kv_set"]("bridge_last_reindex", json.dumps(r, ensure_ascii=False)[:1500])
                G["emit"]("bridge.reindex", "会话索引完成：新/变 %s，关联 %s，待确认 %s" %
                          (r.get("new_or_changed"), r.get("linked"), r.get("pending")), "ok")
            except Exception as e:
                G["kv_set"]("bridge_last_reindex", json.dumps({"ok": False, "error": str(e)}))
        threading.Thread(target=_bg, daemon=True, name="bridge-reindex").start()
        handler._json({"ok": True, "status": "running",
                       "message": "后台增量扫描已启动（只读元数据，不读聊天历史），用 /api/bridge/status 查结果",
                       "last_result": _jload(G["kv_get"]("bridge_last_reindex", ""), None)})
        return True
    if path == "/api/bridge/link":
        # 人工确认关联（pending → linked），AI 不擅自转正
        sid = str(body.get("session_id", "")); pid = str(body.get("project_id", ""))
        with G["_lock"], G["conn"]() as c:
            c.execute("UPDATE dsh_session_index SET project_id=?,link_status=?,link_basis=?,updated_at=? WHERE session_id=?",
                      (pid, "linked" if pid else "unlinked", "human_confirmed", _iso(), sid))
        handler._json({"ok": True, "session_id": sid, "project_id": pid}); return True
    return False
