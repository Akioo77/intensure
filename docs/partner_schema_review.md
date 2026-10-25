# 给伙伴的回复：assurance_schema.sql 落地前的冲突核对

> 发出方：intensure 采集层（孙晓萱）
> 日期：2026-10-25
> 状态：**脚本还没执行**，先对齐两个硬冲突再落地

---

## 一、连接信息确认（回答密码问题）

`assurance_user` **有密码**，已实测能连上（对照组：错密码会报 `password authentication failed`）。

```
postgresql://assurance_user:assurance_dev_pwd@127.0.0.1:5432/intensure_dev
```

密码是我们 `docs/access-control-grants.sql` 里统一设的，和另外三个模块账号同规格：
`intensure_dev_pwd` / `translation_dev_pwd` / `implementation_dev_pwd` / `assurance_dev_pwd`。

⚠️ **注意 `127.0.0.1` 只在你本机成立**。我们的 PG 跑在 Colima 里，只监听本机回环；
你从自己机器连需要走隧道（`ssh -L 5432:127.0.0.1:5432`）或换成可达地址，
不能直接用 `127.0.0.1`。

---

## 二、🔴 两个硬冲突：表名撞了，且结构不一样

你列的 11 张表里，有 2 张**我们这边已经建好了**（v3.5 数据库架构时就建了），同名但结构不同：

### 冲突 1：`assurance.healing_intents`

| | 我们的结构 | 你的结构 |
|---|---|---|
| 列数 | **14 列** | 3 列 |
| 主键 | `id uuid` | `id uuid`（一致） |
| 我们独有 | `created_at` `diagnosis_id` `parent_intent_id` `strategy` `primitives` `risk` `guard_checks` `verdict` `blocked_by` `ttl_seconds` `executed_at` `execution_result` `metadata` | — |
| 你独有 | — | `action_payload jsonb` |

我们的字段不是冗余，是**自愈意图协议本体**：
- `strategy` + `primitives` = 用什么策略、拆成哪些可执行原子动作
- `guard_checks` = 执行前的安全校验（这正是 09-19 联调定下来的契约）
- `verdict` + `blocked_by` = 判定结果与被拦截原因
- `parent_intent_id` = 关联原始意图（自动自愈意图的溯源链）

你的 `action_payload` 语义上等于我们的 `primitives jsonb` + `guard_checks jsonb`。

### 冲突 2：`assurance.healing_history`

| | 我们的结构 | 你的结构 |
|---|---|---|
| 表类型 | **分区表**（RANGE 按 `executed_at` 月分区） | 普通表 |
| 列数 | 9 列 | 3 列 |
| 我们独有 | `intent_id` `success` `duration_ms` `error_message` `recovery_check_id` `metadata` | — |

⚠️ **分区这件事不能退**：自愈历史是高频写入表，不分区会随演示次数无限膨胀，
而且我们已有月分区 `healing_history_2026_10/11/12` 和配套的保留策略。

---

## 三、🟡 4 组语义重叠（不同名，同一职责）

这些不会撞表名，但**职责重复**，需要你确认用哪套：

| 你的表 | 我们的表 | 重叠点 |
|---|---|---|
| `diagnosis_results` | `assurance.diagnoses` | 都是诊断结果。我们已有 `root_causes` `confidence` `affected_devices` `evidence` |
| `violation_events` | `assurance.consistency_checks` | 我们用 `status` 区分 `pass`/`violated`，事件流合一 |
| `audit_logs` | `shared.audit_log` | 跨模块审计流水，在 `shared` 而非 `assurance`，且 append-only + 月分区 + 错误归档 |
| `intent_registry` | `assurance.intent_states` | 意图注册 vs 意图当前状态，我们存 `current_status` + `last_status_at` |

---

## 四、需要你确认的 3 件事

**Q1. `healing_intents` / `healing_history` 用哪套结构？**
- A. **用我们的**（推荐）——你的模块改读我们的字段，`action_payload` 映射到 `primitives` + `guard_checks`
- B. **用你的**——我们改代码适配，但分区 + 13 个字段会丢，联调契约要重谈
- C. **改名并存**——你的改成 `healing_intents_v2` 之类，但同库两张自愈表，维护成本高

**Q2. `diagnosis_results` 和我们的 `diagnoses` 怎么分工？**
是「你写诊断结果、我写一致性检查」这种职责分离，还是你想合并成一张？

**Q3. 脚本里的 `GRANT` 要不要解开？**
你写的是「已注释，按需决定」。建议**解开但只给 `assurance_user` 授读写**，
不要 `GRANT ALL`——我们这边是按模块隔离权限的设计：
`assurance_user` 现在对 `intensure.*` 只有 SELECT，对 `implementation.policies` 只有 SELECT。

---

## 五、我们这边已经准备好的东西

你把脚本文件发我（微信/邮件/GitHub 都行），我这边有工具可以：

1. **先在临时 schema 里跑一遍**（不动真库），自动输出：
   - 哪几张是新表、哪几张和我们的撞名
   - 撞名表的**列级差异**（我们有哪些列、他有哪些列、谁缺什么）
2. 你确认后再真落地（落地前自动 `pg_dump` 备份）
3. 落地后自动补 `GRANT`（按模块隔离口径）

**你只需要发文件，不需要自己跑 SQL** —— 你那边没有我们的库实例。

---

## 六、顺带提两个状态

1. `assurance_user` 权限已验证：
   可写 `assurance.consistency_checks` / `diagnoses` / `healing_intents` / `healing_history` / `intent_states`；
   可读 `intensure.*`（含 `network_state` / `state_history` / `port_events` / `link_events`）、
   `implementation.policies`、`translation.intent_templates`；
   可写 `shared.audit_log`（仅 INSERT）和自己的 `shared.module_health` 行。

2. 你的 `cloudflared` 隧道域名当前解析失败（HTTP 000），
   所以真实联调还跑不起来。落地 schema 之后，需要你把隧道重新拉起来，
   我们再跑一遍端到端（`demo_scripts/01-08.sh`）。