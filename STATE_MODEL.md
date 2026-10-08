# 统一状态模型 · 跨智能体对接文档

> 意图保障智能体 · Part A（状态采集与数字化网络状态）对外输出契约
> 版本：v1.0（2026-08-05）｜作者：同学 A

---

## 1. 文档目的

本系统（意图保障 · Part A）负责**"看见网络"**：持续采集 Mininet 园区网络的真实运行状态，统一成 JSON 模型，并提供查询接口。

这份文档定义：
- **统一状态模型**（实际运行状态）的 JSON Schema —— 供意图保障智能体内部使用，也供其他智能体读取"网络现状"
- **意图模型**（期望状态）的推荐 Schema —— 供意图翻译智能体输出、意图实现智能体消费
- **一致性检验接口** —— 意图保障智能体如何用"实际状态"校验"期望状态"（自愈触发依据）

**三个智能体的数据流：**

```
用户意图
   │  自然语言
   ▼
[意图翻译智能体] ──意图 JSON (期望状态)──► [意图实现智能体] ──配置下发──► 网络
   ▲                                              │
   │       一致性检验 (期望 vs 实际)                │ 下发结果
   │                                              ▼
[意图保障智能体] ◄──────────── 实际运行状态 (本系统) ─────────── 网络
   │  状态采集 / 历史 / 可视化
   └─ 发现偏差 → 生成"自愈意图" → 反馈给翻译/实现智能体
```

---

## 2. 运行环境

| 项 | 值 |
|---|---|
| 网络模拟 | Mininet 2.3.0 + OVS 3.3.4 |
| SDN 控制器 | os-ken 2.8.1（Ryu 官方继任者，语义等价） |
| 拓扑 | 4 主机（h1-h4，10.0.0.1-4/24）+ 2 交换机（s1、s2）线性 |
| REST API | `http://<host>:8080`（VM 内） |
| 状态文件 | `/tmp/network_state.json`（原子写，最新快照） |
| 历史文件 | `/tmp/network_history.json`（环形缓冲 200 条 ≈ 16 分钟） |

拓扑：`h1、h3 ── s1 ── s2 ── h2、h4`（s1-s2 为 5ms 链路）

---

## 3. 统一状态模型（实际状态）— `GET /api/state`

顶层结构：

```json
{
  "meta":        { "...": "控制器元信息" },
  "switches":    { "dpid_hex": { "...": "交换机及端口描述" } },
  "hosts":       { "mac":     { "...": "主机及位置/IP" } },
  "port_stats":  { "dpid_hex": [ { "...": "端口计数器" } ] },
  "flows":       { "dpid_hex": [ { "...": "流表条目" } ] },
  "events":      [ { "...": "最近事件 (FIFO 500)" } ]
}
```

### 3.1 meta

```json
{
  "start_time": 1785913917.27,
  "last_update": 1785914273.47,
  "update_count": 414,
  "controller_pid": 9677
}
```

### 3.2 switches（交换机 + 端口描述）

```json
{
  "0000000000000001": {
    "dpid": "0000000000000001",
    "connected_at": 1785913917.4,
    "num_ports": 4,
    "disconnected_at": null,
    "ports": [
      { "port_no": 4294967294, "hw_addr": "6e:4b:ca:f0:d3:48", "name": "s1",
        "state": 1, "config": 0, "curr_speed": 0, "max_speed": 0 },
      { "port_no": 1, "hw_addr": "ba:31:db:a8:9f:30", "name": "s1-eth1",
        "state": 4, "config": 0, "curr_speed": 0, "max_speed": 0 }
    ]
  }
}
```

**端口 state 位（OpenFlow 1.3 OFPPS）：**

| bit | 值 | 含义 |
|---|---|---|
| 0 | 1 | LINK_DOWN（链路断） |
| 1 | 2 | BLOCKED（STP 阻塞） |
| 2 | 4 | LIVE（链路存活，正常） |

> ⚠ `port_no=4294967294`（0xfffffffe）= **OFPP_LOCAL 本地管理口**，永远 LINK_DOWN（没线缆），**一致性检验时应跳过**。

### 3.3 hosts（主机追踪）

```json
{
  "00:00:00:00:00:01": {
    "mac": "00:00:00:00:00:01",
    "ipv4": ["10.0.0.1"],
    "dpid": "0000000000000002",
    "port": 3,
    "first_seen": 1785912334.25,
    "last_seen": 1785912888.43
  }
}
```

> 已过滤交换机接口 MAC（IPv6 DAD/NS 帧误报），hosts 只含真实主机。

### 3.4 port_stats（端口计数器）

