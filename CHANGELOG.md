# CHANGELOG — 网络意图 SDN 采集模块

> 历史变更记录。所有变更都有对应 git commit 与测试验证。

---

## v3.0 — 2026-09-03 (05:50) Dashboard 调试面板清理 + 自愈前后对比重建

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

## v1.0 — 2026-08-04 ~ 2026-09-02 初始开发

- Mininet + Colima + os-ken 环境搭建
- 自定义拓扑（simple/multi/mesh）
- 链路事件监听（LLDP + PortStatus）
- 故障注入（断链/时延/丢包/限速）
- 采集视角 Dashboard（单文件 SPA）
- 7 个 bug 修复（详见 `memory/2026-08-04.md` / `memory/2026-08-05.md`）

---

## v3.9 — 2026-10-08 (07:13) 关掉心跳 + 简化文档

**触发**: 主人反馈 60s 心跳仍给 DB 大量负担 + 不太明白"我们负责哪些表 + 哪些方法"

### 🐛 修复

- **心跳默认关闭**
  - 环境变量 `INTENSURE_HEARTBEAT=1` 启用
  - 验证: 清空 module_health 后跑 30s 确认 0 行写入
  - controller_app.py + assurance_mvp.py 同步改

### 📝 新文档

- **`docs/our_tables_and_methods.md`**（241 行，演示现场速查）
  - 我们写 8 张表（6 intensure + 2 shared），0 读别人表
  - db_writer.py 12 个方法 + 触发时机
  - 8 条演示现场可用的 SQL 查询
  - Q&A 标准答案

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