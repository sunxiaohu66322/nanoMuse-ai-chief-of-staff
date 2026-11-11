# CHANGELOG

> 从最新一条往下读 = 完整改动史。每版 = 远端历史之上 1 个新 commit + 1 个 annotated tag。
> 基线 commit 用 `git rev-parse <tag>` 查；`raw.githubusercontent.com` 刚推完几分钟内可能返回上一版，
> 实时权威以 `gh api repos/sunxiaohu66322/nanoMuse-ai-chief-of-staff/contents/CHANGELOG.md` 或仓库文件页 / tags 页为准。

---

## v4 · 2026-11-11 20:05 +08:00 · 「全融合主动助理」定稿 + 第二阶段 6 项

**基线**：`0a3d6ef`（v3 末次提交）｜**tag**：`v4`｜可见性：**PUBLIC**
**说明**：把 v4 第二阶段真实成果、9196×9134 合并评估、以及「nanoMuse × 个人助理全融合」的
完整说明一次性补齐；公开版六件套 + 版本记录 + 自包含兜底包落地。不重建仓库、不删历史、不 force push。

| 类别 | 变更 |
|:--|:--|
| 新增代码 | `总管进度引擎.py`（656 行：四态真推进 / 自动推断 / 正式计划甘特 / 长期记忆 / 四级证据） |
| 改代码 | `总管扩展.py`（install 挂引擎 / `do_advance` 转发 / `decorate_context` / 路由转发 / 四态文案 / 去重）；`web/index.html`（+「◧ 进度与计划」页签、正式计划甘特、四态/证据/推断/记忆视图；修 `/api/health`→`/health` 一行真缺陷）；`data/schema.sql`（+5 张表） |
| 新增报告 | `v4-第二阶段交付报告.md`（四态 + 真浏览器 10/10 + 三轮失败过程）；`9196与9134合并可行性评估-给第三方AI.md`（396 行，档 A/B/C + 12 风险 + 8 评审问题） |
| 新增文档 | `FUSION-全融合说明.md` · `00_读我_AI-FIRST.md` · `01_架构.md` · `02_现状与边界.md` · `03_怎么改.md` · `CHANGELOG.md` · `VERSIONS.md` · `AI-FIRST-自包含包.md` · `给第三方AI的开场话术.txt` |
| 改文档 | `README.md`（v4 头部 + 融合入口）· `llms.txt`（版本记录入口 + v4 口径）· `交付与恢复手册.md`（v4 头） |
| 验收证据 | `验收/v4-真实浏览器回归.mjs` · `v4-浏览器验收结论.md/.json` · `v4-浏览器验收日志.json` · 4 份后端自检 JSON · 8 张 `v4-01..08` PNG |
| 密钥终检 | 纳入集合正则扫描 `gh[pousr]_* / github_pat_* / sk- / AKIA / -----BEGIN / password= / token= / Bearer` → **0 命中** |
| 排除 | `data/state.db*` · `__pycache__` · `_backup*` / `_备份*` · `*.log` · 临时诊断与被进程占用的运行态文件 · 运行期 `自动复核-*` / `结构化证据-*` · 验收中间轮次冗余 PNG |

**四态硬判据**：`patrol / review / executed` 一律不算项目推进；只有 `advanced`
（结构化证据合规 + 项目状态层真实变化 + 回读确认）才算。收敛实测第 3 轮 `advanced=0`。

**如实保留的未完成**：开机自启未挂（服务停过 2h+，20:01 手动恢复）· Super Agent 融合度 0（5 项前置未批准）
· 9134 档 A 投影未实施 · 两套系统都无计划 start/end 日期 · DSH 后台回执未逐笔复验。

---

## v3 · 2026-11-11 03:36 +08:00 · 可视化驾驶舱（历史 commit `8e9cae6`）

首页重做为面向非技术老板的驾驶舱（AI 每日简报 / KPI + SVG 状态环 / 9 张项目卡片 /
显眼 AI 对话 / 技术日志折叠）；修复公网 `/nanomuse-ai/api/state` 泄露 134KB 内部数据；
Super Agent 隔离评估 BLOCKED；真浏览器 10/10，54 张 PNG + 5 轮过程（3 个真缺陷修复）。

## v2 · 2026-11-11 01:40 +08:00 · 主动型项目总管（`f1c99a1`）

真 AI 对话（持久化 + 结构化读库）、真 DSH headless 调度与回读、frappe-gantt 1.2.2 甘特、
60s 巡检 + 主动闭环、六模块统一工作台；真浏览器 8/8，10 张 PNG + 三轮过程。

## v1 · 2026-11-10 · 监控后台基线

SQLite 项目自动发现 / 9 项目 / 巡检骨架 / 真浏览器 7/7。v1 逻辑在 v2–v4 中一字未删。
