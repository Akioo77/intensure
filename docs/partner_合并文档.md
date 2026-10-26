# 给伙伴：字段确认 + 冲突实测（合并版）

> 日期：2026-10-26　发起：intensure 采集层（孙晓萱）
> 状态：**DB 侧已按你的要求改完并实测；你的 SQL 我们做了临时冲突测试，有 1 个必须先解决的问题**

---

## 摘要（先看结论）

| 项 | 结论 |
|---|---|
| 冲突1 `healing_intents` | ✅ 采用我方结构，你要求的 4 个字段**已加并实测写入成功** |
| 冲突2 诊断表 | ✅ 职责分离：你写 `diagnosis_results`，我方写 `consistency_checks`；**生产路径我方不写 `diagnoses`** |
| `healing_history` | ✅ 月分区保留，已补你要的 `result_details` + `verified` |
| 你的 SQL 脚本 | ⚠️ 安全（无 DROP/DELETE/ALTER），但有 **2 张表硬冲突 + 1 条索引会报错** |
| **需要你决策** | 🔴 你的**文字要求**和**SQL 文件**不一致，见第五节，请选 A/B/C |

---

## 一、冲突1：`healing_intents` —— 用我方结构 + 4 个字段已加（实测通过）

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `status` | TEXT NOT NULL | `'pending'` | 信封生命周期。**带 CHECK 约束**：只允许 `pending`/`approved`/`rejected`/`executed`/`failed`，非法值直接 `violates check constraint` |
| `priority` | INTEGER NOT NULL | `100` | 自愈队列排序，**数值越小越先执行** |
| `affected_devices` | TEXT[] NOT NULL | `'{}'` | 受影响设备，Guard G3 范围安全检查用 |
| `expires_at` | TIMESTAMPTZ | 写入时不传则自动 `now() + ttl_seconds` | 信封过期时间 |

另补 2 个索引：

- `idx_healing_intents_queue` → `(status, priority, created_at) WHERE status IN ('pending','approved')` —— 队列扫描
- `idx_healing_intents_expires` → `(expires_at) WHERE expires_at IS NOT NULL` —— 过期清扫

其余按你说的**放 metadata JSONB**，不单独建列：`strategy_spec` / `guard_passed` / `has_conflict`。

### ⚠️ 一个语义区别，请不要混用

原有 `verdict` 和新的 `status` 是**两个不同维度**：

- `verdict`：**Guard 的判定结果**（这个意图能不能执行：APPROVED / BLOCKED / PENDING）
- `status`：**信封的生命周期**（这个意图现在走到哪一步：pending / approved / rejected / executed / failed）

举例：Guard 判 `APPROVED`，但信封可能还是 `pending`（还没被实现模块接走）；执行失败后 `status='failed'` 而 `verdict` 仍是 `APPROVED`。

---

## 二、冲突2：诊断表 —— 职责分离，我方选择「不写 diagnoses」

| 谁 | 写什么 |
|---|---|
| **你方** | `diagnosis_results`（完整版，含 CoT 推理步骤 / LLM 审计信息）|
| **我方** | `consistency_checks`（pass / violated 状态）|
| 关联方式 | 两表通过 `intent_id` 关联 |

**关于我方 `diagnoses` 与你 `diagnosis_results` 重叠的问题，我方选择：生产路径不写 `diagnoses`，以你的 `diagnosis_results` 为准。**

避免误会的说明：

- 我方 `assurance_mvp.py` 是**本地 mock**（离线无你的服务时让 demo 自己跑完整闭环），它本地仍会写 `diagnoses`，仅用于离线演示。
- **联调/生产模式下我方不写 `diagnoses`**，诊断一律读你的 `diagnosis_results`。
- 表**不删**（保留历史数据与本地 mock 用途），但契约上不再是我方的写入职责。

这样不会出现两套诊断数据打架。

---

## 三、`healing_history`：月分区保留，9 列清单 + 已补你要的字段

分区完全保留（RANGE by `executed_at`，月分区 2026_10 / 11 / 12）。

### 原有 9 列

