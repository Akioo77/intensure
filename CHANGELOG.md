# CHANGELOG — 网络意图 SDN 采集模块

> 历史变更记录。所有变更都有对应 git commit 与测试验证。
> **最新版本在最上面**(倒序)

---


---


## v3.10.2 — 2026-10-25 状态复查 + 修 3 个问题

**触发**: 16 天没动（上次提交 10-09），主人问进度 → 全量复查 git / 文档 / 环境，发现问题

### 🐛 Bug 修复

- **`controller_app.py` · DEAD 事件空指针崩溃**（demo 实测暴露）
  `state_change_handler` 里 `format(datapath.id, '016x')`，但 DEAD_DISPATCHER 状态下
  `datapath.id` 可能是 `None` → `TypeError: unsupported format string passed to NoneType`,
  整个 handler 挂掉。已加 `getattr(datapath, 'id', None)` 守卫 + 提前 return。
  —— 这个 bug 在仓库里从 v3.0 起就存在，是 16 天没跑才第一次真正触发。
- **`assurance_bridge.py` · 补 `ASSURANCE_BASE_FALLBACK`**
  P3-5 原计划有 fallback 备用地址（cloudflared 换域名时不掉连），实际没实现。
  现在优先级：显式参数 > `ASSURANCE_BASE` > `ASSURANCE_BASE_FALLBACK` > 空(no-op)。

### 📝 文档更新

- **`docs/OPTIMIZATION.md`**：v0.1 → v0.2。P0-1 / P0-5 / P1-4 / P3-5 实际早已完成但没记账，
  现在移入「✅ 已完成」段并补提交号；待办数 20 → 16，计数表同步更新。
- **`README.md`**：补「联调环境变量」表 —— `ASSURANCE_URL`（live_env/reporter 读）
  vs `ASSURANCE_BASE`（assurance_bridge 读）vs `ASSURANCE_BASE_FALLBACK` 三者关系与用途。

### 📊 统计

- 4 文件改动（2 代码 + 2 文档）
- 修 2 个真 bug + 补 4 项记账 + 补 1 段环境变量文档

---

## v3.10.3 - 2026-10-25 伙伴 assurance_schema.sql 落地准备 + 2 个 bug 修复

**触发**: 伙伴(A 同学)回传数据库对接文档的反馈，要求在 PG 实例上执行他的 `assurance_schema.sql`

### 🐛 Bug 修复

- **`controller_app.py` · DEAD 事件空指针崩溃**（demo 实测暴露）
  `state_change_handler` 里 `format(datapath.id, '016x')`，但 DEAD_DISPATCHER 状态下
  `datapath.id` 可能是 `None` → `TypeError: unsupported format string passed to NoneType`，
  整个 handler 挂掉。已加 `getattr(datapath, 'id', None)` 守卫 + 提前 return。
  —— 这个 bug 在仓库里从 v3.0 起就存在，是 16 天没跑才第一次真正触发。
- **`assurance_bridge.py` · 补 `ASSURANCE_BASE_FALLBACK`**
  P3-5 原计划有 fallback 备用地址（cloudflared 换域名时不掉连），实际没实现。
  现在优先级：显式参数 > `ASSURANCE_BASE` > `ASSURANCE_BASE_FALLBACK` > 空(no-op)。

### 🔒 新增：伙伴 schema 落地工具

- **`demo_scripts/apply_partner_schema.py`** —— 安全落地伙伴的 `assurance_schema.sql`：
  - 三道闸：危险语句拦截 / 重写后校验 / 冲突未确认拒绝落地
  - dry-run 在独立 schema + 独立事务里跑，结束必删，真库零影响
  - 自动输出冲突表清单 + **列级差异**（谁多哪些列、谁缺哪些列）
  - 已用模拟脚本实测：9 新表 + 2 硬冲突，检测准确

### 📝 文档

- **`docs/partner_schema_review.md`**（新建）——给伙伴的回复：
  密码确认、2 个硬冲突（`healing_intents` / `healing_history`）、4 组语义重叠、3 个待确认问题
