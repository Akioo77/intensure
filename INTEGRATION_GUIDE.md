# 采集模块 · 联调对接文档

> **版本**: v3.0（2026-09-03）
> **状态**: ✅ 端到端 mock 联调 100% 通过，等待真实模块 IP

---

## 📋 目录

1. [概述](#一-概述)
2. [我们的工作进展](#二-我们的工作进展)
3. [对接需要的信息](#三-对接需要的信息)
4. [我们的对外接口](#四-我们的对外接口硬约束)
5. [项目文件清单](#五-项目文件清单)
6. [如何运行与验证](#六-如何运行与验证)
7. [联调流程建议](#七-联调流程建议)
8. [常见问题](#八-常见问题)
9. [已知限制](#九-已知限制)
10. [附录](#十-附录)

---

## 一、概述

### 1.1 项目背景

本项目是「意图驱动网络管理系统」的**采集层**，与翻译（同学 B）、实现（同学 ?）、保障（同学 C）三个智能体协作闭环：

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ 意图翻译    │ ──▶│ 意图实现    │ ──▶│ 意图保障    │
│ (同学 B)    │    │ (同学 ?)    │    │ (同学 C)    │
└─────────────┘    └─────────────┘    └─────────────┘
                                           ▲
                                           │ POST /check, /verify
                                           │ (ActualState)
                                           │
                                    ┌──────┴──────┐
                                    │ 采集模块    │
                                    │ (我们)      │
                                    └─────────────┘
```

**我们的职责**：看见网络 → 探测 → 实际状态数字化 → 上报给保障模块。
**我们不做的**：意图翻译、一致性诊断、自愈决策、配置下发。

### 1.2 4 大职责完成度（v3.0）

| # | 职责 | 完成度 | 实现位置 |
|---|------|--------|----------|
| 1 | **Mininet 场景搭建**（拓扑 + 故障注入）| ✅ 100% | `topo_*.py` + `fault_injector.py` |
| 2 | **网络状态采集**（交换机/端口/流表/主机/丢包）| ✅ 100% | `controller_app.py`（LLDP + PortStatus）|
| 3 | **实际状态模型**（统一 JSON + 历史 + 接口）| ✅ 100% | `actual_state_builder.py` + `state_history.py` + `reporter.py` |
| 4 | **可视化**（拓扑 + 链路 + 告警 + 自愈前后对比）| ✅ 100% | `dashboard.html`（单文件 SPA）|

### 1.3 已交付的能力清单

#### ✅ 5 类故障注入（`fault_injector.py`）

```python
fi.link_down(src, dst)          # 断链
fi.link_up(src, dst)            # 恢复
fi.delay(src, dst, delay_ms)   # 时延
fi.loss(src, dst, percent)      # 丢包
fi.rate_limit(src, dst, mbps)   # 限速
fi.clear_all()                  # 一键清除
```

#### ✅ 6 类采集

| 类别 | 实现 | 字段 |
|------|------|------|
| 交换机列表 | OpenFlow FeaturesReply | `switches{}` |
| 端口状态 | PortStatus 事件 | `port_no`, `state`, `name` |
| 端口收发字节数 | PortStats | `rx_bytes`, `tx_bytes` |
| 端口丢包数 | PortStats | `rx_errors`, `tx_dropped` |
| 流表 | FlowStatsReply | `flows{}` |
| 主机↔交换机连接 | **LLDP 自动发现** | `hosts{}` |
| 周期可达性探测 | live_env.py pingall | `latency_ms`, `packet_loss` |

#### ✅ 3 种联调模式

| 模式 | 命令 | 状态 |
|------|------|------|
| **mock** | `integration_demo.py --mode mock` | ✅ 5/5 上报，9.1ms 平均 |
| **real** | `integration_demo.py --mode real --assurance-url ...` | ⏳ 等同学 C IP |
| **skip** | `integration_demo.py --mode skip` | ✅ 单独验证采集 |

---

## 二、我们的工作进展

### 2.1 端到端 mock 联调结果（实测，2026-09-03 04:59）

```
Step 0: 清理残留                                          ✓
Step 1: Controller 6653 端口等待 + Mininet 启动          ✓
Step 2: 2 交换机 + 4 主机发现                             ✓
Step 3: Mock 保障模块启动                                 ✓
Step 4: 注册测试意图 REQ-001-CI-001 + 激活                ✓ HTTP 200
Step 6: 2 个正常周期探测                                 ✓ reachable=True, latency=0.041-0.049ms
Step 7: 注入 s1-s2 链路故障                              ✓ loss=66.67%
Step 8: 修复链路 + 复测                                   ✓ reachable=True, latency=0.062ms
Step 8.5: 故障期 ActualState 样例（含 down_links）        ✓
Step 9: 5 次上报                                         ✓ 全部 HTTP 200
Step 10: Reporter 统计                                   ✓ 平均 9.1ms, 5/5 成功
Step 11: ActualState 样例输出                            ✓ 字段格式符合联调 §4.2
```

### 2.2 单元测试覆盖（45/45 通过）

| 测试文件 | 通过率 | 覆盖内容 |
|----------|--------|----------|
| `test_actual_state_builder.py` | 21/21 ✓ | ActualState 字段映射、链路去重、OFPP_LOCAL 跳过、ISO 8601 时间 |
| `test_reporter.py` | 24/24 ✓ | HTTP 客户端、4xx/5xx 重试、线程安全、UTF-8 编码、Chinese payload |

### 2.3 关键设计决策（避免误解）

1. **ActualStateBuilder**：
   - dpid 数字 → `"swN:pN"` 字符串映射（诊断模块按 `:` 提取设备名）
   - 双向链路去重（frozenset）
   - 显式 `null` vs 缺省（`latency_ms` 无 SLA 传 `null`）
   - OFPP_LOCAL 端口跳过（OVS 内部端口）
   - ISO 8601 时间戳带 `+08:00` 时区

2. **AssuranceReporter**：
   - 单文件、无 `requests` 依赖（用标准库 `urllib`）
   - 4xx 不重试（除 409）/ 5xx 指数退避重试
   - 线程安全统计（`deque` + `Lock`）
   - UTF-8 编码（兼容中文 payload）

3. **架构边界（v2.0 重构后）**：
   - 删除：一致性校验、冲突检测、自愈回路、异常检测（其他同学负责）
   - 保留：采集 + Dashboard + 故障注入（加分项）

### 2.4 已修的 Bug 记录（端到端测试发现）

| # | Bug | 修复 |
|---|------|------|
| 1 | `controller_app.py:356` 用 `dpid` 变量不存在 | 改为 `datapath.id` |
| 2 | `cleanup_mininet()` 用 `pkill -f integration_demo` 自杀 | 改用具体进程名 |
| 3 | 所有 `print()` 没 `flush=True`（buffer 看不到）| 全部加 `flush=True` |
| 4 | mock server 端点路径不匹配（`/register-request` vs `/api/v1/assurance/register-request`）| 加 `strip_prefix()` 兼容 |
| 5 | mock server `_handle_check` 在 VIOLATED 状态返回 409（过严）| 改为只拒绝 REGISTERED |
| 6 | Mininet 启动时 controller 未完全就绪（6653 端口未监听）| 加 socket 探测等待 |

---

## 三、对接需要的信息

### 3.1 需要同学 C（保障模块）提供

| 项 | 内容 | 格式示例 | 必需 |
|---|------|----------|------|
| **模块 URL** | FastAPI 服务地址 | `http://192.168.1.100:8000` | ✅ |
| **端口** | HTTP 端口 | `8000` | ✅ |
| **Swagger 地址** | 自动生成文档 | `http://<IP>:8000/docs` | ✅（验证用）|
| **状态机起始状态** | `/register-request` 后默认 `REGISTERED` 还是 `ACTIVE` | 文档说明 | ✅ |
| **`/register-request` 是否就绪** | 该端点已实现 | ✅/❌ | ✅ |
| **`/activate` 是否就绪** | 该端点已实现 | ✅/❌ | ✅ |
| **`/check` 是否就绪** | 接收 ActualState | ✅/❌ | ✅ |
| **`/verify` 是否就绪** | 接收复测 ActualState | ✅/❌ | ✅ |
| **`/record/{intent_id}` 是否可读** | 状态查询 | ✅/❌ | 可选 |
| **Windows 防火墙状态** | 8000 入站已开 | ✅/❌ | ✅ |

### 3.2 需要同学 B（翻译模块）确认

| 项 | 内容 | 状态 |
|---|------|------|
| **child_intent_id 是否透传** | child_intent 的 ID 一致作为 ActualState 的 `intent_id` | ⏳ 待确认 |
| **确认稿 schema 是否仍然有效** | v0.1 是否是最终版本 | ⏳ 待确认 |
| **ISOLATE 类型如何下发** | 是否展开为两个方向的 DENY | ⏳ 待确认 |
| **service.protocol = ANY 时 down_links 是否仍触发违例** | 边界情况 | ⏳ 待确认 |

### 3.3 需要确认的假设

1. **假设 1**：同学 C 的 `/check` 端点已就绪
   - 如果未就绪：`integration_demo.py --mode mock` 可继续使用 mock 测试

2. **假设 2**：ActualState 字段格式与同学 A 写的《采集模块联调说明.md §4.2》一致
   - 我们严格遵守该文档格式
   - 如有差异请提前通知我们

3. **假设 3**：跨平台网络可达
   - macOS (Colima VM) ↔ Windows (同学 C)
   - 需要双方在同一局域网或 VPN 下

---

## 四、我们的对外接口（硬约束）

### 4.1 POST /check —— 周期探测后上报

**请求体**：

```json
{
  "intent_id": "REQ-001-CI-001",
  "actual": {
    "intent_id": "REQ-001-CI-001",
    "checked_at": "2026-09-03T04:59:55+08:00",
    "source_host": "10.0.0.1",
    "destination_host": "10.0.0.4",
    "reachable": true,
    "latency_ms": 0.062,
    "packet_loss": 0.0,
    "expected_flow_present": true,
    "down_links": [],
    "down_ports": [],
    "conflicting_flows": []
  }
}
```

### 4.2 POST /verify —— 修复后复测上报

**请求体**：

```json
{
  "intent_id": "REQ-001-CI-001",
  "actual_after_heal": {
    "intent_id": "REQ-001-CI-001",
    "checked_at": "2026-09-03T04:59:55+08:00",
    "reachable": true,
    "latency_ms": 0.062,
    "packet_loss": 0.0,
    "expected_flow_present": true,
    "down_links": [],
    "down_ports": [],
    "conflicting_flows": []
  }
}
```

### 4.3 ActualState 字段详细定义

| 字段 | 必填 | 类型 | 说明 | 我们的实现 |
|------|------|------|------|-----------|
| `intent_id` | ✅ | string | 关联意图 ID | 透传上游 |
| `checked_at` | ✅ | string (ISO 8601) | 探测时间，带时区 | `datetime.now().isoformat()` 带 `+08:00` |
| `reachable` | ✅ | bool | 是否可达 | `pingall` 结果 |
| `expected_flow_present` | ✅ | bool | 期望流表是否存在 | 流表 dump 后比对 cookie |
| `source_host` | ⬜ | string | 源主机 IP | 配置项 `SRC_HOST` |
| `destination_host` | ⬜ | string | 目的主机 IP | 配置项 `DST_HOST` |
| `latency_ms` | ⬜ | float 或 null | 实测延迟 ms | 仅在有 SLA 时上报 |
| `packet_loss` | ⬜ | float (0~1) 或 null | 丢包率 | 仅在有 SLA 时上报 |
| `down_links` | ⬜ | array[string] | 中断链路 | 格式 `"sw1:p1-sw2:p2"` |
| `down_ports` | ⬜ | array[string] | 中断端口 | 格式 `"sw1:p1"` |
| `conflicting_flows` | ⬜ | array[object] | 冲突流表 | `[{switch, cookie}]` |

**字段格式硬约束**：
- `down_links` 元素必须是 `"设备:端口-设备:端口"`，**字符串**
- `dpid` 是 16 位十六进制（如 `"0000000000000001"`）→ 在 down_links 里映射为 `sw1`
- 时间戳必须带时区（不要裸 ISO 字符串）

### 4.4 错误码处理（`reporter.py` 实现）

| HTTP | 含义 | 我们的行动 |
|------|------|-----------|
| 200 | 正常 | 继续 |
| 400 | 字段错误 | 报错（不重试） |
| 404 | 意图未注册 | 报错（一般是上游问题）|
| 409 | 状态机前置条件不满足 | 轮询 `/record/{intent_id}` |
| 5xx | 保障模块异常 | 指数退避重试（1s → 2s → 4s），最多 2 次 |

### 4.5 ActualState 故障态样例（已验证）

```json
{
  "intent_id": "REQ-001-CI-001",
  "checked_at": "2026-09-03T04:59:55+08:00",
  "reachable": false,
  "expected_flow_present": true,
  "source_host": "10.0.0.1",
  "destination_host": "10.0.0.4",
  "latency_ms": null,
  "packet_loss": null,
  "down_links": ["sw1:p3-sw2:p3"],
  "down_ports": ["sw2:p3"],
  "conflicting_flows": []
}
```

⚠️ **关键点**：`latency_ms` 和 `packet_loss` 在故障时显式 `null`（不是缺省）。
---

## 五、项目文件清单

### 5.1 核心交付文件（v3.0 最终版）

| 文件 | 行数 | 类型 | 说明 |
|------|------|------|------|
| **actual_state_builder.py** | 273 | ⭐ 核心 | 内部 state → ActualState 格式转换器 |
| **reporter.py** | 316 | ⭐ 核心 | HTTP POST 客户端（指数退避重试）|
| **integration_demo.py** | 495 | ⭐ 核心 | 端到端联调 demo（real/mock/skip 三模式）|
| **mock_assurance_server.py** | 219 | ⭐ 核心 | 本地 mock 保障模块（FastAPI）|
| **live_env.py** | 192 | ⭐ 核心 | v2.0 重构：周期探测 + 上报 |
| **controller_app.py** | ~600 | ⭐ 核心 | os-ken 主控制器（LLDP + PortStatus）|
| **state_server.py** | 208 | 加分项 | Dashboard 后端 REST API |
| **dashboard.html** | 828 | 加分项 | 采集视角 Dashboard（单文件 SPA）|
| **fault_injector.py** | 197 | 加分项 | 5 类故障注入 |
| **state_model.py** | ~340 | 内部 | 内部数据抽象 + validate |
| **state_history.py** | ~80 | 内部 | 历史环形缓冲 |
| **topo_simple.py** | ~50 | 加分项 | 单链 2 交换机 4 主机拓扑 |
| **topo_multi.py** | ~60 | 加分项 | 多分支 3 交换机 6 主机 |
| **topo_mesh.py** | ~50 | 加分项 | K4 网状 4 交换机 |
| **simple_switch_13.py** | ~70 | 基础 | L2 最简控制器（被 controller_app.py 替代）|

### 5.2 测试文件

| 文件 | 测试数 | 通过率 |
|------|--------|--------|
| **test_actual_state_builder.py** | 21 | 21/21 ✓ |
| **test_reporter.py** | 24 | 24/24 ✓ |
| **test_recovery.py** | 5 | 5/5 ✓ |
| `test_osken.sh` | shell 测试 | ✓ |

### 5.3 文档

| 文件 | 类型 | 状态 |
|------|------|------|
| **README.md** | 项目说明 | ✅ v3.0 |
| **REQUIREMENTS.md** | 需求文档 | ✅ v3.0 |
| **INTEGRATION_PLAN.md** | 联调计划 | ✅ v1.0 |
| **STATE_MODEL.md** | 数据模型 | ✅ |
| **CHANGELOG.md** | 变更记录 | ✅ v3.0 |

### 5.4 归档到 `legacy/`（**不要参考**）

> ⚠️ **重要**：以下 12 个文件已**归档**，仅作历史参考。**不要基于这些做联调假设**。
> 它们在 v2.0 重构前存在，职责已被划归其他同学。

```
legacy/
├── PHASE3_PLAN_v1.md
├── conflict_detector.py            # 冲突检测 → 翻译模块 v0.1 schema 包含
├── conflict_resolver.py            # 冲突消解 → 翻译模块 suggestions 包含
├── healing_engine.py               # 自愈回路 → 保障模块 /verify 包含
├── healing_intent_generator.py     # 自愈策略 → 保障模块 /healing/trigger 包含
├── anomaly_detector.py             # 异常检测 → 保障模块 /anomalies 包含
├── intent_registry.py              # 意图注册表 → 翻译模块 child_intents 管理
├── consistency_check.py            # v1.0 示例代码
├── state_history.py (在 legacy/)   # v1.0 历史（被当前 state_history.py 替代）
├── state_server.py (在 legacy/)    # v1.0 自营 REST（v2.0 精简）
├── intent_sample.json              # v1.0 schema
├── intents_full.json               # v1.0 多意图示例
└── test_*.py                       # 对应旧测试
```

### 5.5 ⚠️ 注意事项（避免踩坑）

#### ❌ 不要修改的字段

| 文件 | 字段/函数 | 为什么 |
|------|-----------|--------|
| `actual_state_builder.py` | `down_links` 格式 `"swN:pN"` | 诊断模块按 `:` 解析设备名 |
| `actual_state_builder.py` | `OFPP_LOCAL` 跳过（port_no=0xfffffffe） | OVS 内部端口，永远 LINK_DOWN |
| `actual_state_builder.py` | 时间戳带 `+08:00` 时区 | 避免下游时区歧义 |
| `reporter.py` | `reporter.check()` 重试策略 | 4xx 不重试（除 409），5xx 退避 |

#### ❌ 不要启用的服务

| 服务 | 原因 |
|------|------|
| `dashboard.html` 中的 ActualState 样例卡片（已删除） | 调试面板，已清理 |
| `state_server.py` 的 `/api/intents` 等 15 个 endpoints | v2.0 重构时已删除，返回 404 |

#### ✅ 安全修改的范围

| 文件 | 可以改什么 |
|------|-----------|
| `live_env.py` | 环境变量 `ASSURANCE_URL`、`INTENT_ID`、`PROBE_INTERVAL`、`SRC_HOST`、`DST_HOST` |
| `controller_app.py` | LLDP 间隔、PortStats 间隔 |
| `dashboard.html` | 顶部 badge 文案、配色（注意暗色主题变量） |
| `topo_*.py` | 主机/交换机数量（注意 `controller_app.py` 的 `n` 参数匹配） |

### 5.6 项目路径

| 位置 | 路径 |
|------|------|
| macOS 项目根 | `~/workspace/projects/intensure/` |
| macOS 镜像 | `~/.openclaw/workspace/projects/intensure/` |
| VM 项目根（Colima Ubuntu 24.04） | `/home/sunxiaoxuan.guest/intensure/` |
| 本机 Git 仓库 | `~/workspace/projects/intensure/.git/` |

> 📌 VM 路径用 mount 共享（virtiofs），**macOS 改文件后必须 scp 到 VM 才生效**。

---

## 六、如何运行与验证

### 6.1 环境要求

| 组件 | 版本 |
|------|------|
| macOS | 14.7+（Apple Silicon/x86_64 均可）|
| Colima | 1.x（含 Lima 2.2+）|
| VM 内核 | Ubuntu 24.04 LTS |
| VM 资源 | 2 核 / 2GB RAM / 10GB 磁盘 |
| Python（VM 内） | 3.12 |
| Mininet | 2.3.0 |
| Open vSwitch | 3.3.4 |
| os-ken（SDN 控制器） | 2.8.1 |
| Flask | 3.x |
| requests 替代 | 我们用 `urllib`（标准库）|

### 6.2 一键启动（采集模块 + Dashboard）

```bash
# Terminal 1: VM 内启动 controller + REST API + Dashboard
colima ssh
cd /home/sunxiaoxuan.guest/intensure
sudo -E nohup python3 -u run_all.py > /tmp/controller.log 2>&1 &

# Terminal 2: VM 内启动 Mininet + 周期探测
sudo -E nohup python3 -u live_env.py > /tmp/live_env.log 2>&1 &

# Terminal 3: macOS 上建立 SSH 隧道
ssh -F ~/.colima/ssh_config -N -L 18080:127.0.0.1:8080 colima &

# 浏览器打开
open http://127.0.0.1:18080/
```

### 6.3 端到端 demo（mock 模式）

```bash
colima ssh
cd /home/sunxiaoxuan.guest/intensure
python3 integration_demo.py --mode mock --intent-id REQ-001-CI-001
```

预期输出（截断）：

```
🚀 端到端联调 Demo (mode=mock)
   保障模块: http://127.0.0.1:8000/api/v1/assurance
   测试意图: REQ-001-CI-001

  ✓ Mininet 已清理
  ✓ Controller + REST 已就绪 (PID ...)
  ✓ Controller 6653 端口就绪
  ✓ Mininet 已启动
  ✓ 2 个交换机已连接
  ✓ 4 个主机已发现
  ✓ Mock 保障模块已启动（http://127.0.0.1:8000）
  ✓ 测试意图已注册
  ✓ 测试意图已激活
  ✓ Reporter 已就绪 → http://127.0.0.1:8000/api/v1/assurance
  ✓ [normal-1] reachable=True, latency=0.049, loss=0.0%
  ✓ [normal-2] reachable=True, latency=0.041, loss=0.0%
  ✓ 链路已 down
  ✓ [post-fault-1] reachable=False, latency=None, loss=66.67%
  ✓ 链路已恢复
  ✓ [after-heal] reachable=True, latency=0.062
  ✓ 上报完成: HTTP 200 (7.88ms)
  ...（共 5 次）
  ✓ 成功率: 5/5
  ✓ 平均响应时间: 9.1ms

🎉 端到端联调 Demo 完成!
```

### 6.4 端到端 demo（real 模式，等同学 C IP）

```bash
colima ssh
cd /home/sunxiaoxuan.guest/intensure
python3 integration_demo.py \
  --mode real \
  --assurance-url http://<同学C的IP>:8000/api/v1/assurance \
  --intent-id REQ-001-CI-001
```

### 6.5 单元测试

```bash
colima ssh
cd /home/sunxiaoxuan.guest/intensure
python3 -m unittest test_actual_state_builder -v
python3 -m unittest test_reporter -v
python3 -m unittest test_recovery -v
```

预期：
- `test_actual_state_builder`: 21/21 ✓
- `test_reporter`: 24/24 ✓
- `test_recovery`: 5/5 ✓

### 6.6 验证我们的输出（供你/同学 C 校对）

```bash
# 健康检查
curl -s http://127.0.0.1:8080/api/health

# 完整状态
curl -s http://127.0.0.1:8080/api/state | python3 -m json.tool

# 单个交换机
curl -s http://127.0.0.1:8080/api/switches

# 事件流
curl -s "http://127.0.0.1:8080/api/events?limit=20"

# 验证报告
curl -s http://127.0.0.1:8080/api/validate
```

---

## 七、联调流程建议

### 阶段 1：连通性测试（5 分钟）

**目标**：确认网络可达

1. **同学 C** 提供 IP（如 `192.168.1.100`）
2. 我们这边：
   ```bash
   curl -s http://<同学C的IP>:8000/api/health
   curl -s http://<同学C的IP>:8000/docs  # Swagger 应能看到
   ```
3. 失败排查：
   - Windows 防火墙 → 放行 8000 入站
   - 跨网段 → 检查路由 / VPN

### 阶段 2：单 intent 完整流程（30 分钟）

**目标**：跑通 `REQ-001-CI-001` 一个意图的完整生命周期

| Step | 谁 | 做什么 |
|------|----|--------|
| 1 | 我们 | 准备 child_intent JSON（用桌面上 `意图翻译案例1-6` 案例 1）|
| 2 | 我们 | `curl -X POST http://<同学C>:8000/api/v1/assurance/register-request -d @req001.json` |
| 3 | 同学 C | 返回 200，确认记录已建 |
| 4 | 我们 | `curl -X POST http://<同学C>:8000/api/v1/assurance/activate -d '{"intent_id":"REQ-001-CI-001"}'` |
| 5 | 我们 | `python3 integration_demo.py --mode real --intent-id REQ-001-CI-001` |
| 6 | 我们 | 注入 s1-s2 链路故障 |
| 7 | 同学 C | 收到 ActualState（reachable=false），诊断为 VIOLATED |
| 8 | 同学 C | 决定下发自愈意图（同学 ?） |
| 9 | 同学 ? | 修复链路 |
| 10 | 我们 | 复测 → 上报 actual_after_heal |
| 11 | 同学 C | 收到 → RECOVERED |

### 阶段 3：多 intent 并发（可选，1 小时）

- 同时注册多个 intent（如 12 个）
- 验证我们的 `actual_state_builder.py` 能区分不同的 `intent_id`
- 验证我们的 `reporter.py` 线程安全

### 阶段 4：跨网段 / 跨平台压力测试（可选）

- 同学 B/C 模拟多用户并发
- 我们这边压力测试 1000 个 ping/秒
- 验证 ActualState 不会丢失或乱序

---

## 八、常见问题

### Q1：Windows 防火墙阻挡 8000 端口

**症状**：`curl http://<IP>:8000/api/health` 超时或拒绝

**解决**（同学 C 操作）：
1. Windows 设置 → Windows Defender 防火墙 → 高级设置
2. 入站规则 → 新建规则 → 端口 → TCP 8000 → 允许连接

### Q2：跨网段无法连接

**症状**：同一局域网内能 ping 通但 8000 端口拒绝

**排查**：
- `telnet <IP> 8000` 测试 TCP 连通
- `nc -zv <IP> 8000`
- 同学 C 端：`netstat -ano | findstr :8000` 确认服务在监听

### Q3：ActualState 字段不一致

**症状**：同学 C 收到 ActualState 后某个字段不符合 §4.2 约定

**排查清单**：
- `down_links` 元素是否是 `"swN:pN"` 字符串？
- `checked_at` 是否带时区（`+08:00` 或 `Z`）？
- `latency_ms`/`packet_loss` 故障时是 `null` 还是缺省？
- `dpid` 是 16 位十六进制字符串？

### Q4：时间戳时区问题

**症状**：同学 C 端解析时间报错或时间错位

**约定**：我们统一用 `Asia/Shanghai (+08:00)`，格式 `2026-09-03T04:59:55+08:00`

### Q5：中文 payload 编码问题

**症状**：ActualState 包含中文时报 `UnicodeEncodeError`

**解决**：我们已经在 `reporter.py` 显式 `json.dumps(payload, ensure_ascii=False).encode('utf-8')`，避免此类问题。

### Q6：pkill 自杀问题

**症状**：跑 demo 时 controller 自己被 pkill 干掉

**解决**：用具体进程名，不要 `pkill -f integration_demo`。

### Q7：Mininet 起不来（接口已存在）

**症状**：`RTNETLINK answers: File exists`

**解决**：
```bash
sudo mn -c   # 清残留
```

### Q8：dashboard 显示「意图接口不可达」

**症状**：Dashboard 顶部 badge 显示 warn

**原因**：v2.0 重构后已删除该面板。如果是旧版本 Dashboard，请重新 `scp` 最新版本。

### Q9：OS 字符编码乱码

**症状**：故障注入时报 `UnicodeDecodeError`

**解决**：VM 内 `export LANG=en_US.UTF-8` 或 `export LC_ALL=C.UTF-8`

### Q10：ssh 隧道断开

**症状**：`curl http://127.0.0.1:18080/` 拒绝连接

**解决**：重新建立隧道：
```bash
ssh -F ~/.colima/ssh_config -N -L 18080:127.0.0.1:8080 colima
```

---

## 九、已知限制

### 9.1 v2.0 范围限制（明确边界）

| 不做的 | 为什么 |
|--------|--------|
| 一致性诊断 | 同学 C 的 `/check` 端点已包含 |
| 冲突检测 / 消解 | 同学 B 的 v0.1 schema 已包含 |
| 自愈决策 | 同学 C 的 `/verify` 端点处理 |
| 配置下发 | 同学 ? 负责 |
| 异常检测 | 同学 C 的 `/anomalies` |

### 9.2 性能边界

| 项 | 当前实现 | 建议上限 |
|----|----------|----------|
| 交换机数 | 2-6（已测）| 16 |
| 主机数 | 4-6（已测）| 64 |
| 探测频率 | 15 秒/次 | 1 秒/次 |
| 周期 pingall | 12 对 | 56 对 |
| 同时 intent 数 | 1（已测）| 12 |
| Reporter 调用并发 | 单线程 | 多线程（已支持）|

### 9.3 不覆盖的场景

- **生产级可靠性**：我们是 PoC / MVP，不是生产级实现
- **HA / 容灾**：单 controller 实例，单 VM
- **多租户隔离**：暂未实现
- **安全 / 认证**：API 无认证（依赖网络隔离）
- **持久化**：state 在内存 + `/tmp/*.json`（重启会丢部分历史）
- **TLS / HTTPS**：HTTP 明文（demo 阶段够用）

### 9.4 演示模式与生产模式的差异

| 项 | 演示模式（demo） | 生产模式 |
|----|-----------------|---------|
| 拓扑 | Mininet 模拟 | 真实硬件 / 多机 |
| 状态采集 | os-ken 控制器 | 同 + 商业南向协议 |
| 故障注入 | fault_injector.py | 实际故障 |
| 部署 | 单 VM | 分布式集群 |

---

## 十、附录

### 10.1 测试覆盖表

| 类别 | 文件 | 测试数 | 通过 |
|------|------|--------|------|
| ActualState 构建 | test_actual_state_builder.py | 21 | 21 ✓ |
| HTTP 上报 | test_reporter.py | 24 | 24 ✓ |
| 故障恢复 | test_recovery.py | 5 | 5 ✓ |
| 端到端 | integration_demo.py (mock) | 11 步骤 | 全绿 |
| **总计** | — | **45+** | **45+ ✓** |

### 10.2 完整 demo log（保存在项目目录）

- `test_reports/2026-09-03_integration_test_report.md` — 完整测试报告（4.7KB）
- `test_reports/integration_demo_mock.log` — 详细 log（4.5KB）
- `demo_screenshots/01_dashboard_topology.png` — 实时拓扑
- `demo_screenshots/02_dashboard_integration_panel.png` — 联调面板
- `demo_screenshots/03_dashboard_flow_table_traffic.png` — 流表 + 流量
- `demo_screenshots/04_dashboard_link_detail.png` — 链路详情
- `demo_screenshots/v3_final/*.png` — v3.0 清理后的截图

### 10.3 相关文档（同级目录）

- `采集模块联调说明.md`（同学 A 写） — 我们严格遵守的契约
- `意图翻译智能体_案例1-6_接口确认稿_v0.1.pdf` — 同学 B 写的下游约定
- `README.md`（项目内） — 项目总览
- `REQUIREMENTS.md`（项目内） — 需求规格
- `INTEGRATION_PLAN.md`（项目内） — 联调计划

### 10.4 关键代码片段（粘出来方便对照）

#### ActualStateBuilder.build() 调用

```python
from actual_state_builder import ActualStateBuilder
from reporter import AssuranceReporter

builder = ActualStateBuilder()
reporter = AssuranceReporter("http://同学C:8000/api/v1/assurance")

state = read_state_from_controller()  # /api/state
reach = probe_reachability(mininet)
flow_present = check_flow_present(state)

actual = builder.build(
    intent_id="REQ-001-CI-001",
    source_host="10.0.0.1",
    destination_host="10.0.0.4",
    state=state,
    reachability_result=reach,
    expected_flow_present=flow_present,
)

response = reporter.check("REQ-001-CI-001", actual)
# response = {"status_code": 200, "body": {...}, "error": None}
```

#### FaultInjector 调用

```python
from fault_injector import FaultInjector
fi = FaultInjector(mininet_net)

fi.link_down("s1", "s2")          # 断 s1-eth3 <-> s2-eth3
fi.link_up("s1", "s2")            # 恢复
fi.delay("s1", "s2", 200)         # 200ms 时延
fi.loss("s1", "s2", 10)           # 10% 丢包
fi.rate_limit("s1", "s2", 50)     # 50Mbps 限速
fi.clear_all()                    # 一键清除
```

#### 完整的探测周期（伪代码）

```python
# live_env.py 的核心循环
while True:
    state = read_state_from_controller()              # /api/state
    loss_pct = net.pingAll()                          # Mininet pingall
    reachable = (loss_pct == 0.0)
    latency = measure_latency(net)                    # h1 -> h4 RTT
    
    actual = builder.build(
        intent_id=INTENT_ID,
        source_host=SRC_HOST,
        destination_host=DST_HOST,
        state=state,
        reachability_result={'reachable': reachable, 'latency_ms': latency, 'packet_loss': loss_pct/100.0},
        expected_flow_present=check_flow_present(state),
    )
    
    reporter.check(INTENT_ID, actual)                # POST /check
    
    time.sleep(PROBE_INTERVAL)                       # 默认 15s
```

### 10.5 联系方式（待填）

### 10.6 版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| **v3.0** | 2026-09-03 | Dashboard 清理 + 自愈前后对比重建 + 同学标签修正 |
| v2.0 | 2026-09-03 | 与上游契约对齐 + 职责重定义 + 新增 5 个核心模块 |
| v1.0 | 2026-08-04 ~ 09-02 | 初始开发 + 7 个 bug 修复 |

