# intensure 负责的表 + 操作方法（专注版 v0.2）

> **演示现场速查**：被问到"你们管哪些表 / 怎么写 / 怎么证明数据写进去了"时读这一篇
> **配套**：演示脚本 `demo_scripts/demo_db_pipeline.sh`
> **最近**：v3.9（2026-10-08，心跳默认关）
> **版本**：v0.2（2026-10-09 修订：去掉演示时间描述，改为常驻参考）

---

## 一句话总结

> 我们（intensure 采集层）**只写 8 张表**：6 张在自己的 `intensure` schema，2 张在 `shared`。**读 0 张别人的表**——读别人表的事情交给上层（保障模块）。这是有意的职责分离。

---

## 一、我们负责哪些表（写权限）

### 1.1 intensure schema（6 张，都是我们写的）

| 表 | 写频率 | 干什么 |
|---|---|---|
| `intensure.network_state` | **每 5s 一次**（单行 upsert）| 保障模块每 5s 读这张表的当前快照 |
| `intensure.state_history` | **30s 节流 + summary 变化才写** | 历史快照，用于趋势分析 |
| `intensure.port_events` | **端口状态翻转时**（up→down / down→up）| 端口变更流水（用于排障）|
| `intensure.link_events` | **LLDP 链路发现/丢失时** | 拓扑变化流水 |
| `intensure.flows_snapshot` | **未来用**（Phase 2）| 流表快照 |
| `intensure.probe_results` | **仅异常**（reachable 变 / 丢包>1% / 延迟>200ms）| 连通性探针异常记录 |

### 1.2 shared schema（2 张，我们可以写）

| 表 | 写频率 | 干什么 |
|---|---|---|
| `shared.audit_log` | **跨模块调用时**（severity 标签）| 审计日志，append-only |
| `shared.module_health` | **Phase 1 不启用**（生产环境加 `INTENSURE_HEARTBEAT=1`）| 心跳，生产环境才需要 |

---

## 二、我们读谁的表（读权限）

| 表 | 读频率 | 干什么 |
|---|---|---|
| `implementation.policies` | **不读**（这是保障模块的事） | 我们采集合不读 |

> 等等，你可能会问："我们也不读别人的表吗？"
>
> **答**：对，我们（采集层）是"只写不读"的传感器。读的事情交给上层（保障模块）。**这是有意的职责分离**。

---

## 三、操作方法（`db_writer.py` 的所有方法）

### 3.1 写方法（10 个）

| 方法 | 干啥 | 调用时机 |
|---|---|---|
| `write_network_state(payload)` | upsert 单行 | controller 每 5s 调 |
| `write_state_history_if_needed(payload, summary)` | **选择性**写：30s 节流 + summary 变化 | controller 每 5s 调（内部判断要不要真写）|
| `write_port_event(dpid, port_no, port_name, event_type)` | 端口翻转时写 | controller 收到 OpenFlow PortStatus 时调 |
| `write_link_event(src, src_port, dst, dst_port, event_type)` | LLDP 链路变化时写 | controller 收到 LLDP PacketIn 时调 |
| `write_probe_result(...)` | **直接写**（不判定）| live_env.py 直接调 |
| `write_probe_with_decision(...)` | **判定 + 写**：先 should_write_probe 决定要不要写 | live_env.py **首选这个** |
| `write_audit(module, actor, action, severity)` | 写审计日志 | 任何跨模块操作前/后 |
| `write_heartbeat(...)` | **Phase 1 不调**（生产再启用）| 未来生产监控 |
| `write_consistency_check(...)` | **保障模块用** | assurance_mvp 用 |
| `write_diagnosis(...)` | **保障模块用** | assurance_mvp 用 |
| `write_healing_intent(...)` | **未来用**（Phase 2）| 保障模块自愈 |

### 3.2 读方法（2 个）

| 方法 | 干啥 |
|---|---|
| `query(sql, params)` | 读多行，返回 `list[dict]` |
| `query_one(sql, params)` | 读单行，返回 `dict` 或 `None` |

### 3.3 选择性写规则（关键！）

| 触发条件 | 写哪个表 |
|---|---|
| 距上次 >30s **或** summary 变化 | `state_history` |
| reachable 变化 / 丢包 >1% / 延迟 >200ms / 首次 | `probe_results` |
| result 变化 / 距上次心跳 >60s / 诊断中 | `consistency_checks` |
| 端口 up→down 或 down→up | `port_events` |
| LLDP 链路发现/丢失 | `link_events` |

**不是"全部写"，是"该写才写"**。这是我们能撑住 100 个意图的关键。

---

## 四、演示时直接能跑的查询（汇报用）

### 4.1 一句话总结版（演示现场复制粘贴）

打开 psql 终端：

```bash
sudo -u postgres psql -d intensure_dev
```

#### **查询 1**（证明"我们采集合在写库"）

