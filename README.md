# 采集模块（Mininet + os-ken）· 意图驱动网络管理系统

> 意图驱动网络管理系统 · **采集层**（ · Part A v2.0）

## 🎯 一句话概述

本系统是意图驱动网络管理系统的**采集模块**：

- **运行环境**：macOS（宿主机）+ Colima 跑的 Ubuntu 24.04 VM（x86_64, 2 核 2GB）
- **网络模拟**：Mininet 2.3.0 + Open vSwitch 3.3.4
- **SDN 控制器**：os-ken 2.8.1（Ryu 官方继任者，API 兼容）
- **对外接口**：向同学 A 的"意图保障模块"上报 ActualState（HTTP POST）
- **采集视角 dashboard**：单文件 SPA（`dashboard.html`）作为加分项

> 📌 **范围重定义**：v2.0（2026-09-03）起，本系统只做"采集 + 上报"，不再做一致性校验、冲突检测、自愈决策（这些由其他同学模块负责）。

---

## ✅ 完成状态

### ✅ 已完成（v1.0 ~ v3.9,2026-08-04 ~ 2026-10-08）

| 任务 | 状态 |
|---|---|
| Phase 1 环境搭建（Colima + Mininet + os-ken + OVS） | ✅ v1.0 |
| 自定义拓扑（4 主机 2 交换机 / 多分支 / K4 网状） | ✅ v1.0 `topo_simple.py` / `topo_multi.py` / `topo_mesh.py` |
| 状态采集（交换机/主机/端口/流表/事件） | ✅ v1.0 `controller_app.py` |
| 链路事件监听（LLDP 自动发现 / PortStatus） | ✅ v1.0 控制器内置 |
| 故障注入（断链 / 时延 / 丢包 / 限速） | ✅ v1.0 `fault_injector.py` |
| 周期可达性探测（pingall / 流表 dump） | ✅ v1.0 `live_env.py` |
| 端到端 demo（拓扑 + 流量 + 故障 + 恢复） | ✅ v1.0 `demo_full.py` |
| 采集视角 dashboard（拓扑 / 链路 / 事件 / 自检） | ✅ v1.0 `dashboard.html` |
| **`actual_state_builder.py`** —— 内部 state → ActualState 格式（联调 §4.2） | ✅ v2.0 (2026-09-03) |
| **`reporter.py`** —— POST `/check` + `/verify` HTTP 客户端 | ✅ v2.0 (2026-09-03) |
| **`integration_demo.py`** —— 端到端联调脚本（real/mock/skip 三模式） | ✅ v2.0 (2026-09-03) |
| **`mock_assurance_server.py`** —— 本地 mock 保障模块（离线调试） | ✅ v2.0 (2026-09-03) |
| **`live_env.py` v2.0** —— 周期探测 + ActualState 上报（用 builder + reporter） | ✅ v2.0 (2026-09-03) |
| Dashboard 「自愈前后对比」面板（基于真实 link events + ping history） | ✅ v3.0 (2026-09-03) |
| REQUIREMENTS.md §1 同学标签全面纠正（11 处）| ✅ v3.0 |
| 单元测试：`test_actual_state_builder.py`（21/21 通过） | ✅ v2.0 |
| 单元测试：`test_reporter.py`（24/24 通过） | ✅ v2.0 |
| 归档旧版 Phase 3 探索（冲突 / 自愈 / 异常检测 / state_server） | ✅ v2.0 `legacy/` |
| **数据库架构 Phase 0**（5 schema + 43 表 + 4 模块 GRANT + 保留策略）| ✅ v3.5 (2026-10-08) |
| **intensure 选择性写库**（db_writer.py 12 方法 + 30s 节流 + 异常才写）| ✅ v3.6 (2026-10-08) |
| **保障模块 MVP**（assurance_mvp.py 8 状态机 + 14 guard_checks + E2E 演示）| ✅ v3.7 (2026-10-08) |
| **一键 E2E 演示**（demo_db_pipeline.sh 8 步全绿）+ 汇报材料 | ✅ v3.8 (2026-10-08) |
| **心跳默认关**（生产 INTENSURE_HEARTBEAT=1 启用）+ 演示速查文档 | ✅ v3.9 (2026-10-08) |

