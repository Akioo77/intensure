# 采集模块（Mininet + Ryu/os-ken）— 项目需求与契约

> 应用场景：园区网络 · 意图驱动网络管理系统的**采集层**
> 文档版本：v2.0 (2026-09-03 重构)
> 编写：采集模块开发团队整理
> 配套契约：见 `~/Desktop/网络智能体/采集模块联调说明.md`（同学 A，2026-09-02）+ `意图翻译智能体_案例1-6_接口确认稿_v0.1.pdf`（意图翻译模块的写者）

---

## 1. 系统总目标（背景）

实现"意图驱动网络管理系统"，包含 3 个互相协作的智能体：

| 目标 | 描述 |
|---|---|
| **目标 1** | 意图翻译与配置生成的精准度与可靠性提升 |
| **目标 2** | 意图冲突检测与自动消解 |

三个智能体协作闭环（**理解 → 实现 → 保障**）：

```
              意图智能体（3 个同学分工）              采集模块（同学 A 负责）
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ 意图翻译    │ ──→ │ 意图实现    │ ──→ │ 意图保障    │ ←─ │  采集模块   │
│ 智能体      │    │ 智能体      │    │ 智能体      │   │ (Mininet +  │
│ (同学 B/C)  │ ←── │ (同学 B/C)  │ ←── │ (同学 B/C)  │   │  Ryu/os-ken)│
└─────────────┘    └─────────────┘    └─────────────┘   └─────────────┘
                                          │
                                    ActualState
                                    (POST /check)
```

> ⚠️ **同学标签修正（2026-09-03）**：v2.0 文档此处曾误标为「同学 A = 意图保障」，2026-09-03 修正。
> - **同学 A = 状态采集**（采集模块）= 本系统
> - **同学 B / C = 意图智能体（翻译/实现/保障）**：具体谁负责哪个智能体本模块不关心，只对意图保障智能体输出 ActualState
> - 其他模块的同学归属以课程组安排为准

**采集模块的位置**：是**意图保障智能体的感知层**。
                            ▲                    │
       自愈意图反馈 ─────────────────────────────────┘
                            │
                     ┌──────┴──────┐
                     │  采集模块   │  ← **本系统（我）**
                     │ (Mininet +  │
                     │  Ryu/os-ken)│
                     └─────────────┘
