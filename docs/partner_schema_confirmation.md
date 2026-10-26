# 给伙伴的确认回复：冲突1 / 冲突2 / healing_history 已按你的意见落地

> 日期：2026-10-26　状态：**DB 已改完并实测通过**，字段可直接用

---

## 一、冲突1：healing_intents —— 采用我方结构 + 已加你要求的 4 个字段

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| status | TEXT NOT NULL | pending | 信封生命周期。**带 CHECK 约束**：只允许 pending / approved / rejected / executed / failed，非法值直接 `violates check constraint` |
| priority | INTEGER NOT NULL | 100 | 自愈队列排序，**数值越小越先执行** |
| affected_devices | TEXT[] NOT NULL | 空数组 | 受影响设备，Guard G3 范围安全检查用 |
| expires_at | TIMESTAMPTZ（可空） | 写入时自动算 | 信封过期时间；不传则自动 now() + ttl_seconds |

同时补了 2 个索引：

- `idx_healing_intents_queue`：`(status, priority, created_at) WHERE status IN ('pending','approved')` —— 队列扫描
- `idx_healing_intents_expires`：`(expires_at) WHERE expires_at IS NOT NULL` —— 过期清扫

### ⚠️ 一个语义区别，请不要混用

原有 `verdict` 和新的 `status` 是**两个不同维度**：

- `verdict`：**Guard 的判定结果**（这个意图能不能执行）
- `status`：**信封的生命周期**（这个意图现在走到哪一步）

举例：Guard 判 `APPROVED`，但信封可能还是 `pending`（还没被实现模块接走）；执行失败后 `status='failed'`，而 `verdict` 仍是 `APPROVED`。

其余三个按你说的**放 metadata JSONB**，不单独建列：`strategy_spec` / `guard_passed` / `has_conflict`。

---

## 二、冲突2：诊断表 —— 同意职责分离，我方选择「不写 diagnoses」

| 谁 | 写什么 |
|---|---|
| **你方** | `diagnosis_results`（完整版，含 CoT 推理步骤 / LLM 审计信息）|
| **我方** | `consistency_checks`（pass / violated 状态）|
| 关联 | 两表通过 `intent_id` 关联 |

**关于我们 `diagnoses` 表重叠，我方选择：生产路径不写 `diagnoses`，以你的 `diagnosis_results` 为准。**

避免误会的说明：

- 我方 `assurance_mvp.py` 是**本地 mock**（离线无你的服务时让 demo 自己跑闭环），它本地仍会写 `diagnoses`，只用于离线演示。
- **联调/生产模式下我们不写 `diagnoses`**，诊断一律读你的 `diagnosis_results`。
- 表**不删**（保留历史数据与本地 mock 用途），但契约上不再是我方的写入职责。

这样不会出现两套诊断数据打架。

---

## 三、healing_history：分区保留，9 列清单 + 已补你需要的字段

分区完全保留（RANGE by `executed_at`，月分区 2026_10 / 11 / 12）。

### 原有 9 列

| # | 列名 | 类型 | 说明 |
|---|---|---|---|
| 1 | id | BIGINT | 行主键（与 executed_at 组成复合主键）|
| 2 | executed_at | TIMESTAMPTZ | 执行时间，**分区键** |
| 3 | healing_intent_id | UUID NOT NULL | **这就是你要的 healing_id**，外键指向 healing_intents.id |
| 4 | intent_id | TEXT NOT NULL | 原始意图 ID |
| 5 | success | BOOLEAN NOT NULL | 你要的 success ✅ |
| 6 | duration_ms | INTEGER | 执行耗时 |
| 7 | error_message | TEXT | 失败原因 |
| 8 | recovery_check_id | BIGINT | 关联 consistency_checks 的恢复校验 |
| 9 | metadata | JSONB NOT NULL | 扩展字段（默认 {}）|

### 按你要求新补 2 列

| 列名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| result_details | JSONB NOT NULL | 空对象 | 执行结果明细 → 你要的 result_details ✅ |
| verified | BOOLEAN NOT NULL | false | 恢复是否已独立校验通过 → 你要的 verified ✅ |

**你要的 4 个字段对应关系**：

- `healing_id` → 用 `healing_intent_id`（UUID）
- `success` → 已有 ✅
- `result_details` → 新增 ✅
- `verified` → 新增 ✅

---

## 四、已实测（不只是改 DDL）

在 `intensure_dev` 上跑了 12 项验证，全部通过：

- 4 个新字段写入后读回值正确（status=pending / priority=10 / affected_devices=[s1]）
- `expires_at` 不传时自动算出且未过期
- **非法 status 被 CHECK 拒绝**（bogus_status → violates check constraint）
- `healing_history` 分区表能正常写入，result_details / verified 读回正确
- 按 status + priority 能查到待执行队列（索引可用）

---

## 五、顺带说一个我们这边的 bug（已修，跟你无关但建议你自查）

我方 `db_writer.query()` 原来对空参数传了空元组给 psycopg2，导致 **SQL 里带字面量百分号的查询（如 LIKE）会静默返回空**。已修并加回归测试。

> 如果你那边也有类似封装，建议检查同样问题 —— 症状是「明明有数据，LIKE 查询却查不到」。

---

## 六、下一步

1. 以上若无异议，我方按此契约定稿
2. 你把 `assurance_schema.sql` 发我（或确认用现有契约），我在临时 schema 里 dry-run 一遍你的新表，确认无冲突再落地
3. 你的 cloudflared 隧道目前 HTTP 000，真实联调跑不起来 —— 麻烦重新拉起来，我们跑一次端到端