### 🎯 下一步（我们主动要做）

| 任务 | 优先级 | 备注 |
|---|---|---|
| 三方联调真实数据完整闭环（intensure → A 同学 service）| 🟡 P1 | assurance_bridge 已接，需跑真实数据 |
| 修复 `reporter.py` 频率 + DNS 老 bug（10-19 联调发现）| 🟡 P1 | 老 bug 100% 失败 |
| 性能压测（100 意图场景的负载 + 存储）| 🟢 P2 | VM 17G 可用 |
| 复杂故障类型扩展（burst / reorder / VLAN mismatch）| 🟢 P2 | 让 assurance-agent 诊断器有更多样本 |
| Dashboard 增强（历史回放 / 跳数对比）| 🟢 P2 | 演示后看反馈再加 |
| IPv6 / BGP 支持（如项目要求）| 🔵 P3 | 长期 |

> **我们不做的**（边界）：翻译组 / 冲突消解组 / A 同学 assurance-agent 内部 / 协调三组（那是组长的活）

---

## 🚀 日常使用

### 0️⃣ 环境搭建（一次性，新机器）

**VM 内 apt 装系统依赖**（推荐）：

```bash
sudo apt install -y python3-mininet openvswitch-switch python3-os-ken python3-psycopg2 python3-flask python3-flask-cors python3-requests
```

**或 venv 装 Python 依赖**（跨平台）：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

测试 / lint 用：

```bash
pip install -r requirements-dev.txt
```

### 1️⃣ 启动 VM（每次开电脑后）

```bash
colima start
```

### 2️⃣ 一键跑端到端 demo（采集模块内部）

```bash
colima ssh
cd /home/sunxiaoxuan.guest/intensure
sudo -E python3 -u demo_full.py
```

预期输出：pingall 0% 丢包、流表下发、断链感知 ✓、时延 200ms ✓、最终验证 0 issue。

### 3️⃣ 采集模块 + Dashboard（自营视图）

```bash
# 终端 1: 控制器 + 自营 dashboard（采集视角）
cd /home/sunxiaoxuan.guest/intensure
sudo -E python3 -u run_all.py

# 终端 2: 常驻 Mininet + 周期探测
sudo -E python3 -u live_env.py

# 终端 3 (macOS): 端口隧道
ssh -F ~/.colima/ssh_config -N -L 18080:127.0.0.1:8080 colima
```

浏览器打开 **http://127.0.0.1:18080/** 看采集视角 dashboard。

### 4️⃣ 端到端联调（含上报到同学 A 的保障模块）

#### 模式 A：本地 mock 调试（不需要同学 A 在场）

```bash
colima ssh
cd /home/sunxiaoxuan.guest/intensure

# 1. 跑 integration_demo.py（--mode mock 会自动启动 mock server）
python3 integration_demo.py --mode mock --intent-id REQ-001-CI-001
```

预期输出：
- 6 步流程：注册 → 激活 → 周期探测 → 故障注入 → 修复 → 复测
- 每次 POST 打印 `HTTP 200 (XXms)` 或错误码
- 末尾输出 Reporter 统计 + ActualState 样例

#### 模式 B：真实联调（已有 A 同学服务地址）

```bash
# 终端 1: 同学 A 在他 Windows 机器上跑保障模块
#   uvicorn main:app --host 0.0.0.0 --port 8000
#   浏览器打开 http://<他的IP>:8000/docs 看 Swagger

# 终端 2: 采集模块 + 联调 demo（本系统）
colima ssh
cd /home/sunxiaoxuan.guest/intensure
python3 integration_demo.py \
  --mode real \
  --assurance-url http://<同学A的IP>:8000/api/v1/assurance \
  --intent-id REQ-001-CI-001
```

`integration_demo.py` 会自动：
1. 启动 controller + REST（localhost:8080）
2. 启动 Mininet 拓扑（4 主机 2 交换机）
3. 等待交换机/主机发现
4. 注册测试意图（POST `/register-request`）
5. 激活（POST `/activate`）
6. 周期探测 → POST `/check`（异步不阻塞）
7. 注入 s1-s2 链路故障 → 等失败 → POST `/check`
8. 修复链路 → POST `/verify`
9. 输出 Reporter 统计 + ActualState 样例（给同学 A 对齐字段用）