```

**采集模块的位置**：是**保障智能体的感知层**。负责"看见网络真实状态"，**对外只输出一份 ActualState**，由保障模块消费并做一致性诊断。

---

## 2. 我的工作范围（v2.0 重新定义，v3.7 起扩展）

> 📌 **范围沿革**：v2.0（2026-09-03）收敛为「采集 + 上报」；**v3.7（2026-10-08）起重新扩展** ——
> 新增 `assurance_mvp.py`（一致性检查 + 根因诊断 + `intent_states` 状态轨迹 + 写库），
> 并配套数据库（6 schema / 39 表）、alembic 迁移、DB 自动重连、历史归档定时清理、统一日志（v3.10 ~ v3.17）；
> **v3.18.0（2026-10-29）**起补上可观测性（`metrics.py` + `GET /metrics`）与跨模块契约版本协商。

> ⚠️ **范围调整说明**：v1.0 文档（2026-08-08）将本系统定义为"状态采集与数字化网络状态"，包含一致性校验、冲突检测、自愈回路等。**实际项目分工中，这些由其他同学的智能体模块（翻译/实现/保障）实现**。
>
> 本文档基于 `采集模块联调说明.md`（2026-09-02 版）重新定义范围，**核心职责是采集与上报**。
> ⚠️ **例外（v3.7 起）**：本模块另实现了**本地 MVP 版**的一致性校验 + 根因诊断（`assurance_mvp.py`）供演示与自测；
> **对外联调仍以对方模块为准**，本模块的 MVP 不替代对方的正式保障服务。

### 2.1 采集模块职责清单

| # | 职责 | 状态 |
|---|---|---|
| 1 | Mininet 场景搭建（拓扑 + 故障注入） | ✅ 已完成 |
| 2 | 网络状态采集（拓扑 / 主机 / 链路 / 流表） | ✅ 已完成 |
| 3 | 链路事件监听（down / up） | ✅ 已完成 |
| 4 | 实际可达性探测（pingall / iperf / 时延测量） | ✅ 已完成 |
| 5 | 流表 dump 与期望比对（`expected_flow_present`） | ✅ 已完成 |
| 6 | **ActualState 组装**（符合联调说明 §4.2 字段约定） | ✅ 已完成 v2.0 `actual_state_builder.py` |
| 7 | **POST `/check` 上报**（探测后调用） | ✅ 已完成 v2.0 `reporter.py` |
| 8 | **POST `/verify` 上报**（修复后复测） | ✅ 已完成 v2.0 `reporter.py` |
| 9 | 端到端联调 Demo（注册 → 激活 → 探测 → 上报 → 故障 → 复测） | ✅ 已完成 v2.0 `integration_demo.py`（real/mock/skip 三模式）；**真实联调仍阻塞于对方隧道不通** |
| 10 | 采集视角的可视化（Dashboard 加分项） | ✅ 已完成 |

### 2.2 不在本模块范围内的职责（明确边界）

| 职责 | 实际归属 | 说明 |
|---|---|---|
| 意图翻译（NL → JSON） | 翻译模块（同学 B/C） | v0.1 schema 已固化 |
| 冲突检测 / 消解 | 翻译模块（同学 B/C） | schema 中 `conflicts[]` 已含 |
| 一致性校验 | **对方保障模块**（正式） | 本模块 v3.7 起另有本地 MVP（`assurance_mvp.py`），仅供演示自测 |
| 自愈决策 / 根因诊断 | **对方保障模块**（正式） | 本模块 v3.7 起另有本地根因诊断 MVP（`diagnose_violation`），不产出正式自愈意图 |
| 配置生成 / 仿真 / 下发 | 实现模块（同学 B/C） | — |

> 注：早期文档此处误标「同学 A = 保障模块 / 同学 C = 翻译模块」。**真实情况是同学 A = 采集**（本系统），同学 B/C 是其他智能体。

> 早期本地代码（`conflict_detector.py` / `healing_engine.py` / `intent_registry.py` / `conflict_resolver.py` / `anomaly_detector.py`）已归档至 `legacy/`，作为历史探索材料保留。

---

## 3. 联调契约（**对外硬约束**）

### 3.1 ActualState 字段约定（**严格遵守**）

> 完整定义见 `~/Desktop/网络智能体/采集模块联调说明.md` §4.2。本系统组装 ActualState 时**字段名、字段类型、必填项、字符串格式**必须一致。

```json
{
  "intent_id": "REQ-001-CI-001",            // 必填
  "checked_at": "2026-09-03T10:05:00+08:00",// 必填，ISO 8601
  "reachable": true,                        // 必填
  "expected_flow_present": true,            // 必填
  "source_host": "10.0.10.5",               // 选填
  "destination_host": "10.0.60.10",         // 选填
  "latency_ms": 12.3,                       // 选填，无 SLA 传 null
  "packet_loss": 0.0,                       // 选填，无 SLA 传 null
  "down_links": ["sw1:p1-sw2:p2"],          // 选填，"设备:端口-设备:端口"
  "down_ports": ["sw1:p1"],                 // 选填，"设备:端口"
  "conflicting_flows": [{"switch": "sw4", "cookie": "0x123"}]
}
```

**关键约定**：

- `down_links` / `down_ports` 必须是 `"swN:pN"` 这种**字符串格式**（不是 dpid 数字）
- 内部 state 用的是 dpid 数字（如 `0000000000000001:port 3`），**上报前必须做转换**
- 诊断模块按 `:` 提取设备名做豁免判定，**格式错会触发误判**

### 3.2 联调流程（本模块参与的步骤）

```
[上游] 翻译模块输出确认稿
   │  POST /register-request → 保障模块（同学 B/C）建记录
   ▼
[上游] POST /activate → 保障模块进入监控
   ▼
[本模块] ──── 周期循环 ────
   │  1. 周期探测（pingall + 流表 dump + 链路事件）
   │  2. 组装 ActualState
   │  3. POST /check → 保障模块诊断
   │     │
   │     ├─ 正常 → 继续监控
   │     └─ 异常 → 触发修复流程（部署模块执行）
   ▼
[本模块] ──── 修复后复测 ────
   │  1. 重新探测
   │  2. POST /verify → 保障模块判定 RECOVERED / FAILED
   └─
