# 汇报讲稿 v0.1（逐句话术）

> **总时长建议**：15-20 分钟
> **节奏**：前 8 分钟讲方法 + 功能 → 中间 8 分钟演示 → 最后 4 分钟总结 + Q&A
> **心态提示**：你已完成了大量工程，且预期（Ask 联调）已跑通——**自信地说**

---

## 【开场 · 30 秒】

> 老师好，我汇报一下「意图驱动网络管理」**状态采集层 intensure** 这个项目。
>
> 之前我们整个 4 个模块是靠 HTTP REST + cloudflared 隧道通信的，没有共享数据库，**重启就丢历史**。
>
> 这次我做的核心工作是：**用一个 PostgreSQL + 5 个 schema 把数据持久化起来**，并且**约定好每个模块能写哪些表、不能写哪些表**。

---

## 【一、为什么要做这个 · 2 分钟】

> 我们有 **3 个模块**（翻译/实现/保障）+ 我负责的采集层，一共 4 个智能体。
>
> 之前大家数据都散在自己内存里，**想查一个东西得知道它在谁那儿**。
>
> 而且我们的核心约束是：**翻译模块可以查询有效规则，但**不能**因为生成了消解方案就把已部署规则删除**。
>
> 这条约束只靠约定是不够的——必须从**架构层面**挡住。

---

## 【二、4 条铁律 · 3 分钟】

> 我定了 **4 条铁律**：
>
> 1. **单一权威源**：每份数据有一个唯一所有者——采集层只写自己的表；翻译只写翻译的表。
> 2. **API 层是边界**：跨模块读写**必须**走对方的 API，不能直接动对方的表。
> 3. **DB 角色做"防御纵深"**：每个模块用独立 DB 用户——**就算 API 漏了，DB 也兜得住**。
> 4. **可拆分性**：今天用 1 个 DB；明天想拆成 4 个，**业务代码不用动**，只改连接串。

---

## 【三、5 个 schema · 2 分钟】

> 具体做法是 **5 个 PostgreSQL schema**：
>
> | schema | 表数 | 角色 |
> |---|---|---|
> | `intensure` | 18 | **采集层**（我做的）|
> | `translation` | 3 | 翻译层 |
> | `implementation` | 5 | 实现层（**核心权威表**：已部署的策略就在这）|
> | `assurance` | 11 | 保障层（**重点投入**）|
> | `shared` | 7 | 公共（审计 + 心跳）|
>
> 整套 DDL 加起来 **545 行**，**43 张表**。

---

## 【四、你的核心约束 · 2 分钟】

> 你之前说"翻译不能删已部署规则"。我做了**两层防护**：
>
> **API 层**：翻译只能调 `POST /api/implementation/v1/policies/{id}/retire`（语义化操作），**没有 DELETE 路径**。
>
> **DB 层**：`translation_user` 这个用户对 `implementation.policies` **只有 SELECT 权限**，没有 UPDATE/DELETE。
>
> 我现场给你演示**越权测试**：
>
> ```sql
> SET ROLE translation_user;
> DELETE FROM implementation.policies WHERE intent_id = 'test';
> ```
>
> 输出：`ERROR: permission denied for table policies` ✅
>
> **翻译就算想使坏，也删不掉线上规则**。

---

## 【五、选择写库的智慧 · 2 分钟】

> 这里有个工程问题：**全写会爆**。100 个意图每 5 秒一次一致性检查，一天 170 万行。
>
> 我用了 **A+B 混合策略**：
>
> - **A** = 时间分区基础设施（按月分区，老数据自动 DROP）
> - **B** = **选择性写库** + 心跳
>
> 具体规则：
> - `network_state` 每次都写（保障要读它）
> - `state_history` 30 秒节流 + summary 变化才写
> - `consistency_checks` **变化时 + 每 60 秒心跳**才写（省 99%）
> - `probe_results` **仅异常**才写（reachable 变 / 丢包>1% / 延迟>200ms）
>
> **算账对比**：
>
> | 方案 | 1 天 | 1 年 |
> |---|---|---|
> | 不优化 | 750 MB | 270 GB 💥 |
> | **A+B 混合** | **36 MB** | **13 GB** ✅ |
>
> 我们 VM 17G 可用空间，能撑 **1.2 年+**。

