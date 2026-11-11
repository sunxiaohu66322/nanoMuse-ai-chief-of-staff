-- nanoMuse AI 私人总管 · 数据模型（从真实 state.db 导出，仅结构，不含任何运行数据）
-- 服务启动时若 data/state.db 不存在，会自动按本文件同级代码建表并重新发现项目。
-- 真源说明：本服务只读本机 9180 项目导航 / 9134 任务图谱 / DSH sessions / Host task-board，
--           再把「项目状态」这一层物化到下面这些表；不修改任何上游。

CREATE TABLE projects(
 id TEXT PRIMARY KEY, name TEXT, source TEXT, category TEXT,
 goal TEXT, overall REAL, overall_verified INTEGER DEFAULT 0,
 state TEXT, flags TEXT, blockers TEXT, next_action TEXT,
 pending_decisions TEXT, milestones TEXT, evidence TEXT,
 owner TEXT, priority TEXT, auto_advance INTEGER DEFAULT 1,
 last_change TEXT, last_patrol TEXT, created_at TEXT, meta TEXT,
 sessions TEXT, file_evidence TEXT, graph_nodes INTEGER DEFAULT 0);

CREATE TABLE snapshots(
 id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, at TEXT,
 overall REAL, revision TEXT, state TEXT, turn_delta INTEGER, file_mtime REAL,
 digest TEXT);

CREATE TABLE tasks(
 id TEXT PRIMARY KEY, project_id TEXT, title TEXT, status TEXT,
 note TEXT, updated_at TEXT);

CREATE TABLE actions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 kind TEXT, title TEXT, risk TEXT, status TEXT, attempts INTEGER DEFAULT 0,
 output TEXT, result TEXT, checked INTEGER DEFAULT 0);

CREATE TABLE approvals(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 kind TEXT, title TEXT, detail TEXT, status TEXT DEFAULT 'pending',
 decided_at TEXT, note TEXT);

CREATE TABLE events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, level TEXT, kind TEXT,
 project_id TEXT, message TEXT, data TEXT);

CREATE TABLE notifications(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 title TEXT, body TEXT, level TEXT, read INTEGER DEFAULT 0, via TEXT);

CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);

-- v2 新增（对话 / 甘特 / 主动闭环 / DSH 会话回执）
CREATE TABLE chat_messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, role TEXT, text TEXT,
 project_id TEXT, action_id INTEGER, approval_id INTEGER, model TEXT,
 elapsed_ms INTEGER, context_json TEXT);

CREATE TABLE gantt_tasks(
 id TEXT PRIMARY KEY, project_id TEXT, name TEXT, start_date TEXT, end_date TEXT,
 progress REAL, dependencies TEXT, state TEXT, scheduled INTEGER DEFAULT 0,
 source TEXT, updated_at TEXT);

CREATE TABLE auto_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT, trigger TEXT,
  decision TEXT, action_id INTEGER, outcome TEXT, detail TEXT, session_id TEXT,
  attempts INTEGER DEFAULT 1);

CREATE TABLE dsh_sessions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT, session_id TEXT,
 kind TEXT, task TEXT, outcome TEXT, final_text TEXT, elapsed_ms INTEGER);

-- v4 第二阶段新增（进度真实性 / 长期记忆 / 正式计划甘特 / 四态推进）
-- 真源仍是同一份 state.db；以下表只由 总管进度引擎.py 写入，不覆盖上游采集结果。
CREATE TABLE project_memory(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 kind TEXT, title TEXT, body TEXT, source TEXT, confidence TEXT, digest TEXT);
CREATE TABLE progress_evidence(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 milestone TEXT, condition TEXT, evidence_ref TEXT,
 level TEXT DEFAULT 'ai_candidate', confidence TEXT, source TEXT, digest TEXT, session_id TEXT);
CREATE TABLE next_action_inferred(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT,
 candidate TEXT, source TEXT, confidence TEXT, status TEXT DEFAULT 'inferred', digest TEXT);
CREATE TABLE project_plans(
 id TEXT PRIMARY KEY, project_id TEXT, name TEXT, start_date TEXT, end_date TEXT,
 dependencies TEXT, planned_progress REAL DEFAULT 0, status TEXT DEFAULT 'proposed',
 source TEXT, proposed_by TEXT, decided_at TEXT, updated_at TEXT);
CREATE TABLE advance_states(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, project_id TEXT, trigger TEXT,
 s_patrol INTEGER DEFAULT 0, s_review INTEGER DEFAULT 0,
 s_executed INTEGER DEFAULT 0, s_advanced INTEGER DEFAULT 0,
 outcome TEXT, reason TEXT, digest TEXT, session_id TEXT, file TEXT);