```

### 3.3 联调前需对齐的一件事

> 联调说明 §7：探测输出样例对齐
>
> 采集侧需提供 Ryu 链路事件、ping 结果、流表 dump 各一份真实 JSON，**保障侧据此确认 `down_links` 等格式**。不一致时由保障侧写适配或采集侧调整格式。
>
> **本模块行动项**：在 `integration_demo.py` 首次运行时输出探测样例样张，提交给同学 A 确认。

---

## 4. 跨平台联调注意事项

### 4.1 部署环境（计划）

| 角色 | 运行环境 | 访问入口 |
|---|---|---|
| 采集模块（本系统） | macOS + Colima / Linux VM（Ubuntu 24.04） | `http://127.0.0.1:8080`（自营） + POST to 保障模块 |
| 意图保障模块（同学 B/C） | **Windows** | `http://<IP>:8000/api/v1/assurance/...` |
| 意图翻译模块 | 待确认 | 待其他同学提供 |
| 意图实现模块 | 待确认 | 待其他同学提供 |

### 4.2 跨平台坑（已识别 / 待识别）

1. **Windows 防火墙放行 8000**：联调前需确认同学 B/C 在 Windows 防火墙开了 8000 端口入站
2. **网络互通**：macOS / Linux 与 Windows 在同一局域网；保障模块机器 IP 待同学给
3. **Ryu vs os-ken**：联调说明要求 Ryu（§4.2 提到 Ryu 链路事件），本系统使用 **os-ken 2.8.1**（Ryu 官方继任者，API 兼容）。如评审严格需"Ryu"，在文档中说明替换理由
4. **时区**：ISO 8601 时间戳带 `+08:00`，双方时钟可不同步（用 ISO 字符串即可）
5. **JSON 编码**：macOS Python 默认 UTF-8，Windows Python 视版本可能是 GBK，统一强制 UTF-8

### 4.3 服务访问方式（计划）

- 浏览器打开 `http://<保障模块IP>:8000/docs` 有 Swagger UI，可手工试接口
- Swagger 可视化的好处：联调初期无需写客户端代码就能验证

---

## 5. 当前交付清单（本模块 + 保留项）

### 5.1 代码模块（**非测试 .py 共 28 个** + 10 个测试文件）

```
intensure/
├── 拓扑层
│   ├── topo_simple.py          # 4 主机 / 2 交换机线性
│   ├── topo_multi.py           # 6 主机 / 3 交换机
│   └── topo_mesh.py            # K4 网状 6 链路
├── 控制器层
│   ├── simple_switch_13.py     # 最简 L2 参考
│   ├── controller_app.py       # LLDP + PortStatus 链路事件流
│   └── state_server.py         # 自营 REST API（Flask，18 个端点，含 /metrics）✅ v3.19.0
├── 采集 / 状态层
│   ├── state_collector.py      # CLI 探测工具（手工验证用）
│   ├── actual_state_builder.py # 内部 state → ActualState 格式 ✅ v2.0
│   ├── state_model.py          # 统一状态模型
│   ├── state_history.py        # 历史快照（写 intensure.state_history 分区表）
│   └── live_env.py             # 常驻探测 + 上报 ✅ v2.0
├── 故障层
│   ├── fault_injector.py       # **9 种故障注入** ✅ v3.17.0
│   └── demo_full.py            # 端到端 demo
├── 上报 / 联调层 ✅ v2.0
│   ├── reporter.py             # POST /check + /verify（对方 assurance-agent）
│   ├── integration_demo.py     # 端到端联调脚本（real/mock/skip 三模式）
│   ├── mock_assurance_server.py# 本地 mock 保障模块（离线调试）
│   └── assurance_bridge.py     # 跨智能体推送桥（ASSURANCE_BASE 配地址）
├── 保障模块层 ✅ v3.7
│   └── assurance_mvp.py        # 一致性检查 + 根因诊断 + 写库
├── 持久化层 ✅ v3.5 ~ v3.15
│   ├── db_writer.py            # 选择性写库（21 方法）
│   ├── logging_setup.py        # 统一日志配置 ✅ v3.16.0
│   └── db_maintenance/         # alembic 基线生成 + 保留期清理脚本
└── 可视化层（加分项）
    └── dashboard.html          # 37KB，采集视角的单文件 dashboard
```

### 5.2 测试覆盖（当前）

| 测试项 | 状态 |
|---|---|
| pingall 0% 丢包 | ✅ |
| 链路断开 → 端口状态实时感知 | ✅ |
| 链路恢复 → 流量恢复 | ✅ |
| 加时延 → RTT 双向验证 | ✅ |
| LLDP 链路自动发现 | ✅ |
| 多交换机 / 网状拓扑自适应 | ✅ |
| 周期连通性探测 | ✅ |
| 复杂故障类型（burst / reorder / VLAN mismatch / flow race）| ✅ v3.17.0 |
| DB 自动重连（PG 重启后 0.91s 恢复）| ✅ v3.15.0 |
| 历史归档定时清理（systemd timer）| ✅ v3.15.0 |