```sql
-- 看当前网络快照（每 5 秒被我们刷新）
SELECT 
    version,
    captured_at,
    jsonb_array_length(jsonb_path_query_array(payload, '$.switches.*.ports[*]')) AS port_count
FROM intensure.network_state;
```

**预期结果**：version 是个大数字（每 5s +1），captured_at 是几秒前。

#### **查询 2**（证明"选择性写库生效"）

```sql
SELECT 
    capture_reason,
    count(*) AS rows
FROM intensure.state_history
GROUP BY capture_reason;
```

**预期结果**：
```
 capture_reason | rows 
----------------+-----
 scheduled      |   ~3
 on-change      |   ~2
```
（30s 节流的 scheduled + 状态变化触发的 on-change）

#### **查询 3**（证明"异常才写库"—— 故障注入后会涨）

```sql
SELECT 
    is_anomaly,
    anomaly_type,
    reachable,
    packet_loss,
    probed_at
FROM intensure.probe_results
ORDER BY probed_at DESC LIMIT 5;
```

**预期结果**：每行都是异常（有 `anomaly_type`），正常的不写。

#### **查询 4**（证明"端口事件有写入"）

```sql
SELECT 
    to_char(occurred_at, 'HH24:MI:SS') AS time,
    dpid,
    port_name,
    event_type
FROM intensure.port_events
ORDER BY id DESC LIMIT 10;
```

#### **查询 5**（证明"链路事件有写入"）

```sql
SELECT 
    to_char(occurred_at, 'HH24:MI:SS') AS time,
    src_dpid || ':' || src_port || ' <-> ' || dst_dpid || ':' || dst_port AS link,
    event_type
FROM intensure.link_events
ORDER BY id DESC LIMIT 10;
```

#### **查询 6**（证明"审计日志可查"）

```sql
SELECT 
    to_char(occurred_at, 'HH24:MI:SS') AS time,
    module_name,
    severity,
    action
FROM shared.audit_log
ORDER BY id DESC LIMIT 10;
```

#### **查询 7**（证明"我们（intensure）写了心跳，生产环境才看得到"）

```sql
-- Phase 1 不写心跳（默认 disabled）。生产时 INTENSURE_HEARTBEAT=1 才会有数据。
-- 这里只看其他模块的（如有）
SELECT * FROM shared.module_health;
```

#### **查询 8**（最重磅：证明保障模块用了我们写的数据）

> 这条不在我们表里，但能证明我们表里的 `down_ports` 真的"有用"

```sql
SELECT 
    intent_id,
    confidence,
    affected_devices,
    evidence->'down_ports' AS down_ports_evidence
FROM assurance.diagnoses
ORDER BY diagnosed_at DESC LIMIT 3;
```

**预期结果**：故障注入后 confidence=0.85, down_ports=["s1:eth1"]。

### 4.2 一键汇总查询（演示结束用）

```sql
SELECT 'intensure.network_state'     AS tbl, count(*) FROM intensure.network_state
UNION ALL SELECT 'intensure.state_history', count(*) FROM intensure.state_history
UNION ALL SELECT 'intensure.port_events',    count(*) FROM intensure.port_events
UNION ALL SELECT 'intensure.link_events',    count(*) FROM intensure.link_events
UNION ALL SELECT 'intensure.probe_results',  count(*) FROM intensure.probe_results
UNION ALL SELECT 'shared.audit_log',         count(*) FROM shared.audit_log
ORDER BY 1;
```

**预期结果**：每张表都有行数（除了 heartbeat 因为我们关了），证明"数据真的写进去了"。

---

## 五、被问"我们不写什么表"时的标准答案

老师可能问："你们为什么不写 `assurance.consistency_checks`？"

> "**职责分离**——我们（intensure）是'眼睛'，只负责采集；写 `consistency_checks` 是保障模块的事。09-19 联调的关键发现是：**我们必须把真实 `down_ports` 写进我们的表**，保障模块才能基于这些真实证据做出高 confidence 诊断。这是**层与层之间的契约**。"

---

## 六、汇报后可能被问到的"问题清单"

| Q | 答案 |
|---|---|
| 你们为什么不用一个大表？ | 数据归属混乱；越权风险高；后期想拆模块很难拆 |
| 数据库多大？会爆吗？ | 不写心跳后，100 意图 1 年 ≈ 13 GB，VM 17G 可用撑 1.2 年+ |
| 翻译怎么改我们的表？ | **改不了** —— 我们的用户只 SELECT 别人的表，翻译也只能 SELECT |
| 心跳怎么关？ | 默认关；生产环境启动时加 `INTENSURE_HEARTBEAT=1` 即可 |
| 自愈怎么触发？ | 这次 MVP 只做检测；自愈留待 Phase 2 接实现模块 |

---

_写于 2026-10-08 (v0.1) · 2026-10-09 (v0.2 修订) · Phase 1 MVP 完成_
_配套：demo_runbook.md / database-architecture.md_
_常驻参考：被问"管哪些表 / 怎么写 / 怎么证明写进去了"时读这一篇就够 💎_