```json
"0000000000000001": [
  { "port_no": 1, "rx_packets": 12, "tx_packets": 18, "rx_bytes": 960, "tx_bytes": 1440,
    "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0, "duration_sec": 356 }
]
```

### 3.5 flows（流表）

```json
"0000000000000001": [
  { "priority": 0, "cookie": 0, "packet_count": 5, "byte_count": 390,
    "duration_sec": 300, "idle_timeout": 0, "hard_timeout": 0,
    "match": "OFPMatch(oxm_fields={'in_port': 1, 'eth_dst': '00:00:00:00:00:02'})",
    "instructions": "[OFPInstructionActions(actions=[OFPActionOutput(port=3)])]" }
]
```

### 3.6 events（事件流）

```json
{ "time": 1785914199.4, "type": "port_link_status",
  "dpid": "0000000000000001", "port_no": 3, "name": "s1-eth3",
  "down": true, "reason": 2 }
```

事件类型：`controller_started` / `switch_connected` / `switch_disconnected` / `host_discovered` / `host_moved` / `port_link_status`（down=true 故障，false 恢复）/ `host_link_status` 等。

---

## 4. 历史状态 — `GET /api/history?key=&limit=`

| key | 内容 | 用途 |
|---|---|---|
| `summary` | {time, switches, hosts, flows, rx_bytes, tx_bytes, ports_down} | 流量趋势 |
| `validation` | {time, ok, errors, warnings, infos, issues[]} | 自愈时间线 |
| `connectivity` | {time, loss_pct, reachable, unreachable} | 连通性探测（每 15s pingall） |
| `port_stats` | [{time, dpid, port_no, name, rx_bytes, tx_bytes, ...}] | 端口级趋势 |

---

## 5. REST API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 完整最新状态 |
| GET | `/api/switches` `/api/hosts` `/api/port_stats` `/api/flows` | 分项 |
| GET | `/api/events?since=&limit=` | 事件流 |
| GET | `/api/validate` | 即时跑 6 维验证（schema/sanity/consistency/completeness/anomaly/connectivity） |
| GET | `/api/history?key=&limit=` | 历史状态 |
| POST | `/api/probe` | 连通性探测上报（live_env 每 15s 调用） |
| GET | `/api/health` | 健康检查 |
| GET | `/` | 可视化监控台 |

验证报告结构：`{ok, issue_count, by_level:{error,warning,info}, issues[], validated_at}`

---

## 6. 意图模型（期望状态）— 推荐 Schema v0.1

> 供意图翻译智能体输出。这是**契约提案**，三方确认后冻结。

```json
{
  "intent_id": "intent-20260805-001",
  "timestamp": 1785913932.2,
  "user_statement": "保证研发部和市场部可以互相访问，且链路必须可用",
  "semantics": {
    "type": "connectivity",
    "endpoints": [
      { "name": "研发部", "ip_cidr": ["10.0.0.0/28"], "hosts": ["h1", "h3"] },
      { "name": "市场部", "ip_cidr": ["10.0.0.16/28"], "hosts": ["h2", "h4"] }
    ]
  },
  "target_state": {
    "connectivity": [
      { "src": "研发部", "dst": "市场部", "allow": true }
    ],
    "link_status": [
      { "link": "s1-s2", "state": "up" }
    ],
    "qos": [
      { "link": "s1-s2", "max_latency_ms": 50, "max_loss_pct": 1 }
    ]
  },
  "priority": 1,
  "status": "active",
  "conflicts": []
}
```

**target_state 断言类型**（与第 7 节一致性检验一一对应）：

| 类型 | 断言字段 | 含义 |
|---|---|---|
| connectivity | src/dst/allow | 两端是否可达（allow=true 必须通，false 必须不通） |
| link_status | link/state | 链路 up/down |
| qos | max_latency_ms / max_loss_pct | 时延/丢包上限 |
| resource | min_bandwidth_mbps | 带宽下限 |

---

## 7. 一致性检验 — 期望 vs 实际

**入口**：`consistency_check.py`（本仓库示例实现，`/api/state` + 历史数据输入）

```
用法:
  python3 consistency_check.py intent.json            # 检查一个意图
  python3 consistency_check.py --all                  # 检查所有 active 意图

输出:
{
  "intent_id": "intent-20260805-001",
  "status": "compliant" | "violated" | "unknown",
  "checked_at": 1785914273.4,
  "deviations": [ { "type": "link_status", "link": "s1-s2",
                    "expected": "up", "actual": "down",
                    "severity": "critical" } ],
  "evidence": { "...": "实际状态摘要" }
}
```

