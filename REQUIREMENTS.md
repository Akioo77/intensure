# 采集模块（Mininet + Ryu/os-ken）— 项目需求与契约

> 应用场景：园区网络 · 意图驱动网络管理系统的**采集层**
> 文档版本：v2.0 (2026-09-03 重构)
> 编写：庄英琪代笔，主人审核
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
              意图智能体（3 个同学分工）              采集模块（同学 A = 本系统 = 主人）
┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ 意图翻译    │ ──→ │ 意图实现    │ ──→ │ 意图保障    │ ←─ │  采集模块   │
│ 智能体      │    │ 智能体      │    │ 智能体      │   │ (Mininet +  │
│ (同学 B/C)  │ ←── │ (同学 B/C)  │ ←── │ (同学 B/C)  │   │  Ryu/os-ken)│
└─────────────┘    └─────────────┘    └─────────────┘   └─────────────┘
                                          │
                                    ActualState
                                    (POST /check)
```

> ⚠️ **同学标签修正（2026-09-03）**：v2.0 文档此处曾误标为「同学 A = 意图保障」，主人澄清后修正。
> - **同学 A = 状态采集**（采集模块）= 本系统 = 主人
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

## 2. 我的工作范围（v2.0 重新定义）

> ⚠️ **范围调整说明**：v1.0 文档（2026-08-08）将本系统定义为"状态采集与数字化网络状态"，包含一致性校验、冲突检测、自愈回路等。**实际项目分工中，这些由其他同学的智能体模块（翻译/实现/保障）实现**。
>
> 本文档基于 `采集模块联调说明.md`（2026-09-02 版）重新定义范围，**我只做采集与上报**，不实现业务逻辑（一致性校验、冲突消解、自愈决策由其他模块负责）。

### 2.1 采集模块职责清单

| # | 职责 | 状态 |
|---|---|---|
| 1 | Mininet 场景搭建（拓扑 + 故障注入） | ✅ 已完成 |
| 2 | 网络状态采集（拓扑 / 主机 / 链路 / 流表） | ✅ 已完成 |
| 3 | 链路事件监听（down / up） | ✅ 已完成 |
| 4 | 实际可达性探测（pingall / iperf / 时延测量） | ✅ 已完成 |
| 5 | 流表 dump 与期望比对（`expected_flow_present`） | ✅ 已完成 |
| 6 | **ActualState 组装**（符合联调说明 §4.2 字段约定） | ❌ 待做 |
| 7 | **POST `/check` 上报**（探测后调用） | ❌ 待做 |
| 8 | **POST `/verify` 上报**（修复后复测） | ❌ 待做 |
| 9 | 端到端联调 Demo（注册 → 激活 → 探测 → 上报 → 故障 → 复测） | ❌ 待做 |
| 10 | 采集视角的可视化（Dashboard 加分项） | ✅ 已完成 |

### 2.2 不在本模块范围内的职责（明确边界）

| 职责 | 实际归属 | 说明 |
|---|---|---|
| 意图翻译（NL → JSON） | 翻译模块（同学 B/C） | v0.1 schema 已固化 |
| 冲突检测 / 消解 | 翻译模块（同学 B/C） | schema 中 `conflicts[]` 已含 |
| 一致性校验 | 保障模块（同学 B/C） | Windows:8000 FastAPI |
| 自愈决策 / 根因诊断 | 保障模块（同学 B/C） | `/verify` 端点处理 |
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

### 5.1 代码模块（**核心 .py 11 个**）

```
intensure/
├── 拓扑层（保留）
│   ├── topo_simple.py          # 4 主机 / 2 交换机线性
│   ├── topo_multi.py           # 6 主机 / 3 交换机
│   └── topo_mesh.py            # K4 网状 6 链路
├── 控制器层（保留，简化）
│   ├── simple_switch_13.py     # 最简 L2 参考
│   └── controller_app.py       # LLDP + PortStatus 链路事件流
├── 采集客户端（保留）
│   ├── state_collector.py      # CLI 探测工具（手工验证用）
│   └── live_env.py             # 常驻探测（需改造为"探测 + 上报"）
├── 故障层（保留）
│   ├── fault_injector.py       # 5 种故障注入
│   └── demo_full.py            # 端到端 demo（需改造）
├── 上报层（❌ 待做）⭐
│   ├── actual_state_builder.py # 内部 state → ActualState 格式
│   ├── reporter.py             # POST /check + /verify
│   └── integration_demo.py     # 端到端联调脚本
└── 可视化层（保留，作为加分项）
    └── dashboard.html          # 51KB，采集视角的单文件 dashboard
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

### 5.3 待补测试（联调相关）

- ActualState 字段格式与 §4.2 一致性测试
- POST /check 上报样例（含 reachable=false 等异常态）
- POST /verify 复测样例（含 reachable=true 恢复态）
- 跨平台联调端到端：注册 → 激活 → 探测 → 上报 → 故障 → 修复 → 复测

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
4. **历史缓冲**：本模块不再持久化历史（保障模块负责），采集视角的 dashboard 仅显示当前 + 近期窗口
5. **时区与编码**：跨平台联调时强制 ISO 8601 + UTF-8，避免 GBK 编码问题

---

## 9. 待办清单

- [ ] 等同学 B/C 提供保障模块 IP / Swagger 文档
- [ ] 等同学 B/C 提供各自模块的访问入口
- [ ] 写 `actual_state_builder.py`（采集 → ActualState 格式）
- [ ] 写 `reporter.py`（POST /check + /verify）
- [ ] 写 `integration_demo.py`（端到端联调脚本）
- [ ] 改造 `live_env.py` 为"探测 + 上报"模式
- [ ] 跨平台联调一次跑通

---

📁 项目根目录：`~/workspace/projects/intensure/`
🖥️ VM 路径：`/home/sunxiaoxuan.guest/intensure/`
🌐 Dashboard（采集视角）：`http://127.0.0.1:18080/`（自营）
🌐 保障模块 Swagger：`http://<同学 B/C 的 IP>:8000/docs`（待提供）

**文档维护**：主人随时可改，庄英琪同步更新。