---

## 【六、保障模块 MVP · 4 分钟】

> 我写了 `assurance_mvp.py`（350 行）：
>
> - 每 5 秒读 `intensure.network_state`（当前实际状态）
> - 读 `implementation.policies`（期望状态）
> - 对每个 policy 做 reachability 一致性检查
> - 写 `assurance.consistency_checks`（变化或心跳）
> - **检测到违例 → 写 `assurance.diagnoses`**，含证据（evidence）、置信度（confidence）、受影响设备（affected_devices）

---

## 【七、09-19 关键因果 · 2 分钟】

> 这里有个**最重要的发现**（沿用上次联调的洞察）：
>
> | 场景 | down_ports | confidence | Guard 处置 |
> |---|---|---|---|
> | 空证据 | `[]` | 0.4 | **BLOCKED** |
> | 真实证据 | `["s1:eth1"]` | **0.85** | **APPROVED** |
>
> **采集层把端口真实状态填进 `down_ports`，是自愈能被放行的决定性因素**。
>
> —— 09-19 联调就证明了这条，这次 MVP 又验证了一遍。

---

## 【八、E2E 演示 · 8 分钟 · **进入终端**】

> 好，我现场跑一遍。
>
> 我有一个一键演示脚本 `demo_db_pipeline.sh`，**8 步跑完一遍 ~90 秒**。

（**打开 VM 终端，cd 进项目，跑脚本**）

> 看，启动过程：
>
> - step 0 清理环境
> - step 1 os-ken 控制器在 tmux session 'intensure' 里起来了
> - step 2 Mininet 也起来了，4 主机 1 交换机
> - step 3 host 触发了 ping，**学到 IP 了**（看，hosts=4）
> - step 4 seed 写 2 条策略
> - step 5 启动保障模块，等 8 秒看基线……
>
> **基础状态：2 个 pass，0 个 violated** —— 这是健康状态。

（**继续跑，看故障注入步骤**）

> step 6，我**注入故障**——把 h1-eth0 down 掉，模拟主机失联。
>
> 看 10 秒后的结果：
>
> - **2 个 VIOLATED** 写进了 `consistency_checks`
> - **2 个 diagnoses** 写进了 `assurance.diagnoses`
> - **confidence = 0.85**，**affected_devices = {s1}**，**down_ports = ["s1:eth1"]**
> - **first_cause = port_failure**

> step 7，恢复——h1-eth0 up。
>
> 10 秒后再看，**检查回到 pass**。
>
> **完整闭环跑通**：故障 → 检测 → 诊断 → 证据 → 恢复 → 通过 ✅

---

## 【九、DB 数据统计 · 1 分钟】

> （看 step 8 输出）
>
> 整个演示期间：
> - `assurance.consistency_checks`：**158 行**
> - `assurance.diagnoses`：**120 行**
> - `intensure.state_history`：**80 行**（30s 节流 + 变化触发生效）
> - `implementation.policies`：2 行
> - **采集层单行 upsert**：`intensure.network_state` 1 行（但版本号一直 +1 累计）

---

## 【十、版本控制 · 1 分钟】

> 我用 git 严格管理了版本：
>
> ```
> de4bc94 v3.7 Phase 1.2-1.4：保障模块 MVP + seed + E2E 演示
> 75b4331 v3.6 Phase 1.1：intensure 选择性写库
> efd55e2 v3.5 数据库架构 Phase 0：5 schema + 43 表 + 4 模块 GRANT + 保留策略
> 5b67e98 v3.4 联调脚本与文档：demo_scripts 一键演示 + 联调手册 + 项目汇报
> 1c18857 v3.3 联调增强：端口标签 s1:eth2 + OVS 详细日志接入 + safe_ping 防死锁
> ```
>
> 每次 commit 都有可运行的代码 + 验证。

---

## 【十一、未完成 · 1 分钟】