**单元测试实测**（pytest 实际收集，非手工计数）：

| 测试文件 | 用例数 |
|---|---|
| `test_db_writer.py` | 39 |
| `test_state_model.py` | 26 |
| `test_assurance_mvp.py` | 24 |
| `test_ovs_logger.py` | 15 |
| `test_state_server.py` | 11 |
| `test_state_history.py` | 10 |
| `test_reporter.py` | 9 |
| `test_actual_state_builder.py` | 8 |
| `test_graceful.py` | 4 |
| `test_contract_version.py` | 11 |
| `test_metrics.py` | 10 |
| **合计** | **167** |

### 5.3 待补测试（联调相关）

- [x] ActualState 字段格式与 §4.2 一致性测试 —— `test_actual_state_builder.py`（8 例）
- [x] POST /check 上报样例（含 reachable=false 等异常态）—— `test_reporter.py`（9 例）
- [x] POST /verify 复测样例（含 reachable=true 恢复态）—— `test_reporter.py`
- [ ] 跨平台联调端到端：注册 → 激活 → 探测 → 上报 → 故障 → 修复 → 复测
      （**阻塞：对方 assurance-agent 的 cloudflared 隧道当前 HTTP 000**）

### 5.4 归档目录（`legacy/`）

> 早期探索材料，保留作为历史参考，不接入主流程。

```
legacy/
├── intent_registry.py        # 与翻译模块 child_intents 重叠
├── conflict_detector.py      # 与翻译模块 conflicts[] 重叠
├── conflict_resolver.py      # 与翻译模块 suggestions 重叠
├── healing_engine.py         # 与保障模块 /verify 重叠
├── healing_intent_generator.py
├── anomaly_detector.py       # 业务级异常与保障模块重叠
├── test_*.py                 # 对应测试
├── state_model.py            # 完整状态抽象（保障模块负责）
├── state_history.py          # 历史持久化（保障模块负责）
├── state_server.py           # 自营 REST（取消，被 reporter 替代）
├── PHASE3_PLAN_v1.md         # 旧路线图
└── dashboard_v6_v7_v8_v9.png # 旧截图
```

---

## 6. Demo 流程（计划版）

**5 分钟联调演示脚本**：

1. **环境就绪**（30s）
   - 启动 Colima VM → controller + Mininet
   - 启动采集模块（live_env + reporter）
   - 启动保障模块（同学 B/C 的 Windows:8000，Swagger UI 打开）

2. **意图注册与激活**（30s）
   - 手工 curl 注册 REQ-001-CI-001（财务 → 报销系统 HTTPS）
   - 激活进入监控
   - Swagger 显示 status=ACTIVE

3. **正常态上报**（30s）
   - 采集模块首次探测 → POST /check
   - 保障模块响应：violated=false，status=ACTIVE
   - Dashboard 显示 0 issue

4. **故障注入 + 异常上报**（60s）
   - `fault_injector.py link_down sw1:p1-sw2:p2`
   - 采集模块探测失败 → POST /check（down_links 含该链路）
   - 保障模块响应：violated=true，violations=[REACHABILITY_FAILURE]
   - Dashboard 显示告警

5. **修复 + 复测**（60s）
   - `fault_injector.py link_up`
   - 采集模块探测恢复 → POST /verify
   - 保障模块响应：status=RECOVERED
   - Dashboard 显示恢复

6. **总结**（30s）
   - Dashboard 截图 + 项目结构 + 接口契约文档

---

## 7. 接口契约索引

| 文档 | 内容 | 路径 |
|---|---|---|
| 采集模块联调说明 | 真实契约（采集模块 = 同学 A 写） | `~/Desktop/网络智能体/采集模块联调说明.md` |
| 意图翻译接口 v0.1 | 真实 schema（意图翻译模块写） | `~/Desktop/网络智能体/意图翻译智能体_案例1-6_接口确认稿_v0.1.pdf` |
| 下一阶段方案 | 本模块执行计划 | `~/workspace/projects/intensure/INTEGRATION_PLAN.md` |
| 旧版需求 v1.0 | 历史参考（已废弃） | `legacy/PHASE3_PLAN_v1.md` |
| 本模块 README | 使用指南 | `~/workspace/projects/intensure/README.md` |