- **`docs/OPTIMIZATION.md`**：v0.1 → v0.2。P0-1 / P0-5 / P1-4 / P3-5 实际早已完成但没记账，
  现在移入「✅ 已完成」段并补提交号；待办数 20 → 16，计数表同步更新。
- **`README.md`**：补「联调环境变量」表 —— `ASSURANCE_URL`（live_env/reporter 读）
  vs `ASSURANCE_BASE`（assurance_bridge 读）vs `ASSURANCE_BASE_FALLBACK` 三者关系与用途。

### 📊 统计

- 6 文件改动（2 代码 + 1 工具 + 3 文档）
- 修 2 个真 bug + 建 1 个安全落地工具 + 补 4 项记账 + 写 1 份对伙伴的回复

---

## v3.11.2 - 2026-10-26 P1-5 一键启动脚本 + 按伙伴反馈补齐 healing 字段

### ✨ 新增（P1-5）

3 个脚本，均已在 VM 实测通过：
- **start_all.sh** —— 一键起 4 组件（tmux 窗口 controller/api/mininet/assurance，
  --with-live 加 live_env）。起完自动校验端口与 /api/health。
- **stop_all.sh** —— 一键优雅停止：先 SIGTERM（走 graceful.py 落盘/关DB/停拓扑），
  8 秒未退则强杀；同时清 tmux 与 Mininet，最后复查残留。
- **status.sh** —— 5 段体检：进程 / tmux 窗口 / 端口+健康 / 13 张表行数 / 汇总。

实测：start_all 起 4 组件全绿；status 全绿 exit 0；stop_all 三进程全部优雅退出、
tmux 与 Mininet 清理干净、DB 数据保留。

### 🤝 按伙伴反馈补字段（契约对齐）

**冲突1** —— 采用我方 healing_intents 结构，补齐伙伴要求的 4 个字段：
- status TEXT NOT NULL DEFAULT 'pending' + CHECK 约束（pending/approved/rejected/executed/failed）
- priority INTEGER NOT NULL DEFAULT 100（队列排序，越小越先）
- affected_devices TEXT[] NOT NULL DEFAULT '{}'（Guard G3 范围检查）
- expires_at TIMESTAMPTZ（写入时不传则自动 now()+ttl_seconds）
- 另补 2 索引：队列扫描 (status,priority,created_at) + 过期清扫 (expires_at)
- 其余 strategy_spec / guard_passed / has_conflict 放 metadata JSONB
- 语义区别：verdict 是 Guard 判定，status 是信封生命周期，两者独立

**冲突2** —— 职责分离不合并：伙伴写 diagnosis_results（含 CoT），我方写
consistency_checks；生产路径我方不写 diagnoses（表保留，仅本地 mock 用）。

**healing_history** —— 分区保留，补伙伴需要的 result_details JSONB + verified BOOLEAN；
healing_id 对应我方 healing_intent_id。

### 🐛 Bug 修复（实测发现的真 bug）

- **db_writer.query() 含字面量百分号静默返回空**：原实现 cur.execute(sql, params or ())，
  空元组会触发 psycopg2 的 %-插值，SQL 里 LIKE 的百分号被当格式符 → 报错 →
  except 吞掉 → 返回空列表。表现为「明明有数据，LIKE 查询却查不到」。
  query_one() 同样问题。已修为「空参数不传」，并加回归测试 test_query_with_literal_percent。

### 📝 文档

- docs/database-schema.sql 同步新字段（保证「新建库 = 现有库」）
- docs/partner_schema_confirmation.md（新建）—— 给伙伴的字段确认回复
- docs/OPTIMIZATION.md v0.5 -> v0.6：P1-5 完成，已完成 9 -> 10，待办 11 -> 10

### ✅ 验收

- healing 新字段 12 项实测全过（含 CHECK 拒绝非法 status、分区表写入、索引可用）
- 单元测试 131 -> 132（新增百分号回归测试）
- DDL 前已备份 ~/intensure-backups/pre_healing_ddl.sql