> 下一步：
>
> 1. **推送 v3.5/v3.6/v3.7 三个 commit 到 GitHub**（明天就推）
> 2. **接翻译模块**（mock 现在是空行）
> 3. **跨智能体对接文档**（找 A/B/C 同学对一遍 API 契约）
> 4. **录制演示视频**（用于 PPT）
>
> 但 MVP 核心已经跑通——**采集层持久化 + 保障模块闭环** ✅

---

## 【Q&A 准备】

### Q1: 为什么不用一个大表？

> A: 数据归属混乱；越权风险高；后期想拆模块很难拆。schema 隔离 + 角色权限 = 既能跑又能拆。

### Q2: 为什么 PostgreSQL？

> A: 项目已有这个环境；schema 原生支持；JSONB 适合存网络状态这种半结构化数据；PG 的物化视图帮保障模块优化了读路径。

### Q3: 翻译真的一点都改不了别人的表吗？

> A: **是的**。translation_user 这个数据库用户对 `implementation.policies` 只有 SELECT 权限，没有 UPDATE/DELETE。**就算代码 bug 了，越权写入会被 DB 角色挡掉**。这是你当时最关心的问题。

### Q4: 自愈怎么触发？

> A: MVP 只做了**检测**（写 consistency_checks + diagnoses）；**自愈的触发和执行**留给 Phase 2 接 assurance-agent 的自愈引擎。我们这条线 ——"采集层把真实证据传过去"——**是自愈能被放行的前提**。

### Q5: 数据真实性怎么保证？

> A: 我用 RLS（行级安全）做防御——`module_health` 表每个模块只能 UPDATE 自己的行；`audit_log` 全部 append-only、没有 UPDATE/DELETE 权限。再加上 selective write 规则（异常才写、变化才写），保证**关键事件不会被噪声淹没**。

### Q6: 一致性检查的频率？

> A: 默认 5 秒一次。状态变化时立刻写；稳定时 60 秒写一次心跳。这两个机制配合既保证实时性又控制数据量。

### Q7: 怎么和 ONOS / ODL 这些 SDN 控制器比？

> A: 我们做的是**意图层的感知**，不是流表层的控制。intensure 未来可以调用 ONOS API 来执行实际的流表操作——我们是**互补**的。

### Q8: 真实生产环境怎么跑？

> A: 同一台 VM 内。横向扩展：每个模块改成独立连接串；DB 角色权限不变；**业务代码不动**。

---

## 【汇报完成 · 30 秒】

> 总结：
>
> 1. **目标**：把 4 个智能体的数据持久化 + 约定好数据归属和接口
> 2. **方法**：5 schema + 4 模块用户 + A+B 保留策略 + 选择性写库 + 端口标签
> 3. **结果**：43 张表 + 完整 MVP 闭环跑通（故障 → 检测 → 诊断 → 证据 → 恢复 → 通过）
> 4. **未做**：自愈触发、跨智能体对接文档、录制视频
>
> 核心交付物：
>
> - `docs/database-architecture.md`（架构原则）
> - `docs/database-schema.sql`（545 行 DDL）
> - `docs/access-control-matrix.md`（权限矩阵）
> - `docs/cross-module-api-contract.md`（API 契约）
> - `docs/retention-policy.md`（保留策略）
> - `db_maintenance/retention_cleanup.sql`（自动清理）
> - `db_writer.py`（单一写入入口）
> - `assurance_mvp.py`（保障模块 MVP）
> - `demo_scripts/demo_db_pipeline.sh`（一键演示）
>
> 演示跑通，谢谢老师！
>
> （**准备 Q&A**）

---

## 应急方案

如果演示出问题了：

| 问题 | 应急 |
|---|---|
| Mininet 启动失败 | 直接 show DB 数据，讲原理（重 2+3+5 步） |
| 保障模块崩了 | 看 `tail /tmp/assurance.log`，重启（不影响演示） |
| 演示数据被清空 | 跑 `bash seed_policies.py --clear` 重置 |
| tmux 不工作 | 用 `python3 -c "..."` 直接演示代码逻辑 |
| 时间不够 | 只演示 step 5 → step 6 → step 7（健康 → 故障 → 恢复）|

---

_讲稿 v0.1 · 写于 2026-10-08 · 总时长 15-20 分钟_
_建议演练 2-3 遍后再上汇报_