#### 模式 C：常驻实况 + 持续上报（生产模式）

```bash
colima ssh
cd /home/sunxiaoxuan.guest/intensure

# 配置上报参数
export ASSURANCE_URL=http://<同学A的IP>:8000/api/v1/assurance
export INTENT_ID=REQ-001-CI-001
export PROBE_INTERVAL=15
export SRC_HOST=10.0.0.1
export DST_HOST=10.0.0.4

python3 live_env.py
```

`live_env.py` 会无限循环：
- 每 15s 跑 pingall + 时延测量
- 读 controller state → 组装 ActualState → POST `/check`
- 所有上报异步（不阻塞探测循环）

### 5️⃣ 关掉一切

```bash
colima ssh -- sudo pkill -9 -f 'run_all[.]py'
colima ssh -- sudo pkill -9 -f 'live_en[v]'
colima ssh -- sudo pkill -9 -f 'integration_demo[.]py'
colima ssh -- sudo mn -c
colima stop
```

---

## 🌐 对外接口（**采集 → 保障模块**）

> 详细契约见 `~/Desktop/网络智能体/采集模块联调说明.md`（同学 A 写的真实契约，硬约束）

### 上报端点

| 方法 | 路径 | 触发时机 |
|---|---|---|
| `POST` | `/api/v1/assurance/check` | 每次探测后 |
| `POST` | `/api/v1/assurance/verify` | 修复后复测 |

### ActualState 字段（关键）

| 字段 | 必填 | 格式 |
|---|---|---|
| `intent_id` | ✅ | 字符串 |
| `checked_at` | ✅ | ISO 8601（如 `"2026-09-03T10:05:00+08:00"`） |
| `reachable` | ✅ | bool |
| `expected_flow_present` | ✅ | bool |
| `source_host` / `destination_host` | ⬜ | IP 字符串 |
| `latency_ms` / `packet_loss` | ⬜ | 数字 / null |
| `down_links` | ⬜ | `["sw1:p1-sw2:p2"]`（**字符串格式**） |
| `down_ports` | ⬜ | `["sw1:p1"]` |
| `conflicting_flows` | ⬜ | `[{switch, cookie}]` |

### 错误码处理

| 错误 | 含义 | 行动 |
|---|---|---|
| 200 | 正常 | 继续 |
| 404 | 意图未注册 | 报错（一般是注册流程出问题） |
| 409 | 状态机前置条件不满足 | 轮询 `/record/{intent_id}` 看当前状态 |
| 5xx | 保障模块异常 | 指数退避重试 |

---

## 📂 文件清单（v2.0 重构后）

```
intensure/
├── 拓扑层
│   ├── topo_simple.py          # 单链 2 交换机 4 主机
│   ├── topo_multi.py           # 3 交换机 6 主机
│   └── topo_mesh.py            # K4 网状 4 交换机
├── 控制器层
│   ├── simple_switch_13.py     # 最简 L2
│   └── controller_app.py       # 链路事件采集（LLDP + PortStatus）
├── 采集层
│   ├── state_model.py          # 内部数据抽象（供 ActualStateBuilder 读）
│   ├── state_collector.py      # CLI 工具（手工验证）
│   └── live_env.py             # ⚠️ 需改造：探测 + 调 reporter 上报
├── 故障层
│   ├── fault_injector.py       # 5 种故障注入
│   ├── demo_full.py            # 内部 demo
│   └── verify_live.py          # 实况探测验证
├── 上报层（❌ 待做）
│   ├── actual_state_builder.py # 内部 state → ActualState
│   ├── reporter.py             # POST /check + /verify
│   └── integration_demo.py     # 端到端联调
├── 可视化层（加分项）
│   └── dashboard.html          # 采集视角单文件 SPA
├── 启动器
│   ├── run_all.py              # 控制器 + 自营 dashboard
│   └── start_env.sh            # 一键启动（旧版）
├── 文档
│   ├── REQUIREMENTS.md         # 职责 + 联调契约背景（v2.0）
│   ├── INTEGRATION_PLAN.md     # 下一步方案（v1.0）
│   ├── STATE_MODEL.md          # 内部 state 抽象（参考）
│   ├── PHASE3_PLAN.md          # 旧路线图（待归档到 legacy/）
│   └── README.md               # 本文件
└── legacy/                     # 历史探索材料
    ├── intent_registry.py
    ├── conflict_detector.py
    ├── conflict_resolver.py
    ├── healing_engine.py
    ├── healing_intent_generator.py
    ├── anomaly_detector.py
    ├── state_history.py
    ├── state_server.py
    ├── test_*.py
    └── PHASE3_PLAN_v1.md
```