---

## v3.11.1 - 2026-10-26 P1-3 代码风格：ruff 接入，CI 加防退化拦截

**触发**: 继续推进不依赖伙伴的工作 -> P1-3（代码风格靠作者自律）。

### ✨ 新增

- **`pyproject.toml`** —— ruff 配置
  `line-length=100` / `target-version=py312` / `extend-exclude=["legacy", ...]`（legacy 是 v2.0 归档代码，不维护）。
- **`.pre-commit-config.yaml`** —— ruff-pre-commit v0.6.9，钩子 `ruff --fix`，排除 `legacy/`。
  故意**不加** `ruff-format` hook：那会重排整个文件，制造与本次改动无关的大 diff。

### 🔧 一次性整改（76 处）

- `ruff check --fix` 自动修 **52 处**：未用 import（19）、f-string 无占位符（25）、
  文件尾缺换行（10）、多 import 挤一行（5）、未用变量（5）等。
- 手工拆 **24 处 E501 长行**：只改排版不改语义 —— 字符串用隐式拼接，
  SQL 用换行等价替换（`INSERT INTO t` / `CASE WHEN` 拆行后语义完全一致）。

### ⚠️ 关键豁免（不是风格问题，是硬约束）

- **E402（module-import-not-at-top-of-file）必须豁免**：
  `eventlet.monkey_patch()` 必须在 `os_ken` 之前调用，否则 eventlet 不会 patch
  os_ken 用到的 socket，**控制器直接挂**。涉及 `controller_app.py` / `live_env.py` /
  `run_all.py` / `integration_demo.py` 共 27 处。
- **E741（单字母变量名）豁免**：解析器里 `l`/`i` 表达索引是领域惯例。
- `legacy/` 整个目录排除在 lint 之外。

### 📝 其他

- **CI 新增 `ruff check .` 步骤** —— 防止风格退化。
- `docs/OPTIMIZATION.md` v0.4 -> v0.5：P1-3 移入「✅ 已完成」段；
  已完成 8 -> 9 项，待办 12 -> 11 项。

### ✅ 验收

- `ruff check .` → **All checks passed!**
- 单元测试 131(host) / 142(VM) 全绿（改行前后各验证一次）
- F821 undefined-name 与 F811 redefinition 排查后确认**只存在于 `legacy/`**（v2.0 归档代码），活代码无此问题。

---

## v3.11.0 - 2026-10-26 P1-1 测试覆盖：+131 个用例，CI 修复

**触发**: 主人说「先在没有其他人的情况下推进我们自己的工作」-> 挑 P1-1（测试覆盖率极低）做，
全程不依赖伙伴。

### ✨ 新增测试（6 个文件 / 131 用例，host）

- **`test_db_writer.py`**（35）—— 写库关口
  12 个 `write_*` 方法的成功+失败路径（失败返回 False/None 不抛、errors 计数 +1）、
  连接懒加载/复用/探活失败重连、state_history 30s 节流 + 变化触发 + force、
  `should_write_probe` 四种 anomaly_type、`should_write_consistency` 五种判定。
  含真实库集成用例（无 PG 时自动 SKIP）：upsert 幂等（version 递增且只有一行）+ 节流只落一行。
- **`test_assurance_mvp.py`**（24）—— 状态机核心
  3 个 extract 边界（按 IP/MAC 索引、/mask 剥离、OFPP_LOCAL 跳过、链路双向去重）+
  `check_one_policy` 7 种转换 + `diagnose_violation` 4 种诊断规则。
  重点覆盖「ARP 未触发时不算违例」这个易错边界。
- **`test_state_server.py`**（11）—— 11 个 REST 端点
  状态码 / JSON 格式 / since+limit 参数过滤 / 缺 key 返回 {} 而非 500 / links 双向去重成对。
