# 数据库多模块化架构 · 开发规划 v0.1

> **范围**：意图驱动网络管理系统的 4 个智能体（采集/翻译/实现/保障）共享一个 PostgreSQL
> **目标**：先统一部署、各模块用自己的一组表；按需可拆；**先把数据归属 + 读写接口约定清楚，比提前拆开更重要**
> **状态**：📋 设计已批准，进入 Phase 0+1 执行
> **作者**：庄英琪（基于 2026-10-08 主人思路整理）

---

## 决策记录（2026-10-08）

| 决策点 | 选项 | 选择 | 理由 |
|---|---|---|---|
| 部署位置 | A VM 本地 / B 腾讯云 / C 云 RDS | **A：Colima VM 本地** | 主人指定：方便开发 |
| 本轮范围 | 只交 Phase 0 文档 / Phase 0+1 | **Phase 0+1：跑通 MVP** | 主人指定：先跑通再说 |
| 重点模块 | 均衡 / 保障 | **意图保障（第三智能体）** | 主人指定：再聚焦多一点网络监测智能体 |

---

## 0. 背景与现状

| 项 | 现状 |
|---|---|
| 模块数 | 4（intensure 采集 / 意图翻译 / 意图实现 / 意图保障） |
| 通信方式 | HTTP REST + cloudflared 隧道（无共享存储） |
| 持久化 | 各模块独立：intensure 用 `/tmp/network_state.json` + in-memory；assurance-agent 自带存储；其他模块尚无 DB |
| 主要痛点 | ① 数据散落、无法跨模块分析 ② 重启丢历史 ③ 审计/排障难 |

---

## 1. 核心设计原则（4 条铁律）

1. **单一权威源（Single Source of Truth）**
   每份数据有**唯一所有者**（owner module），其他模块不能直接写。
2. **API 层是边界**
   跨模块读写**必须**走对方模块的 API，不能直接动对方的表。
3. **DB 角色做"防御纵深"**
   每个模块用独立 DB 用户，只授权本模块表；API 漏了也有 DB 兜底。
4. **可拆分性**
   表结构 + API 设计要让"未来拆 DB"只是改连接串 + 拆角色，**不动业务逻辑**。

---

## 2. 模块与数据归属（Schema-per-module）

采用 PostgreSQL **Schema 隔离**（不是独立 Database），5 个命名空间：

### 2.1 `intensure` — 采集层（庄英琪负责）
**自有表**（采集层是"只写不读"的传感器）：

| 表 | 字段概要 | 说明 |
|---|---|---|
| `intensure.network_state` | dpid, ports(JSON), flows(JSON), updated_at | 当前状态快照（替代 `/tmp/network_state.json`） |
| `intensure.state_history` | state_id, snapshot_at, payload(JSONB), ttl_days | 历史快照（替代 in-memory 环形缓冲） |
| `intensure.port_events` | event_id, dpid, port_no, state, occurred_at | 端口 up/down 事件流 |
| `intensure.link_events` | event_id, src_dpid, src_port, dst_dpid, dst_port, state, occurred_at | 链路事件 |
| `intensure.flows_snapshot` | dpid, flow_count, flows(JSON), captured_at | 流表快照 |
| `intensure.probe_results` | probe_id, src_host, dst_host, reachable, latency_ms, packet_loss, probed_at | 连通性探测结果 |

**读**：无（采集层是传感器）
**写**：所有 `intensure.*`

### 2.2 `translation` — 意图翻译模块（B 同学）
**自有表**：

| 表 | 说明 |
|---|---|
| `translation.intent_templates` | 意图模板/规则库（NL 槽位定义） |
| `translation.parsed_intents` | NL → JSON 解析结果（idempotency_key, raw_nl, intent_json, parsed_at, status） |
| `translation.translation_audit` | 翻译审计（哪些 NL 命中了哪些规则） |

**读（关键！）**：
- `implementation.policies`（"当前已部署的有效规则"）
- `assurance.consistency_history`（哪类意图历史上常违规，避坑）

**写**：仅 `translation.*`

### 2.3 `implementation` — 意图实现模块（C 同学）
**自有表**（**核心权威表**——"线上是什么"）：

| 表 | 说明 |
|---|---|
| `implementation.policies` | **已部署的策略/意图**（唯一权威） |
| `implementation.policy_versions` | 策略历史版本（用于回滚/审计） |
| `implementation.deployments` | 部署记录（每次部署 = 1 行） |
| `implementation.deployment_logs` | CLI/NETCONF 执行日志 |
| `implementation.rollback_history` | 回滚记录 |

