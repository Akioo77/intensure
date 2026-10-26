# 数据保留与写库策略 v0.1

> **范围**：意图驱动网络管理系统 4 智能体的 PostgreSQL 数据保留 + 写库触发规则
> **策略**：**A+B 混合方案**（时间分区 + 选择性写库 + 心跳）
> **批准时间**：2026-10-08
> **配套**：`docs/database-schema.sql`（已加选择性写库字段）、`db_maintenance/retention_cleanup.sql`

---

## 0. 为什么需要这策略

**核心问题**：如果不做保留策略，DB 会在数月内爆掉。

假设 100 个意图、一致性检查每 5 秒一次：
- 不做策略：`consistency_checks` 每天 1,728,000 行 ≈ 400 MB/天 → 1 年 150 GB
  - ✅ **实测校准（2026-10-26 压测）**：单行 **279 B** → 482 MB/天 → **176 GB/年**（比原估算高 17%，估算方法正确）
- VM 才 17G 可用空间 → **撑不到 2 个月**

**A+B 混合方案**：
- **A = 时间分区基础设施**（pg_partman 自动 DROP 老分区）
- **B = 选择性写库 + 心跳**（只写"重要"数据，挡掉 95%+ 噪声）

---

## 1. 完整写库策略矩阵

### 1.1 intensure schema（采集层）

| 表 | 写入触发条件 | 保留时长 | 工具 |
|---|---|---|---|
| `network_state` | 每次采集（单行 upsert） | **永久**（单行，永远只 1 行） | 单行表 |
| `state_history` | **每 30s 一次 OR 状态变化时**（避免每秒累积） | 7 天 | pg_partman |
| `port_events` | 仅变化（已有）| 30 天 | pg_partman |
| `link_events` | 仅变化（已有）| 30 天 | pg_partman |
| `flows_snapshot` | **仅变化时**（flows 表稳定 → 不写）| 7 天 | pg_partman |
| `probe_results` | **仅异常时**（reachable 变化 / 丢包>1% / 延迟>200ms）| 30 天 | pg_partman |

### 1.2 assurance schema（保障层）

| 表 | 写入触发条件 | 保留时长 | 工具 |
|---|---|---|---|
| `intent_states` | 每次状态变化（REGISTERED→ACTIVE→...）| 365 天 | 无分区 |
| `consistency_checks` | **状态变化 OR 每 60s 心跳**| 30 天 | pg_partman |
| `diagnoses` | 仅检测到违例时 | 365 天 | 无分区 |
| `healing_intents` | 仅产生自愈意图时 | 365 天 | 无分区 |
| `healing_history` | 自愈执行完成时 | 365 天 | pg_partman |

### 1.3 shared schema（公共）

| 表 | 写入触发条件 | 保留时长 | 工具 |
|---|---|---|---|
| `audit_log` | 全部写，**标 severity**（INFO/WARN/ERROR）| 30 天 INFO+WARN / **永久** ERROR | pg_partman + 手动归档 |
| `module_health` | 每次心跳（每 5s）| 7 天（只保留最新即可，旧数据无用）| 单表清理 |
| `schema_version` | 每次 migration | 永久 | 单表 |

### 1.4 translation / implementation schema

- **低频写**，暂无保留压力
- 暂不分区；后期如果写入频率上升再加

---

## 2. 选择性写库的实现规则

### 2.1 `probe_results`：异常才写

**判定"异常"**（任一满足）：
- `reachable` 从 true→false 或 false→true（变化）
- `packet_loss > 0.01`（1% 阈值）
- `latency_ms > 200`（200ms 阈值，可配）

**实现位置**：`live_env.py`（probe 主循环）

```python
prev_state = {'reachable': None, 'latency': None}
def should_write_probe(reachable, loss, latency, prev):
    if prev['reachable'] is None: return True  # 首次
    if reachable != prev['reachable']: return True  # 变化
    if loss > 0.01: return True  # 丢包超阈
    if latency and latency > 200: return True  # 延迟超阈
    return False
```

### 2.2 `consistency_checks`：状态变化 + 心跳

**判定"应该写"**（任一满足）：
- 状态从 `pass` → `violated` 或反之
- 距上次写入 > 60s（心跳）
- 当前正处于 `diagnosing` 或 `healing` 状态（高频）

**实现位置**：保障模块的循环检查

### 2.3 `state_history`：30s 节流 + 变化触发

**判定**：
- 距上次采集 > 30s **OR**
- 状态摘要有变化（switch 数 / host 数 / 链路数变了）

**实现位置**：`controller_app.py`（采集循环）

### 2.4 `audit_log`：全部写，但带 severity

| 场景 | severity |
|---|---|
| 正常读写 | INFO |
| 失败重试 / 校验失败 | WARN |
| 异常错误 / 越权尝试 | **ERROR**（永久保留）|

---

## 3. pg_partman 配置

### 3.1 安装（Ubuntu 24.04）

```bash
sudo apt install -y postgresql-16-partman
# 或（如果 16-partman 不存在）
sudo apt install -y postgresql-partman
```

### 3.2 启用扩展

```sql
-- 在数据库内执行
CREATE EXTENSION pg_partman;

-- 需要 preconfig：在 postgresql.conf 加
-- shared_preload_libraries = 'pg_partman_bgw'
-- 然后 SELECT partman.create_parent(...) 创建分区管理
```

### 3.3 给每个时间分区表配置

```sql
-- 示例：probe_results
SELECT partman.create_parent(
    p_parent_table := 'intensure.probe_results',
    p_control     := 'probed_at',
    p_type        := 'range',
    p_interval    := 'monthly',
    p_premake     := 4    -- 预创建 4 个月
);
UPDATE partman.part_config
SET retention = '30 days',
    retention_keep_table = false   -- true = detach 保留表；false = DROP
WHERE parent_table = 'intensure.probe_results';
```