- **`test_state_history.py`**（10）—— 环形缓冲 + 原子持久化
  MAX_ENTRIES 封顶、get 语义（key/limit/limit=0/未知 key）、dump 先写 .tmp 再 rename、load 往返。
- **`test_ovs_logger.py`**（15）—— 日志解析
  行解析、空行/畸形行返回 None、**UTC 时间戳不偏 8 小时**（这处错了归因会全错）、
  since/until/module/level/keyword/limit 六种过滤、buffer 定长、关联函数结构。
- **`test_state_model.py`**（26）—— 5 个验证器 + 主入口
  schema/sanity/consistency/completeness/anomaly 各自边界 +
  `validate_state` 的 ok/issue_count/by_level 汇总。

### 🐛 Bug 修复

- **CI 形同虚设**：`ci.yml` 原本只跑 `test_actual_state_builder.py test_reporter.py`，
  新增的测试文件根本不会被执行 —— 名义上有 CI，实际漏掉全部新增覆盖。
  现改为跑 9 个测试文件（`test_recovery.py` 排除：它是手动脚本，硬编码 VM 路径 + 需要真实 mininet 拓扑）。
- **`test_db_writer.py` 类名触发 pytest 误收集**：`TestWriter` 会被 pytest 当测试类收集并告警，
  改名为 `FakeWriter`。
- **真实库用例不幂等**：连续跑第二次会因上次的残留行而失败。
  改为跑前清理 + `finally` 清理（`intensure_user` 有 DELETE 权限）。

### 📊 统计

- 单元测试 **21 -> 131**（host）/ **142**（VM，多 11 个 Flask 用例）
- 6 个裸奔模块补齐：`db_writer` / `assurance_mvp` / `state_server` /
  `state_history` / `ovs_logger` / `state_model`
- 关键模块从 3/9 提升到 **9/9 有测试**
- CI 从 2 个测试文件提升到 9 个

### 📝 文档

- **`docs/OPTIMIZATION.md`**：v0.3 -> v0.4。P1-1 移入「✅ 已完成」段；
  **P0 全部完成 + P1-1 完成**，已完成 7 -> 8 项，待办 13 -> 12 项。

---

## v3.10.4 - 2026-10-25 P0-3 graceful shutdown + P0 全部收口

**触发**: 主人问「接下来应该做什么」-> 盘点 P0 剩余项，发现 P0-2/P0-4 其实早已实现（只是清单没记账），P0-3 是真缺口。

### ✨ 新增

- **`graceful.py`** —— 统一优雅退出（信号处理）：
  `install_graceful_shutdown(cleanups, name)` 注册 SIGTERM/SIGINT；
  收尾逻辑全包 try/except（一个失败不阻断其余）；打印 `GRACEFUL_SHUTDOWN` 标记便于日志断言；
  重复信号幂等（第二次直接 exit 130 强退）。
- 接入 3 个长驻进程：
  `controller_app.py`（落盘 state + 关 DB 连接）、`live_env.py`（停 Mininet 拓扑）、`assurance_mvp.py`（关 DB 连接）。
- **`test_graceful.py`** —— 4 个用例：注册 handler / cleanup 按序执行 / 异常不阻断后续 / 重复信号强退。

### 🐛 Bug 修复

- **`graceful.py` force-exit 后缺 `return`**：第二次信号走 `os._exit(130)` 后仍会继续往下跑。
  真实场景 os._exit 不返回所以无实际影响，但被单元测试逮到，已修。
- **`test_graceful.py` 的 check() 假绿**：原本只记录 FAILURES 不 raise，导致 pytest 把失败当通过。
  已改为真的 raise AssertionError —— **测试能失败才有意义**。

### 📝 文档

- **`docs/OPTIMIZATION.md`**：v0.2 -> v0.3。
  P0-2 / P0-3 / P0-4 移入「✅ 已完成」段并补说明与提交号；
  **P0 全部完成**（5/5），待办 16 -> 13，已完成 4 -> 7。