**读**：
- `translation.parsed_intents`（待部署意图）

**写**：仅 `implementation.*`

### 2.4 `assurance` — 意图保障模块（A 同学）
**自有表**：

| 表 | 说明 |
|---|---|
| `assurance.intent_states` | 每个意图的状态机轨迹（REGISTERED/ACTIVE/VIOLATED/...） |
| `assurance.consistency_checks` | 一致性检查结果（expected vs actual） |
| `assurance.diagnoses` | 根因诊断记录（含 confidence + affected_devices） |
| `assurance.healing_intents` | 自愈意图（Guard 校验 + verdict） |
| `assurance.healing_history` | 自愈执行历史 |

**读**：
- `implementation.policies`（期望状态）
- `intensure.network_state` + `intensure.state_history`（实际状态）
- `assurance.*`（自己的历史）

**写**：仅 `assurance.*`

### 2.5 `shared` — 跨模块公共（所有模块可写 append-only）

| 表 | 说明 |
|---|---|
| `shared.audit_log` | 审计日志（**append-only**，无 UPDATE/DELETE 权限） |
| `shared.module_health` | 各模块心跳 + 健康状态 |
| `shared.schema_version` | schema 版本（迁移追踪） |

---

## 3. 读写接口约定（API Contract）

### 3.1 你的核心例子（写在契约第一条）

> **翻译模块可以查询"有效规则"，但不能因为生成了消解方案就把已部署规则删除。**

翻译流程拆解：
```
1. 翻译模块收到用户 NL
2. 调用实现模块 API：GET /api/implementation/v1/policies/active?intent_type=...
   （实现模块从 implementation.policies 读，返回给翻译）
3. 翻译模块生成消解方案
4. 翻译模块调用实现模块 API：POST /api/implementation/v1/resolutions
   body = { intent_id, resolution_spec }
   （注意——是 POST /resolutions，不是 DELETE /policies/xxx）
5. 实现模块内部校验（是否冲突 / 影响线上）→ 决定是否应用
6. 由实现模块自己更新 implementation.policies
7. 翻译模块从不直接写 implementation.policies
```

### 3.2 API 形态

每个模块对外暴露：
- **读 API（query）**：供其他模块查询，返回 DTO
- **写 API（command）**：供其他模块触发操作；**实际写入由本模块负责**
- 路径规范：`/api/{module}/v{version}/{resource}`
- 错误码：标准 HTTP（200/4xx/5xx）+ 业务 code（VALIDATION_FAILED / CONFLICT / FORBIDDEN 等）

### 3.3 跨模块调用矩阵

| 调用方 | 目标 | API（通过实现模块 API 而非直连 DB） |
|---|---|---|
| 翻译 → 实现 | 查有效规则 | `GET /api/implementation/v1/policies/active` |
| 翻译 → 实现 | 提交消解方案 | `POST /api/implementation/v1/resolutions` |
| 翻译 → 实现 | 撤回已部署策略 | `POST /api/implementation/v1/policies/{id}/retire`（走 API，不直删表） |
| 实现 → 翻译 | 取待部署意图 | `GET /api/translation/v1/parsed?status=ready` |
| 保障 → 实现 | 取期望状态 | `GET /api/implementation/v1/policies` |
| 保障 → 采集 | 取实际状态 | `GET /api/intensure/v1/state/current` |
| 保障 → 采集 | 取历史快照 | `GET /api/intensure/v1/state/history?since=...` |
| 保障 → 翻译 | 取意图模板 | `GET /api/translation/v1/templates` |
| 任何 → shared | 写审计 | `POST /api/shared/v1/audit`（append-only） |
| 任何 → shared | 写健康 | `POST /api/shared/v1/health/heartbeat` |

---

## 4. 访问控制矩阵（DB 层 · 防御纵深）

每个模块用独立 DB 用户，权限如下（GRANT 表）：

| 表 / 操作 | `intensure_user` | `translation_user` | `implementation_user` | `assurance_user` | `shared_writer` |
|---|---|---|---|---|---|
| `intensure.*` | **RW** | – | – | **R** | – |
| `translation.*` | – | **RW** | **R** | **R** | – |
| `implementation.policies` | – | **R only** | **RW** | **R** | – |
| `implementation.deployments` | – | **R** | **RW** | **R** | – |
| `assurance.*` | – | **R** | **R** | **RW** | – |
| `shared.audit_log` | – | – | – | – | **INSERT only**（无 UPDATE/DELETE） |
| `shared.module_health` | **W (own row)** | **W (own row)** | **W (own row)** | **W (own row)** | – |
| `shared.schema_version` | R | R | R | R | – |