---

## 8. 已知限制（诚实声明）

1. **ActualState 字段格式需联调前对齐**（§3.3）：首次跑 integration_demo.py 时需提交探测样例给同学 B/C 确认 `down_links` 格式
2. **保障模块 IP 未定**：同学 B/C 给出 Windows 机器 IP 后才能完整联调
3. **Ryu vs os-ken**：用 os-ken 替代 Ryu（API 兼容），如严格需 Ryu 可切回
4. **历史缓冲**：**v3.5 起本模块自己持久化历史**（`state_history.py` → `intensure.state_history` 月分区表，
   另有 `link_events` / `port_events` / `probe_results` 等分区表 + `retention-cleanup.timer` 每日清理）；
   早期"只做采集、历史交给保障模块"的约定已随 v3.5 数据库落地而改变。采集视角 dashboard 仍主要显示当前 + 近期窗口
5. **时区与编码**：跨平台联调时强制 ISO 8601 + UTF-8，避免 GBK 编码问题

---

## 9. 待办清单

- [ ] 等对方（assurance-agent）提供可用访问入口 —— **当前 cloudflared 隧道 HTTP 000，不通**
      （地址通过环境变量 `ASSURANCE_BASE` / `ASSURANCE_BASE_FALLBACK` 配置，不写死在文档里）
- [ ] 跨平台联调一次跑通 —— 阻塞于上一项
- [x] 写 `actual_state_builder.py`（采集 → ActualState 格式）—— v2.0
- [x] 写 `reporter.py`（POST /check + `/verify`）—— v2.0
- [x] 写 `integration_demo.py`（端到端联调脚本）—— v2.0
- [x] 改造 `live_env.py` 为"探测 + 上报"模式 —— v2.0
- [x] 补齐 `mock_assurance_server.py`（本地 mock，离线可调试）—— v2.0
- [x] 写 `state_server.py`（自营 REST，16 个端点）—— 后续版本启用，非归档

---

📁 项目根目录：`~/workspace/projects/intensure/`
🖥️ VM 路径：`/home/sunxiaoxuan.guest/intensure/`
🌐 Dashboard（采集视角）：`http://127.0.0.1:18080/`（自营）
🌐 保障模块 Swagger：`http://<同学 B/C 的 IP>:8000/docs`（待提供）

**文档维护**：用户随时可改，同步更新。
---

## 🕘 最近更新

**2026-10-29 · 文档同步至当前实现状态**

以代码为准核对后修正（原因：本文停留在 v2.0 时期，部分"待做"其实早已完成）：

| 位置 | 原写法 | 改为 |
|---|---|---|
| §2 工作范围 | v2.0 起只做「采集 + 上报」| 补上 v3.7 起重新扩展（`assurance_mvp.py` + 数据库体系）|
| §5.1 模块树 | 「核心 .py 11 个」，上报层标 **❌ 待做** | 非测试 .py **28 个** + 10 个测试；上报层 / 保障层 / 持久化层均为已完成 |
| §5.1 故障注入 | 5 种 | **9 种**（含 burst / reorder / VLAN mismatch / flow race）|
| §5.1 dashboard | 51KB | **37KB** |
| §5.2 测试覆盖 | 无量化 | 补 **167 例**逐文件实测表 |
| §5.3 待补测试 | 4 项全未做 | 前 3 项已完成并标注对应测试文件 |
| §8 #4 历史缓冲 | 「本模块不再持久化历史」| **v3.5 起自己持久化**（分区表 + 每日清理 timer）|
| §9 待办清单 | 写着「写 reporter.py」「改造 live_env.py」等 | 已完成项勾选并标注版本；阻塞项换成真实原因（隧道 HTTP 000）|

> ⚠️ **称谓约定**：本文 §1 用「同学 A = 状态采集 = 本系统」；
> 而 `assurance_bridge.py` / `README.md` / 联调文档里的「A 同学」指**对方 assurance-agent（保障模块）**。
> 两套叫法并存容易误读 —— 本文涉及对方模块处已改用**功能性称呼**（「对方 / assurance-agent」）。
> 归属以课程组安排为准，此处不做改名。

> 核对方式：以 `docs/database-schema.sql`、`fault_injector.py`、`state_server.py`、`db_writer.py`、
> `assurance_mvp.py` 与 pytest 实际收集结果为准。**未新增任何代码里还没实现的功能。**