- **P0-2 说明**：代码早在 `ca5abc9`(10-09) 就修好了（User-Agent + 绕代理 + 单连接复用 + 错误不吞），
  但清单第 5 步「验证 reporter.check() 6/6 = 200 ACTIVE」**仍待办** —— 需要 A 同学 cloudflared 隧道（当前 HTTP 000）。
- **P0-4 说明**：`_get_conn()` 早已是懒加载 + 探活重连 + 线程锁，不是清单写的「每次新建连接」。

### ✅ 实测

`kill -TERM` 打在写入周期中途（update_count=20, switches=1, hosts=4）：
- 日志见 `[GRACEFUL_SHUTDOWN] controller_app received SIGTERM, running 2 cleanup(s)...`
- 进程干净退出
- 退出后 `network_state.payload` 仍是完整 JSON（`payload_valid=true`）
- `state_history` 232 行，**空 payload 0 行**，孤儿行 **0**
- `assurance.consistency_checks` 536 行（pass=388 / violated=148），无半截写入

### 📊 统计

- 7 文件改动（2 新代码 + 3 改代码 + 2 文档）
- 单元测试 17 -> 21 个，全绿

---

## v3.10 — 2026-10-09 文档整理（2026-10-09）：补全 30 天工作记录 + 清理过期描述

**触发**: 主人物理 workspace 后发现多个文档过期（CHANGELOG 只到 v3.0 / README 待做项过期 / STATE_MODEL 缺 v3.5+）

### ✨ 新增

- `memory/2026-10-09.md`（新建）今日完整对话 + intensure 状态盘点 + 下一步边界

### 📝 文档更新

- `CHANGELOG.md`：补 v3.5 / v3.6 / v3.7 / v3.8 / v3.9 五个版本
- `README.md`：完成状态表加 v3.0-v3.9；改过期'待做'为新'下一步'清单（划清边界）
- `STATE_MODEL.md`：加第 11 节'持久化层'（v3.5+ 数据库架构摘要 + 字段落库对应）
- `docs/our_tables_and_methods.md`：去掉'明天演示前 5 分钟'措辞，改为常驻参考 + 版本号 v0.2

### 📊 统计

- 4 文件改动 +195/-36 行
- 修复 5 个文档过期问题

---


---


---


## v3.9 — 2026-10-08 (07:13) 关掉心跳 + 简化文档

**主要作者**: 开发团队
**触发**: 清理开发阶段遗留物，恢复「自愈前后结果」可视化（v2.0 误删）

### ✨ 新增

- **「自愈前后对比」面板**（dashboard.html）
  - 位置：事件流面板下方，跨整行显示
  - 数据源：纯本地（不依赖保障模块）
    - `/api/events` 解析 `port_link_status` 事件，配对 down→up
    - `/api/history?key=connectivity` 取 ping 历史
  - 显示内容：每个故障周期的「故障前 ping」「故障中 ping」「恢复后 ping」
  - 持续时间 + 验证状态（已自愈/未恢复/异常）
- **live_env.py → /api/probe 自动上报**
  - 每个探测周期自动 POST connectivity 数据到 controller
  - 让 dashboard 的连通性/自愈对比面板有数据

### 🗑️ 移除

- **ActualState 样例面板**（dashboard.html）
  - 用途：联调 §4.2 字段对齐参考（开发阶段）
  - 删除：`ACTUAL_STATE_SAMPLES` 数组 + `renderActualStates()` 函数 + 4 秒循环
- **3 个 mock 样例卡片**（intentional 演示数据）

### 📝 文档修正

- **REQUIREMENTS.md §1**：同学标签全面纠正
  - 之前（错）：同学 A = 意图保障 / 同学 B = 意图实现 / 同学 C = 意图翻译
  - 现在（对）：**同学 A = 状态采集（本系统）**；同学 B/C = 意图智能体
  - 同步修正 11 处相关引用（意图保障模块、IP、Swagger、防火墙等）
- 添加「v2.0 重构时误删同学 A 标签」备注
- 添加"3 大职责完成度"诚实复盘