**所有需要 pg_partman 管的表**（DDL 中 `PARTITION BY RANGE` 的）：
- intensure.state_history
- intensure.port_events
- intensure.link_events
- intensure.probe_results
- intensure.flows_snapshot（虽然 DDL 是非分区，但可以考虑改成分区）
- assurance.consistency_checks
- assurance.healing_history
- shared.audit_log

### 3.4 启用后台 worker

`postgresql.conf`:
```
shared_preload_libraries = 'pg_partman_bgw'
```

或 SQL 里手动跑：
```sql
UPDATE partman.part_config SET automatic_maintenance = 'on';
```

`cron` 手动调（更可控）：
```bash
# /etc/cron.d/postgresql-partman
0 3 * * * postgres /usr/bin/psql -d intensure_dev -c "SELECT partman.run_maintenance('intensure.probe_results');" >/dev/null 2>&1
```

---

## 4. 容量预估（100 意图假设）

| 表 | 全写策略（1天）| A+B 混合策略（1天） | 节省 |
|---|---|---|---|
| probe_results | 17,280 行（4 MB）| ~500 行（0.1 MB）| **97%** |
| consistency_checks | 1,728,000 行（400 MB）| ~144,000 行（40 MB）| **92%** |
| state_history | 86,400 行（43 MB）| ~2,880 行（1.5 MB）| **96%** |
| audit_log | 1,000,000 行（300 MB）| ~100,000 行（30 MB）| 90% |
| **合计/天** | **~750 MB** | **~46 MB** | **94%** |

**新预估**：100 意图 × 1 年 ≈ **17 GB**（实测修正，原估 13 GB）
  > ⚠️ **修正说明（2026-10-26 压测实测）**：60s 心跳 × 100 意图 = 14.4 万行/天，
> 这是节流策略的**地板值**，省不掉（原估 17,000 行/天低估了约 8.5 倍）。
> 要压写入量只能**调心跳间隔**（60s→300s 直接降到 2.9 万行/天）。
> 另：故障期 `is_diagnosing=True` 会强制每次都写，写放大翻倍（实测 213 写/分 vs 稳态 100 写/分），
> **容量规划要按故障态算，不是稳态**。
**实际空间**：VM 17G 可用 → 17 GB/年 ≈ **够用 1 年**（实测修正后；原估 13 GB 时说 1.2 年+）
> 前提是**保留策略生效**（`consistency_checks` 30 天 → 稳态占用约 1.2 GB，远低于 17 GB）

如果意图数 > 500 → 启动 TimescaleDB 替代。

---

## 5. 清理脚本（不依赖 pg_partman 也能跑）

`db_maintenance/retention_cleanup.sql`：

```sql
-- 手动 DROP 30 天前的 probe_results 分区
DO $$
DECLARE
    part RECORD;
BEGIN
    FOR part IN
        SELECT inhrelid::regclass::text AS partition_name
        FROM pg_inherits
        WHERE inhparent = 'intensure.probe_results'::regclass
          AND pg_get_expr(relpartbound, oid) LIKE '%FOR VALUES FROM%'
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS %s', part.partition_name);
    END LOOP;
END $$;
```

更稳的版本按 `pg_partition_tree` 遍历并解析 range，按时间判断。

---

## 6. 监控 & 告警

每 7 天检查一次：

```sql
SELECT
    schemaname || '.' || tablename AS table_name,
    pg_total_relation_size(schemaname || '.' || tablename) AS size_bytes,
    pg_size_pretty(pg_total_relation_size(schemaname || '.' || tablename)) AS size
FROM pg_tables
WHERE schemaname IN ('intensure', 'translation', 'implementation', 'assurance', 'shared')
ORDER BY pg_total_relation_size(schemaname || '.' || tablename) DESC
LIMIT 20;
```

**告警阈值**：
- 单表 > 1 GB → warning
- 总 DB > 10 GB → critical（开始归档）
- 总 DB > 14 GB → DROP 老分区（无论保留期）

---

## 7. 当前未决（Phase 1 不阻塞）

- [ ] audit_log ERROR 永久保留的具体归档策略（S3? 文件？）
- [ ] consistency_checks 心跳频率（60s 是否合适？）
- [ ] probe_results 异常阈值（200ms 是否合理？需要观察）

---

_文档创建：2026-10-08 · v0.1 · 已批准（主人 2026-10-08 05:20 拍板 A+B 混合）_

---

## 附录：2026-10-26 压测实测（数据来源）

完整报告：`docs/load_test_results.md`（工具 `demo_scripts/load_test.py`）

| 项 | 最坏上界（10 分钟）| 真实模式（3 分钟）|
|---|---|---|
| 意图数 | 100 | 100 |
| 写入量 | 13,431 次 | 1,047 次 |
| 吞吐 | 22.3 写/秒 | 5.8 写/秒 |
| 写延迟 p50 / p99 | 35.88 / 614.54 ms | 18.39 / 425.16 ms |
| 进程 CPU 平均 / 峰值 | 2.4% / 28.3% | 1.3% / 26.2% |
| 进程 RSS | 30.6 MB 恒定 | 30.8 MB 恒定 |
| PG 连接数峰值 | 1 | 1 |
| 锁等待 | **0** | **0** |
| 错误 | **0** | **0** |

实测单行字节：`consistency_checks` **279 B**、`diagnoses` **426 B**、`state_history` **2,201 B**。

> 两种模式的区别：真实模式走 `should_write_consistency()`（变更检测 + 60s 心跳），
> 与线上 `assurance_mvp.py:382` 一致；最坏模式每 intent 每周期都写，是上界。