> 关键：`translation_user` 对 `implementation.policies` **只有 SELECT**，无法 DELETE/UPDATE。即使翻译模块的代码 bug 了，也删不掉线上规则。

---

## 5. 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| 数据库 | **PostgreSQL 16** | 项目已有；schema 隔离原生支持 |
| 隔离粒度 | **Schema**（不是 Database） | 同 PG 实例即可、运维简单；拆 DB 时只改连接串 |
| 迁移工具 | **Alembic**（Python 模块）+ **Flyway**（Java/Spring Boot 模块） | 业界标准 |
| 连接管理 | 每个模块独立 DB 用户；连接串带 `search_path=intensure,shared,public` | 显式限定命名空间 |
| ORM | SQLAlchemy（Python）+ JPA（Java） | 各模块自选；表结构在共享 SQL DDL 里维护（DRY） |
| ID 规范 | 全局 UUID v7（时间排序）；表内自增 id 仅做内部顺序 | 跨模块追踪 + 索引友好 |

---

## 6. 部署位置（**待你拍板**）

| 选项 | 优点 | 缺点 |
|---|---|---|
| **A. intensure Colima VM 本地** | 开发零依赖；调试快 | 性能差；只 intensure 能用 |
| **B. 腾讯云 124.220.49.156 独立实例** | 4 模块都能访问（局域网）；已有 PostgreSQL 经验 | 跟二手项目 DB 共用服务器 |
| **C. 云 RDS（PostgreSQL 托管）** | 生产化；备份/高可用开箱即用 | 成本；需公网/内网穿透 |

**我的建议**：开发期 **B**（跟二手项目同服务器，节约成本），生产化阶段再迁 **C**。

---

## 7. 阶段规划

### **Phase 0：契约定义（本轮要交付）** ← 现在
- [x] `docs/database-architecture.md`（本文档）✅
- [ ] `docs/database-schema.sql`（完整 DDL，5 个 schema + 表 + 索引；**为保障高频读优化**）
- [ ] `docs/access-control-matrix.md`（DB 用户 + GRANT 脚本 + 越权测试）
- [ ] `docs/cross-module-api-contract.md`（API 契约 spec，**重点保障常用 3 条**）
- [ ] `docs/migration-strategy.md`（Alembic/Flyway 初始化 + 版本规则）

**验收**：主人审批；4 模块都签字认领自己的表 + 接受 API 契约。

### Phase 1：MVP（**保障模块优先**） ← 本轮也要交付
- [ ] Colima VM 安装 PostgreSQL 16 + 初始化 5 schema + 用户/权限
- [ ] intensure 改造：ActualState + 探测结果写入 `intensure.*`
- [ ] **保障模块 MVP 脚本**（独立的）：
  - 读 `intensure.network_state` 当前快照
  - 读 `implementation.policies` 期望状态
  - 执行一致性检查 → 写 `assurance.consistency_checks`
  - 如检测违例 → 执行诊断 → 写 `assurance.diagnoses`
- [ ] mock 实现模块：seed 几条 `implementation.policies`（先不接 C 同学）
- [ ] mock 翻译模块：返回桩响应
- [ ] E2E 验证：注入故障 → intensure 入库 → 保障检测 → 诊断 + 入库

**验收**：跑一轮 E2E（intensure → DB → 保障 → DB）有数据落库 + 控制台输出 + 截图。

### Phase 1：MVP（intensure 先跑通）
- [ ] DB 实例起来，schema + 用户 + 权限就位
- [ ] intensure 改造：ActualState 写入 `intensure.*`
- [ ] 实现 1 条跨模块闭环证明读写接口跑通：
  - 翻译 → 实现 API → 写入 policies
  - 保障读 policies + intensure state → 写入 diagnoses
- [ ] 其他模块暂用 mock，但 API 契约不变

**验收**：intensure 持久化跑 24h 无异常；1 条跨模块 API 调用成功。

### Phase 2：完整闭环
- [ ] 4 模块全部接入 DB
- [ ] 审计日志 + 健康监控上线
- [ ] 迁移工具就绪（schema 版本管理）
- [ ] 现有 `/tmp/network_state.json` / in-memory 全部废弃

### Phase 3：拆分（如果需要）
- [ ] 每个模块独立 DB
- [ ] 改连接串 + 迁移数据 + 调整角色
- [ ] 业务代码不动（因为一直走 API）

---

## 8. 与现有系统的兼容