---

## 🤝 与其他同学的接口

| 同学 | 模块 | 我们提供的 | 他们提供的 |
|---|---|---|---|
| **同学 A** | 意图保障模块（Windows:8000） | ActualState via POST `/check` + `/verify` | 状态查询 `/record/{id}`、Swagger UI `/docs` |
| **同学 C** | 意图翻译模块 | — | 翻译后的 child_intent JSON（含 `intent_id`） |
| **同学 B** | 意图实现模块 | — | 配置下发（修复链路等） |

> ⚠️ **关键约束**：本系统**只 POST 不读 GET**。所有"当前状态"由保障模块管理（它的 `/record/{intent_id}`），采集模块不维护。

---

## 🛠️ 踩过的坑（血泪史）

### 环境类
1. **Multipass qemu-img 段错误** → 用 Colima + Lima
2. **Ryu 装不上**（setup.py 用已删除的 setuptools API）→ 用 os-ken
3. **pypi SSL 失败**（VPN）→ 阿里云源
4. **osken-manager 找不到 app** → `PYTHONPATH=$PWD`

### 代码类
5. **`global X` 必须在使用前声明**
6. **eventlet.monkey_patch() 替换 select** → Mininet import 失败
7. **子进程 stdout 必须有人读** → 重定向到文件
8. **os-ken vs Ryu API 差异**：`get_protocol() is not None` / `instructions` 字段
9. **`getattr(x, 'a', x.b)` 默认参数立即求值** → 用 `getattr(x, 'a', None)`
10. **Mininet 没有 `Mininet.config`** → 直接用 `node.cmd()` 跑底层命令
11. **OVS local 端口永远 LINK_DOWN** → 验证时跳过 OFPP_LOCAL
12. **`pkill -f` 会匹配自身** → 单独跑 pkill 命令
13. **tar 恢复覆盖新代码** → 先备份再改

### 跨平台联调（待踩坑）
- Windows 防火墙放行 8000 端口
- JSON 编码统一 UTF-8（macOS Python 默认，Windows 需强制）
- dpid 数字格式 → "swN:pN" 字符串格式（联调契约硬约束）

---

## 🔮 下一步（待用户确认后开干）

详见 [`INTEGRATION_PLAN.md`](./INTEGRATION_PLAN.md)：

1. **阶段 1（0.5 天）**：`actual_state_builder.py` —— 内部 state → ActualState 格式
2. **阶段 2（0.5 天）**：`reporter.py` —— POST 客户端
3. **阶段 3（1 天）**：`integration_demo.py` —— 端到端联调
4. **阶段 4（0.5-1 天）**：联调修正
5. **阶段 5（0.5 天）**：Dashboard 调整
6. **阶段 6（0.5 天）**：文档定稿

**预计总工作量**：4 天

---

## 🧭 LLDP 拓扑自动发现（保留功能）

控制器内置 LLDP 发现，采集模块自带的拓扑可视化完全数据驱动：

- 每 3s 向所有数据口发 LLDP
- 端口 down → 立即移除相关链路；超时 30s 兜底清理
- dashboard 拓扑图节点和连线**全部来自真实数据**（不是 hardcoded）

---

📁 项目根目录：`~/workspace/projects/intensure/`
🖥️ VM 路径：`/home/sunxiaoxuan.guest/intensure/`
🌐 采集视角 Dashboard：`http://127.0.0.1:18080/`
🌐 保障模块 Swagger：`http://<同学 A 的 IP>:8000/docs`（待提供）

**维护者**：开发团队