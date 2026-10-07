# 意图驱动网络管理 · 数据库多模块化 · 汇报材料 v0.1

> **作者**：庄英琪（主人 宝宝）
> **日期**：2026-10-08（汇报日 2026-10-09）
> **版本**：v3.7（Phase 1 MVP 完整闭环）
> **配套**：
> - `docs/speaker_notes.md`（讲稿，逐句话术）
> - `docs/demo_runbook.md`（演示照本宣科）
> - `demo_scripts/demo_db_pipeline.sh`（一键演示脚本）

---

## 一、项目背景

「意图驱动网络管理」由 **3 个智能体**协作：

```
意图翻译(NL→JSON) → 意图实现(JSON→CLI) → 意图保障(一致性+诊断+自愈)
```

**主人负责的部分**：**状态采集层 intensure**（"眼睛"，看见网络）

之前的问题：
- 数据散落，无持久化
- 跨模块靠 HTTP/cloudflared 隧道（脆弱）
- 重启丢历史
- 无法做跨模块审计/排障

---

## 二、本次目标（你说的话）

> "我们下一步整个项目打算开发一些数据库... 现在先把数据归属、读写接口约定清楚，比提前拆开更重要。"

> "翻译模块可以查询有效规则，但不能因为生成了消解方案就把已部署规则删除。"

**3 个决策**：
1. 部署位置：**本地**（Colima VM）
2. 范围：**Phase 0 + Phase 1（先跑通 MVP）**
3. 重点模块：**意图保障**（第三智能体）

---

## 三、用到的核心方法

### 3.1 数据库架构原则（4 条铁律）

| # | 原则 | 实现 |
|---|---|---|
| 1 | **单一权威源**（SSOT）| 每份数据有唯一所有者，其他模块不能直接写 |
| 2 | **API 层是边界** | 跨模块读写必须走 API，不能直连对方 DB |
| 3 | **DB 角色做"防御纵深"** | 每个模块独立 DB 用户，API 漏了 DB 兜底 |
| 4 | **可拆分性** | 表结构 + API 设计让"未来拆 DB"只是改连接串 + 拆角色 |

### 3.2 Schema-per-module（不是 Database-per-module）

5 个 PostgreSQL schema：
- `intensure`（采集层）
- `translation`（翻译层）
- `implementation`（实现层）
- `assurance`（保障层）
- `shared`（公共：审计/健康）

### 3.3 **A+B 混合数据保留策略**

| 维度 | 方法 |
|---|---|
| **A** | 时间分区基础设施（`drop_old_partitions()` 函数 + 月分区表）|
| **B** | **选择性写库 + 心跳**（关键节省 ~95% 存储）|

**容量预估（100 意图）**：
| 方案 | 1 天 | 1 年 |
|---|---|---|
| 不优化 | 750 MB | 270 GB 💥 |
| **A+B 混合** | **36 MB** | **13 GB** ✅ |

### 3.4 端口标签格式（关键修复）

**问题**：原端口 `sw1:p2-sw2:p3` 数字式不直观，09-19 联调发现保障模块识别的"端口证据"无法对应。

**修复**：改用 `s1:eth2-s2:eth3` 设备名:端口名格式。

**后续**：09-19 联调关键因果 —— **真实 down_ports 证据 → confidence 0.85 → Guard APPROVED**；空证据 → 0.4 → BLOCKED。

---

## 四、实现的功能

### 4.1 数据库基础设施（Phase 0）

✅ **5 schema + 18 base 表 + 25 月分区 + 32 索引 + 1 物化视图** = 43 张表
✅ **4 个模块用户**（intensure_user / translation_user / implementation_user / assurance_user）+ **RLS 行级安全**
✅ **访问控制矩阵**：翻译对 implementation.policies 仅 SELECT ✅（你提的核心约束）
✅ **保留清理函数**：`drop_old_partitions()` 已验证
✅ **5 个文档**：架构 / DDL / 权限矩阵 / API 契约 / 迁移策略 / 保留策略（1728 行）

### 4.2 选择性写库（Phase 1）

✅ `db_writer.py`（450 行）：单一写入入口，自动重连，统计
✅ **选择性写规则**：
- `network_state` 每次都写（单行 upsert）
- `state_history` 30s 节流 + summary 变化触发
- `probe_results` 仅异常（reachable 变化 / 丢包>1% / 延迟>200ms）
- `consistency_checks` 变化或 60s 心跳

### 4.3 intensure 改造

✅ `controller_app.py`：每 5s 写 network_state + 选择性 state_history + 心跳
✅ `live_env.py`：探针结果用 `write_probe_with_decision`（异常才写）

### 4.4 保障模块 MVP

✅ `assurance_mvp.py`（350+ 行）：
- 每 5s 读 intensure.network_state
- 读 implementation.policies（期望状态）
- 对每个 policy 做 reachability 一致性检查
- 写 consistency_checks（变化/心跳）
- 检测到违例 → 写 diagnoses（含 evidence/confidence/affected_devices）
- 心跳到 module_health

✅ `seed_policies.py`：mock 实现模块 seed 策略

### 4.5 完整闭环验证