**映射规则：**

| 期望断言 | 实际数据来源 | 判定 |
|---|---|---|
| connectivity allow=true | hosts 表 + connectivity 历史（pingall） | 两端主机都在 + 最近探测 loss=0 → compliant |
| connectivity allow=false | hosts 表 + 流表 | 两主机不在同一交换机且无互通流 → 近似判定 |
| link_status up | switches[].ports[].state | state&1==0 → up |
| qos max_latency_ms | 手工探测（iperf/ping 时延）| 超出 → violated |
| 主机存在 | hosts 表 | 期望的 host/IP 出现在 hosts → compliant |

> **自愈触发**：status=violated 且 deviations 非空 → 意图保障智能体生成自愈意图（如 `link_up(s1,s2)`），交给意图实现智能体执行，执行后重新一致性检验。本系统的事件流 + validation 历史已提供"自愈前后"对比所需的全部数据（见 dashboard「自愈状态」面板）。

---

## 8. 对接示例（Python）

```python
import json, urllib.request

API = 'http://127.0.0.1:8080'

def get_state():
    with urllib.request.urlopen(API + '/api/state', timeout=3) as r:
        return json.loads(r.read())

# 意图实现智能体需要"当前拓扑+端口"来生成配置:
state = get_state()
for dpid, sw in state['switches'].items():
    print(dpid, [(p['name'], p['state']) for p in sw['ports']])

# 意图翻译智能体需要"历史意图是否冲突"→ 可查事件/历史
# 意图保障智能体一致性检验 → 见 consistency_check.py
```

或直接复用 `state_collector.py`（REST 客户端 + CLI）：

```bash
python3 state_collector.py http://127.0.0.1:8080 state
python3 state_collector.py http://127.0.0.1:8080 validate
```

---

## 9. 与任务清单的对应关系

| 任务清单项 | 实现 | 状态 |
|---|---|---|
| Mininet 场景搭建 / 故障注入脚本 | topo_simple.py / fault_injector.py | ✅ |
| 交换机列表 / 端口状态 / 字节数丢包 / 流表 / 主机连接关系 | controller_app.py + /api/* | ✅ |
| 定期 ping 探测 | live_env.py（每 15s pingall → /api/probe） | ✅ |
| 统一 JSON / 查询接口 | /api/state + /api/history | ✅ |
| 保存最新 + 部分历史 | state_history.py（200 条环形缓冲） | ✅ |
| 可视化：拓扑 / 链路状态 / 告警 / 自愈前后 | dashboard.html | ✅ |

---

## 10. 已知限制（诚实声明）

1. **连通性断言依赖周期探测**（15s 粒度），故障窗口 <15s 可能漏检；可调 `live_env.py` 周期。
2. **allow=false（隔离）断言**是近似判定（基于拓扑位置），精确隔离需流表级核对。
3. **qos 断言**需要主动测量（iperf3），当前 demo 未自动化，仅保留接口。
4. 历史缓冲 200 条 ≈ 16 分钟；更长周期需改 `state_history.MAX_ENTRIES`。

---

## 11. 持久化层（2026-10-08 v3.5+ 新增）

> **本文档聚焦"对外 JSON 契约"。v3.5 起,intensure 把这些字段持久化到本地 PostgreSQL（`intensure` schema + `shared` schema 共 8 张表）,供跨模块读取。**

**详细架构**:见 [`docs/database-architecture.md`](./database-architecture.md)（架构总览 + 决策记录）

**Schema 落库对应**（intensure 这边只写不读）：

| ActualState 字段 | 持久化到 | 写频率 |
|---|---|---|
| `meta` / `switches` / `hosts` / `flows` / `port_stats` | `intensure.network_state` | 每 5s upsert |
| summary（聚合摘要）| `intensure.state_history` | 30s 节流 + 变化触发 |
| `events`（port_link_status / link 发现 / 丢失）| `intensure.port_events` / `intensure.link_events` | 翻转时 |
| `port_stats` 探针异常 | `intensure.probe_results` | 仅异常 |
| - | `shared.audit_log` | 跨模块调用时 |
| - | `shared.module_health` | 心跳（**生产才启用**，Phase 1 默认关） |

**持久化层关键设计**:
- 选择性写：状态变化 / 探针异常才写（不写噪声）
- 心跳默认关：`INTENSURE_HEARTBEAT=1` 才启用
- 100 意图 1 年 ≈ 13 GB 存储（比全量节省 95%）

**我们不读的表**：`implementation.policies` / `translation.*` / `assurance.*`——读取是上层模块的职责（职责分离）。
