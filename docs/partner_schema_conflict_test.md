# 伙伴 assurance_schema.sql 冲突实测报告

> 测试时间：2026-10-26　被测文件：`~/Downloads/assurance_schema.sql`（356 行，v1.0 2026-10-21）
> 测试方式：**独立 staging schema + 结构复刻**，真库零影响

---

## 一、安全预检（先看脚本有没有危险动作）

✅ **无任何危险语句**：无 DROP / TRUNCATE / DELETE FROM / ALTER TABLE / GRANT ALL。
全部为 `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`，本身是幂等安全的。

---

## 二、表级冲突：2 张硬冲突 + 9 张新表

| 表 | 结果 | 伙伴列数 | 我们列数 |
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

---

## 三、🔴 关键发现：直接跑会「部分成功」，然后运行时炸

因为脚本用的是 `IF NOT EXISTS`，直接对真 assurance schema 执行时：

```
psql:_partner.sql:175: NOTICE: relation "healing_intents" already exists, skipping
psql:_partner.sql:205: NOTICE: relation "healing_history" already exists, skipping
psql:_partner.sql:207: ERROR:  column "healing_id" does not exist
```

**三个后果**：

1. 2 张冲突表被**静默跳过**（只有 NOTICE，不报错）—— 看起来像成功了
2. 但第 207 行 `CREATE INDEX idx_hh_healing ON healing_history(healing_id)` **直接报错**：
   我们的 `healing_history` 有 `healing_intent_id`，**没有** `healing_id`
3. 其余 9 张表正常创建

### 所以最终状态是「半成功」

- ✅ 9 张新表建好了
- ⚠️ 2 张冲突表还是我们的旧结构（伙伴缺 16 个字段：`healing_id` / `record_id` / `strategy_name` /
  `strategy_spec` / `auto_approved` / `approved_by` / `approved_at` / `rejection_reason` /
  `has_conflict` / `conflict_details` / `is_security_baseline` / `guard_passed` / `scheduled_at` /
  `updated_at` / `history_id` / `rollback_needed` 等）
- ❌ 伙伴的服务一旦查 `healing_id` 或写这些字段 → **运行时直接报列不存在**

---

## 四、⚠️ 更要紧的：他的 SQL 文件和他的文字要求自相矛盾

**他文字上说的**（上一轮反馈）：
> 「冲突1 用你那边的结构，但是 status / priority / affected_devices / expires_at 这 4 个必须加，
> 其余（strategy_spec / guard_passed / has_conflict）可放 metadata JSONB」

**但他 SQL 文件里的实际定义**：`healing_intents` 是**他自己完整的 24 列设计**
（`healing_id` 做主键 varchar、`strategy_name`/`strategy_category`/`strategy_spec` 三件套、
`auto_approved`/`approved_by`/`approved_at`/`rejection_reason` 审批链、`has_conflict`/`conflict_details` 冲突链、
`is_security_baseline`/`guard_passed` 安全链、`scheduled_at`/`updated_at` 调度链）。

**这不是「我方结构 + 4 个字段」，而是要整个替换我方结构。**

`healing_history` 同理：他要 `history_id`(varchar 主键)、`healing_id`(varchar)、`executed_by`、
`executor_instance`、`execution_duration_ms`、`result_code`、`rollback_needed`、`rollback_executed`、
`verification_enabled`、`verified_at`、`verification_details`、`created_at` —— 和我方 11 列几乎只共享字段名不共享类型。

---

## 五、建议方案（三选一，需要他明确表态）

### 方案 A：按他自己的文字来（**推荐**）
- 他删掉 SQL 文件里 `healing_intents` / `healing_history` 两段 `CREATE TABLE`，只保留那 9 张新表
- 契约按「我方结构 + 已加的 4 个字段（status/priority/affected_devices/expires_at）」执行
- 他要的 `strategy_spec` / `guard_passed` / `has_conflict` 走 metadata JSONB
- **优点**：与我方代码、已有 19/11 列结构、分区表完全兼容；改动最小
- **代价**：他要在自己服务里做一层字段映射（写 metadata 而不是独立列）

### 方案 B：按他的 SQL 文件来（用他的完整结构）
- 我方重建 `healing_intents` / `healing_history`（改成 varchar 主键 + 他那套列）
- 我方改 `db_writer.write_healing_intent` / `write_healing_history` 适配
- **代价**：`healing_history` 的**月分区要重做**（主键从 bigint 改 varchar，外键也要跟着改）；
  我方今天的改动要回退重做；工作量 1-2 天

### 方案 C：并存（两套表）
- 他建 `healing_intents_v2` / `healing_history_v2`，我方保留原名
- **代价**：同库两套自愈表，后续对账麻烦，**不推荐**

---

## 六、可以立刻落地的一半（不需他决定）

那 **9 张新表没有冲突**，可以现在就安全落地：

- `intent_registry` / `device_inventory` / `assurance_records` / `violation_events`
- `diagnosis_results`（这是我方「不写 diagnoses」后他负责的那张，**关键**）
- `audit_logs` / `fault_patterns` / `case_records` / `_metadata`

我这边有 dry-run 工具（`demo_scripts/apply_partner_schema.py`），
只落地这 9 张、跳过 2 张冲突表，一条命令的事。

---

## 七、附：完整 dry-run 原始输出

见 `docs/partner_schema_conflict_raw.txt`