### 🐛 Bug 修复

- **Dashboard JS 错误**：renderBeforeAfter 用 `key=***` 应为 `key=connectivity`（typo 导致 connectivity 历史永远显示空）
- **state_server.py /api/probe**：清理调试 print，恢复干净输出

### 📊 验收指标

- Dashboard 顶部 badge：「意图接口不可达」彻底消失
- 4 个 legacy endpoints（/api/intents, /api/conflicts, /api/healing, /api/anomalies）仍 404
- 12 个保留 endpoints 全部 200
- 自愈前后对比面板成功渲染（基于真实 link events + ping history）

### 📁 文件变更

| 文件 | 变更 |
|---|---|
| `dashboard.html` | 1432 → 828 行 (-42%) |
| `state_server.py` | 551 → 208 行 (-62%) |
| `live_env.py` | +18 行（connectivity 自动上报） |
| `REQUIREMENTS.md` | §1 同学标签修正 + 11 处引用同步 |
| `CHANGELOG.md` | 新建（本文件） |

---


---


---


## v3.8 — 2026-10-08 (06:06) 一键演示脚本 + 汇报材料

**触发**: 演示准备

### ✨ 新增

- **`demo_scripts/demo_db_pipeline.sh`**（225 行）一键 E2E 演示
  - 8 步全绿(DEMO_AUTO=1):启动控制器 → Mininet → seed 策略 → 保障模块 → 健康基线 → 故障注入 → 恢复
  - 0 错误 / 23 成功
- **3 个汇报文档**（770 行）
  - `docs/presentation.md`（247 行）汇报大纲
  - `docs/speaker_notes.md`（290 行）逐句话术 + Q&A
  - `docs/demo_runbook.md`（215 行）演示照本宣科版

### 📊 配套产出

- 汇报材料 4 个文件 977 行
- v3.7 Phase 1 MVP 完整闭环

---


---


---


## v3.7 — 2026-10-08 Phase 1.2-1.4 保障模块 MVP + seed + E2E 演示

**主要**: 保障模块 + E2E 演示

### ✨ 新增

- **`assurance_mvp.py`**（480 行）保障模块 MVP
  - 8 状态有限状态机(REGISTERED → ACTIVE → VIOLATED → DIAGNOSING → HEALING → VERIFYING → RECOVERED / BLOCKED)
  - 一致性检查 + 根因诊断 + 5 重保护自愈
- **`seed_policies.py`**（98 行）mock 2 条 ACCESS_CONTROL 策略
- **`db_writer.py`** 扩展:get_assurance_writer + query/query_one 读方法

### 🐛 修复

- 3 个 GRANT bug:translation_user / implementation_user / assurance_user 都缺自己 schema 的 USAGE
- intent_states.intent_id 加 UNIQUE 约束(让 ON CONFLICT 工作)

### 📊 验收

- 158 条 consistency_checks + 120 条 diagnoses
- confidence=0.85, affected={s1}, down_ports=['s1:eth1']
- 故障注入 → state=0/DOWN(intensure 真实捕获)

---


---


---


## v3.6 — 2026-10-08 Phase 1.1 intensure 选择性写库

**主要**: db_writer.py + controller 接入

### ✨ 新增

- **`db_writer.py`**（515 行）选择性写库核心
  - 12 个方法:10 写 + 2 读
  - 选择性写规则:30s 节流 + 变化才写 + 异常才写
  - 连接池(懒加载 + 自动重连)
- **`controller_app.py`** 新增:_stats_loop 5s 调 write_network_state + write_state_history;_log_event 写端口/链路事件
- **`live_env.py`** 新增:write_probe_with_decision(自动判断异常才写)

### 📊 验收

- 100 意图 1 年存储 ≈ 13 GB(对比全量 270 GB,节省 95%)
- 30s 节流 + summary 变化触发 on-change

---


---


---


## v3.5 — 2026-10-08 数据库架构 Phase 0 5 schema + 43 表 + 4 模块 GRANT + 保留策略