| 现有 | 新方案 | 迁移路径 |
|---|---|---|
| `/tmp/network_state.json` | `intensure.network_state` 表 | Phase 1 写入；Phase 2 废弃文件 |
| in-memory 环形缓冲 | `intensure.state_history` 表 | Phase 1 写入；Phase 2 删内存代码 |
| assurance-agent 现有存储 | 迁移到 `assurance.*` 表（如果 A 同学愿意）| Phase 2 协商 |
| assurance-agent 的 cloudflared HTTP API | 保留对外暴露；内部模块间走 DB + 内网 API | 长期并存 |
| intensure → A 的 `POST /check` | 保障模块直接 `SELECT FROM intensure.network_state` | Phase 1 改造 |
| REST `/api/state`（仪表盘） | 保留；从 `intensure.network_state` 读 | Phase 1 调整 |

---

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| API 性能（跨模块查询走网络） | 关键路径加缓存（in-memory TTL）；非关键走异步 |
| 跨模块事务难原子 | 用 **Saga 模式**：每步独立提交 + 补偿；最终一致 |
| 拆分时数据迁移难 | Phase 0 设计表结构时**避免跨 schema 外键**，只用 ID 引用 |
| DB 用户/权限配置错导致越权 | Phase 0 写 e2e 权限测试（每个用户尝试越权，期望拒绝） |
| 团队协调成本 | Phase 0 必须全员签字；API 契约用 OpenAPI 3 spec 锁版本 |

---

## 10. 本轮交付物清单（5 个文档 + MVP 跑通）

### Phase 0：5 个文档（先交这些）

| 文件 | 状态 |
|---|---|
| `docs/database-architecture.md` | ✅ 已完成（含决策记录 + 保障优先设计） |
| `docs/database-schema.sql` | ⏳ 下一个 |
| `docs/access-control-matrix.md` | ⏳ |
| `docs/cross-module-api-contract.md` | ⏳（重点：保障模块 3 条核心 API） |
| `docs/migration-strategy.md` | ⏳ |

### Phase 1：MVP 跑通

- [ ] Colima VM 装 PostgreSQL 16 + 初始化
- [ ] intensure 改造（写 `intensure.*`）
- [ ] 保障模块 MVP 脚本（独立可跑）
- [ ] mock 实现/翻译模块
- [ ] E2E 故障注入 + 数据落库验证

---

## 11. 主人已拍板的 3 件事（决策记录见顶部）

1. ✅ **部署位置**：A — Colima VM 本地
2. ✅ **本轮范围**：Phase 0 + Phase 1（**跑通 MVP**）
3. ✅ **重点模块**：**意图保障（第三智能体）**——设计围着它转

---

## 12. 保障模块优先的具体设计影响

既然要重点保障模块，以下设计决策都围绕"保障模块跑得顺"：

### 12.1 保障的高频路径 → 索引策略
保障每 5 秒做一次一致性检查，需要频繁读：
- `intensure.network_state`：当前快照用 **单行 upsert + version 列**（不是 append-only history）
- `implementation.policies`：给 `(intent_id, status, effective_from)` 建复合索引
- `assurance.consistency_checks`：**(intent_id, checked_at DESC)** 复合索引（查"最近 N 次"）
- `assurance.intent_states`：**(intent_id, updated_at DESC)** 复合索引

### 12.2 保障的"证据"需求 → 字段不省
09-19 联调的关键发现：**空证据 → Guard BLOCKED**；真实证据 → APPROVED
所以 `assurance.diagnoses` 必须存：
- `evidence JSONB`（down_ports/down_links/conflicting_flows 等原始证据）
- `confidence NUMERIC`（0.00-1.00）
- `affected_devices TEXT[]`（Postgres 数组类型，方便 `=ANY()` 查询）
- `root_causes JSONB`（候选原因数组）

### 12.3 时间序列 → 分区策略
高频写入表按时间分区（避免后期膨胀）：
- `intensure.state_history`：按 `snapshot_at` 月分区
- `assurance.consistency_checks`：按 `checked_at` 月分区
- `assurance.healing_history`：按 `executed_at` 月分区
- 其他低频表不分

### 12.4 跨模块读保障路径 → 物化视图
保障每次都 `JOIN intensure.network_state + implementation.policies`，加 **物化视图** `assurance_mv.intent_status` 提升性能：
```sql
CREATE MATERIALIZED VIEW assurance_mv.intent_status AS
SELECT p.intent_id, p.expected, n.actual, ...
FROM implementation.policies p
CROSS JOIN intensure.network_state n;
-- 保障模块查这个视图，不是 JOIN 原表
```
**带 REFRESH MATERIALIZED VIEW CONCURRENTLY**（不阻塞读）。

---

_文档创建：2026-10-08 · v0.1 · 待主人审批_