| # | 列名 | 类型 | 说明 |
|---|---|---|---|
| 1 | `id` | BIGINT | 行主键（与 executed_at 组成复合主键）|
| 2 | `executed_at` | TIMESTAMPTZ | 执行时间，**分区键** |
| 3 | `healing_intent_id` | UUID NOT NULL | **这就是你要的 `healing_id`**，外键指向 `healing_intents.id` |
| 4 | `intent_id` | TEXT NOT NULL | 原始意图 ID |
| 5 | `success` | BOOLEAN NOT NULL | 你要的 `success` ✅ |
| 6 | `duration_ms` | INTEGER | 执行耗时 |
| 7 | `error_message` | TEXT | 失败原因 |
| 8 | `recovery_check_id` | BIGINT | 关联 `consistency_checks` 的恢复校验 |
| 9 | `metadata` | JSONB NOT NULL | 扩展字段（默认空对象）|

### 按你要求新补 2 列

| 列名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `result_details` | JSONB NOT NULL | 空对象 | 执行结果明细 → 你要的 `result_details` ✅ |
| `verified` | BOOLEAN NOT NULL | false | 恢复是否已独立校验通过 → 你要的 `verified` ✅ |

**你要的 4 个字段对应关系**：

| 你要的 | 我方对应 |
|---|---|
| `healing_id` | `healing_intent_id`（UUID）|
| `success` | `success`（已有）✅ |
| `result_details` | `result_details`（新增）✅ |
| `verified` | `verified`（新增）✅ |

---

## 四、你的 SQL 脚本冲突实测（在独立 schema 里跑，真库零影响）

### 4.1 安全预检