✅ **故障注入 → 检测 → 诊断入库** 端到端跑通：
- h1-eth0 down → s1-eth1 state=0/DOWN
- 保障模块记录 2 条 VIOLATED 检查
- 写 2 条 diagnoses：**confidence=0.85**, affected_devices={s1}, down_ports=["s1:eth1"]
- 故障恢复 → 检查回到 pass

---

## 五、关键因果（最重要的发现）

> **真实证据 → 高 confidence → 自愈可放行**

| 场景 | down_ports | confidence | Guard |
|---|---|---|---|
| 空证据 | `[]` | 0.4 | **BLOCKED** |
| 真实证据 | `["s1:eth1"]` | **0.85** | **APPROVED** |

**结论**：采集层把端口真实状态填进 `down_ports`，是自愈能被放行的决定性因素。

---

## 六、演示脚本 + 方法

### 6.1 一键演示

```bash
# 在 VM 内：
cd /home/sunxiaoxuan.guest/intensure
DEMO_AUTO=1 bash demo_scripts/demo_db_pipeline.sh
```

### 6.2 演示流程（8 步，~90s）

| # | 步骤 | 演示要点 |
|---|---|---|
| 0 | 前置清理 | tmux + pkill 一键清场 |
| 1 | 启动 os-ken 控制器 | tmux session 'intensure' 跑 controller |
| 3 | 启动 Mininet | single,4 拓扑；4 主机 |
| 3 | 触发 ping | 主机学到 IP |
| 3 | seed 策略 | mock 实现模块写 2 条 |
| 4 | 启动保障模块 | tmux window 跑 assurance_mvp |
| 5 | 健康基线 | **2 pass, 0 violated** |
| 6 | 注入故障 | h1-eth0 down |
| 6 | 检测 | **2 VIOLATED + 2 diagnoses（conf=0.85）** |
| 7 | 恢复 | h1-eth0 up |
| 7 | 验证 | **回到 pass** |
| 8 | 统计 | 158 consistency_checks + 120 diagnoses |

### 6.3 关键查询（演示中可现场跑）

```sql
-- 网络状态
SELECT * FROM intensure.network_state;

-- 一致性检查
SELECT result, violation_types, evidence 
FROM assurance.consistency_checks 
ORDER BY id DESC LIMIT 5;

-- 诊断详情（含证据）
SELECT intent_id, confidence, affected_devices, 
       root_causes->0->>'type' AS cause,
       evidence->'down_ports' AS down_evidence
FROM assurance.diagnoses
ORDER BY diagnosed_at DESC LIMIT 3;

-- 越权测试（证明双保险）
SET ROLE translation_user;
DELETE FROM implementation.policies WHERE intent_id = 'test';
-- ERROR: permission denied for table policies
```

### 6.4 演示中可展示的文档

- `docs/database-architecture.md`（架构原则）
- `docs/database-schema.sql`（完整 DDL，45 张表）
- `docs/access-control-matrix.md`（访问控制矩阵）
- `docs/cross-module-api-contract.md`（API 契约）

---

## 七、版本 + git 历史

```
de4bc94 v3.7 Phase 1.2-1.4：保障模块 MVP + seed + E2E 演示
75b4331 v3.6 Phase 1.1：intensure 选择性写库
efd55e2 v3.5 数据库架构 Phase 0：5 schema + 43 表 + 4 模块 GRANT + 保留策略
5b67e98 v3.4 联调脚本与文档：demo_scripts 一键演示 + 联调手册 + 项目汇报
1c18857 v3.3 联调增强：端口标签 s1:eth2 + OVS 详细日志接入 + safe_ping 防死锁
022a7ab v3.2 添加 .gitignore + 从 repo 移除 __pycache__/
```

---

## 八、未完成 / 下一步

| # | 项 | 优先级 |
|---|---|---|
| 1 | 修复 `demo_scripts/` 里的 API 调用（旧的 A 同学服务已下线） | 高 |
| 2 | 翻译模块接入（mock 现在只是空行） | 中 |
| 3 | 推送 3 个 v3.5/v3.6/v3.7 commit 到 GitHub | 中 |
| 4 | 跨智能体对接文档（同学 A/B/C 接口清单）| 中 |
| 5 | 录制演示视频（用于老师放 PPT） | 低 |

---

## 九、待你说的问题

汇报时老师可能会问：

| 问题 | 答案 |
|---|---|
| 为什么不用一个大表？ | 数据归属混乱 + 越权风险 + 拆分困难 |
| 为什么用 PostgreSQL？ | 项目已有；schema 原生支持；JSONB 适合网络状态 |
| 为什么装本地不装云？ | 你指定，方便开发 |
| 翻译真的不能删规则吗？ | **不能** —— API 层禁止 DELETE；DB 层 translation_user 无 UPDATE/DELETE 权限 |
| 自愈怎么触发？ | MVP 只做检测；自愈留待 Phase 2 接实现模块 |
| 真实环境怎么跑？ | 同一台 VM；横向扩展只需改 connection + 拆 DB |

---

_汇报材料 v0.1 · 写于 2026-10-08 · Phase 1 MVP 完成_
_详情见 `speaker_notes.md`（讲稿）和 `demo_runbook.md`（演示照本宣科）_