**主要**: 数据库架构落地

### ✨ 新增文档（2240 行 / 8 文件）

- **`docs/database-architecture.md`**（353 行）架构总览 + 决策记录
- **`docs/database-schema.sql`**（551 行）43 张表 DDL
- **`docs/access-control-grants.sql`**（117 行）4 模块 GRANT
- **`docs/access-control-matrix.md`**（233 行）权限矩阵
- **`docs/cross-module-api-contract.md`**（292 行）跨模块 API 契约
- **`docs/migration-strategy.md`**（309 行）迁移策略
- **`docs/retention-policy.md`**（250 行）数据保留策略
- **`db_maintenance/retention_cleanup.sql`**（135 行）保留清理 SQL

### 🎯 关键决策

- 部署位置:Colima VM 本地(主人指定)
- 范围:Phase 0 + 1,先跑通 MVP
- 重点:意图保障智能体(主人指定)
- 5 schema:intensure / translation / implementation / assurance / shared
- 43 张表,4 模块 GRANT 隔离

### 📊 关键约束落地

> 翻译模块可以查询有效规则,但不能因为生成了消解方案就把已部署规则删除。
>
> **实现**: GRANT 只给 translation_user 对 implementation.policies 的 SELECT 权限(无 INSERT/UPDATE/DELETE)。

---


---


---


## v2.0 — 2026-09-03 (02:50) 重构：与上游契约对齐

**触发**: 用户分享 `~/Desktop/网络智能体/采集模块联调说明.md`（同学 A 写）

### 核心变更

1. **职责重定义**：从"全包"变为"只采集 + 上报"
   - 删除：一致性校验、冲突检测、自愈回路、异常检测（其他同学负责）
   - 保留：Mininet 拓扑、状态采集、ActualState 组装、REST API、采集视角 Dashboard
2. **新增模块**：
   - `actual_state_builder.py`：内部 state → ActualState 格式转换
   - `reporter.py`：HTTP POST 客户端（指数退避重试 + 线程安全统计）
   - `integration_demo.py`：端到端联调 demo（real/mock/skip 三模式）
   - `mock_assurance_server.py`：本地 mock 保障模块（离线调试）
   - `live_env.py`：v2.0 重构（接受环境变量 + 周期上报）
3. **归档到 legacy/**：12 个旧模块（conflict_detector/healing_engine 等）
4. **测试**：21 + 24 单元测试全过
5. **文档**：REQUIREMENTS.md / INTEGRATION_PLAN.md / README.md v2.0 重写

---


---


---


## v1.0 — 2026-08-04 ~ 2026-09-02 初始开发

- Mininet + Colima + os-ken 环境搭建
- 自定义拓扑（simple/multi/mesh）
- 链路事件监听（LLDP + PortStatus）
- 故障注入（断链/时延/丢包/限速）
- 采集视角 Dashboard（单文件 SPA）
- 7 个 bug 修复（详见 `memory/2026-08-04.md` / `memory/2026-08-05.md`）

---



## 🎯 当前状态（v3.9）

| 同学 A 职责 | 状态 |
|------------|------|
| Mininet 场景搭建（拓扑 + 故障注入 5 类）| ✅ 100% |
| 网络状态采集（交换机/端口/流表/主机连接）| ✅ 100% |
| 实际状态模型（统一 JSON + 历史 + 接口）| ✅ 100% |
| 可视化（拓扑 + 链路 + 告警 + 自愈前后）| ✅ 100% |
| 数据库（5 schema + 43 表 + 选择性写库）| ✅ 100% |
| 一键 E2E 演示 + 汇报材料 | ✅ 100% |

**对外接口**：POST `/check` + `/verify`（v2.0）+ ActualState 主动推送（v3.6+）
**已联调**：A 同学 assurance-agent 0.4.0, register→check→diagnose 走通（2026-09-19）
**下一步**：三方联调真实数据完整闭环 + reporter.py 频率/DNS bug 修复

---