✅ **无任何危险语句**（无 DROP / TRUNCATE / DELETE FROM / ALTER TABLE / GRANT ALL），
全部 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`，幂等安全。

### 4.2 表级分类：2 张硬冲突 + 9 张新表

| 表 | 结果 | 你的列数 | 我方列数 |
|---|---|---|---|
| `healing_intents` | 🔴 **硬冲突** | 24 | 19 |
| `healing_history` | 🔴 **硬冲突** | 17 | 11 |
| `intent_registry` | 🟢 新表 | 12 | — |
| `device_inventory` | 🟢 新表 | 12 | — |
| `assurance_records` | 🟢 新表 | 5 | — |
| `violation_events` | 🟢 新表 | 12 | — |
| `diagnosis_results` | 🟢 新表 | 19 | — |
| `audit_logs` | 🟢 新表 | 5 | — |
| `fault_patterns` | 🟢 新表 | 15 | — |
| `case_records` | 🟢 新表 | 17 | — |
| `_metadata` | 🟢 新表 | 2 | — |

### 4.3 🔴 直接跑会「半成功」，然后在运行时炸

因为用 `IF NOT EXISTS`，直接对真库执行时：

```
NOTICE: relation "healing_intents" already exists, skipping
NOTICE: relation "healing_history" already exists, skipping
ERROR:  column "healing_id" does not exist        ← 第 207 行
```

后果链条：

1. 2 张冲突表被**静默跳过**（只有 NOTICE，看着像成功）
2. 但第 207 行 `CREATE INDEX idx_hh_healing ON healing_history(healing_id)` **直接报错** ——
   我方列名是 `healing_intent_id`，没有 `healing_id`
3. 其余 9 张表正常创建

**最终是「半成功」**：9 张新表建好，2 张冲突表仍是旧结构，你缺 16 个字段
（`healing_id` / `record_id` / `strategy_name` / `strategy_spec` / `auto_approved` / `approved_by` /
`approved_at` / `rejection_reason` / `has_conflict` / `conflict_details` / `is_security_baseline` /
`guard_passed` / `scheduled_at` / `updated_at` / `history_id` / `rollback_needed` 等）。

**你的服务一查 `healing_id` 就会运行时炸，而且是在演示现场才发现。**

---

## 五、🔴 需要你决策：你的文字要求和 SQL 文件不一致

**你上一轮文字说的**：
> 「冲突1 用你那边的结构，但是 status / priority / affected_devices / expires_at 这 4 个必须加，
> 其余（strategy_spec / guard_passed / has_conflict）可放 metadata JSONB」

**但你 SQL 文件里的实际定义**：`healing_intents` 是**你自己的完整 24 列设计** ——
`healing_id` 做主键 varchar、`strategy_name`/`strategy_category`/`strategy_spec` 三件套、
`auto_approved`/`approved_by`/`approved_at`/`rejection_reason` 审批链、
`has_conflict`/`conflict_details` 冲突链、`is_security_baseline`/`guard_passed` 安全链、
`scheduled_at`/`updated_at` 调度链。

**这不是「我方结构 + 4 个字段」，而是要整表替换我方结构。**

`healing_history` 同理：你要 `history_id`(varchar 主键)、`healing_id`(varchar)、`executed_by`、
`executor_instance`、`execution_duration_ms`、`result_code`、`rollback_needed`、`rollback_executed`、
`verification_enabled`、`verified_at`、`verification_details`、`created_at` ——
和我方 11 列几乎**只共享字段名、不共享类型**。

### 三个方案，请选一个

#### 方案 A：按你的文字来（**我方推荐**）

- 你删掉 SQL 里 `healing_intents` / `healing_history` 两段 `CREATE TABLE`，只保留那 9 张新表
- 契约按「我方结构 + 已加的 4 个字段」执行；`strategy_spec` / `guard_passed` / `has_conflict` 走 metadata JSONB
- **优点**：与我方代码、已有 19/11 列结构、月分区完全兼容；改动最小
- **代价**：你需要在服务里做一层字段映射（写 metadata 而不是独立列）

#### 方案 B：按你的 SQL 来（用你的完整结构）

- 我方重建 `healing_intents` / `healing_history`（改成 varchar 主键 + 你那套列）
- 我方改 `db_writer.write_healing_intent` / `write_healing_history` 适配
- **代价**：`healing_history` 的**月分区要重做**（主键 bigint→varchar，外键跟着改）；
  我方今天的改动要回退重做；工作量 **1-2 天**

#### 方案 C：并存两套表

- 你建 `healing_intents_v2` / `healing_history_v2`，我方保留原名
- **代价**：同库两套自愈表，后续对账麻烦，**不推荐**

---

## 六、可立刻落地的一半（不需要你决策）

那 **9 张新表零冲突**，我方可以现在就安全落地：

- `intent_registry` / `device_inventory` / `assurance_records` / `violation_events`
- **`diagnosis_results`**（这是「我方不写 diagnoses」后你负责的那张，关键）
- `audit_logs` / `fault_patterns` / `case_records` / `_metadata`

> 我方有 dry-run 工具，只落地这 9 张、跳过 2 张冲突表，一条命令的事。落地前会先 pg_dump 备份。
> **你确认后我就落。**

---

## 七、附：我方实测证据（12 项全过）

在 `intensure_dev` 上跑的实际验证，不是只改了 DDL：

- 4 个新字段写入后读回值正确（`status=pending` / `priority=10` / `affected_devices=['s1']`）
- `expires_at` 不传时自动算出且未过期
- **非法 `status` 被 CHECK 拒绝**（`bogus_status` → `violates check constraint`）
- `healing_history` 分区表能正常写入，`result_details` / `verified` 读回正确
- 按 `status + priority` 能查到待执行队列（索引可用）

---

## 八、顺带提醒一个 bug（我方已修，建议你自查）

我方 `db_writer.query()` 原来对空参数传了空元组给 psycopg2，导致 **SQL 里带字面量百分号的查询（如 LIKE）会静默返回空**。
已修并加回归测试。

> 如果你那边也有类似封装，建议检查同样问题 —— 症状是「明明有数据，LIKE 查询却查不到」。

---

## 九、下一步

1. **请选 A / B / C**（第五节），尤其是两张冲突表怎么处理
2. 若选 A：我方立刻落地那 9 张新表，你按 metadata 映射改服务
3. 你的 **cloudflared 隧道目前 HTTP 000**，真实联调还跑不起来 —— 麻烦重新拉起来，我们跑一次端到端
