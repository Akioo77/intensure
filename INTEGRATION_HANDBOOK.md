# 联调手册 · 网络意图智能体（intensure ↔ assurance-agent）

> **版本**: v1.0（2026-09-12）
> **状态**: 🟢 端到端 mock 联调 100% 通过；真实联调已启动（依赖 A 同学 tunnel 存活）
> **作者**: 主人 + 庄英琪
> **适用**: 9/13-9/15 演示冲刺 + 三智能体联调实战

---

## 📑 目录

- [1. 联调方法](#1-联调方法)
  - [1.1 核心理念](#11-核心理念)
  - [1.2 三种联调模式](#12-三种联调模式)
  - [1.3 完整意图生命周期（8 步）](#13-完整意图生命周期8-步)
  - [1.4 双轨测试策略](#14-双轨测试策略)
- [2. 任务清单](#2-任务清单)
  - [2.1 已完成 ✅](#21-已完成-)
  - [2.2 进行中 🔄](#22-进行中-)
  - [2.3 待办（按优先级）](#23-待办按优先级)
- [3. 日志目录](#3-日志目录)
  - [3.1 实时运行日志（/tmp/）](#31-实时运行日志tmp)
  - [3.2 正式测试报告（test_reports/）](#32-正式测试报告test_reports)
  - [3.3 演示截图（demo_screenshots/）](#33-演示截图demo_screenshots)
  - [3.4 脚本环境配置（demo_scripts/.env）](#34-脚本环境配置demo_scriptsenv)
  - [3.5 项目代码内嵌日志](#35-项目代码内嵌日志)

---

## 1. 联调方法

### 1.1 核心理念

> **intensure（采集） → assurance-agent（保障）**：我们只负责"看见网络"，不做任何业务判断；保障模块消费 ActualState 并返回 verdict。

| 项目 | 职责 | 边界 |
|---|---|---|
| **intensure（我们）** | 采集、组装、上报 ActualState | ❌ 不做一致性校验、❌ 不做冲突检测、❌ 不做自愈决策 |
| **assurance-agent（A 同学）** | 消费 ActualState，做一致性校验 + 诊断 + 自愈意图生成 | ✅ 决策权在这里 |
| **意图翻译（B 同学）** | 把自然语言意图翻译成 JSON | 🔄 通过 assure 注册到我们的状态空间 |
| **意图实现（待确认）** | 把 JSON 意图下发给网络 | 🔄 我们的状态采集给实现效果反馈 |

### 1.2 三种联调模式

| 模式 | 触发 | 用途 | 命令 |
|---|---|---|---|
| **mock** | `integration_demo.py --mode mock` | 离线端到端验证，不依赖外部模块 | `python integration_demo.py --mode mock` |
| **real** | `integration_demo.py --mode real` | 调用 A 同学真实 assurance-agent | `python integration_demo.py --mode real --assurance-url https://xxx.trycloudflare.com` |
| **skip** | `integration_demo.py --mode skip` | 只跑采集部分，不上报 | （开发采集模块用） |

> 💡 真实联调必须保证：① A 同学 `cloudflared tunnel` 在跑 ② DNS 域名能解析 ③ `/api/health` 返回 200

### 1.3 完整意图生命周期（8 步）

> 📁 对应脚本：`demo_scripts/01_register_intent.sh` ~ `08_verify_recovered.sh`

```
[用户/翻译智能体]
   │  自然语言意图
   ▼
① register ──────────► POST /api/v1/assurance/register
   │                    意图 JSON（intent_id + 期望状态）
   ▼
② activate ──────────► POST /api/v1/assurance/activate
   │                    状态机: REGISTERED → ACTIVE
   ▼
[assurance-agent] 把意图写入「期望状态库」
   │
   ▼
[intensure 周期探测] ──► ActualState（实际状态）
   │                       ↓
   │                  ③ check baseline ─► POST /api/v1/assurance/check
   │                  status=ACTIVE, violations=[]
   │
   ▼
[人为注入故障 / 自然发生]
   │
   ▼
   [intensure 再次探测] ──► 异常 ActualState
   │                       ↓
   │                  ⑤ check violated ─► POST /api/v1/assurance/check
   │                  status=ACTIVE, violations=[...]
   │
   ▼
[assurance-agent 触发诊断]
   │
   ▼
⑥ diagnose ──────────► POST /api/v1/assurance/diagnose?intent_id=...
                       返回: root_cause + evidence_chain
   │
   ▼
[assurance-agent 决定是否自愈]
   │
   ▼
⑦ heal ──────────────► POST /api/v1/assurance/heal?intent_id=...
                       生成 healing_intent + 14 项 guard_checks
                       verdict: ALLOW / BLOCKED
   │
   ▼
[自愈意图下发 → 网络修复]
   │
   ▼
   [intensure 再次探测] ──► 恢复 ActualState
                           ↓
                  ⑧ verify recovered ─► POST /api/v1/assurance/verify
                                      status=ACTIVE, violations=[]
```

**关键产物**：
- `01_register_intent.sh`：写出 `.env`（INTENT_ID + BASE URL），后续 7 步共享
- `00_pre_check.sh`：5 项预检（VM / 隧道 / intensure / assurance-agent / Mininet）
- `99_reset.sh`：清空所有状态，恢复初始环境

### 1.4 双轨测试策略

| 测试层级 | 工具 | 时机 | 产出 |
|---|---|---|---|
| **单元测试** | `test_actual_state_builder.py` (21 个) + `test_reporter.py` (24 个) | 改完代码后必跑 | 退出码 0 |
| **集成测试** | `integration_demo.py`（mock / real / skip 三模式） | 联调前/演示前必跑 | `test_reports/*.md` |
| **端到端联调** | `demo_scripts/01-08.sh` | 演示当天 + A 同学有真实模块时 | `/tmp/0[1-8]_*.log` |
| **冒烟测试** | `00_pre_check.sh` | 任何操作前（环境预检） | 5 项全绿才能继续 |

---

## 2. 任务清单

### 2.1 已完成 ✅

#### 9/3 之前（采集模块本体）
| 任务 | 文件 | 行数 |
|---|---|---|
| 环境搭建（Colima + Mininet + os-ken + OVS） | — | — |
| 自定义拓扑（simple / multi / mesh） | `topo_*.py` | — |
| 状态采集（交换机/主机/端口/流表/事件） | `controller_app.py` | 549 |
| 链路事件（LLDP + PortStatus） | `controller_app.py`（内置）| — |
| 故障注入（断链/时延/丢包/限速） | `fault_injector.py` | — |
| 周期可达性探测 | `live_env.py` | 197 |
| Dashboard 单文件 SPA | `dashboard.html` | — |

#### 9/3（联调基础）
| 任务 | 文件 | 行数 |
|---|---|---|
| ActualState 组装（内部 → 联调 §4.2 格式）| `actual_state_builder.py` | 245 → 285 |
| Reporter（POST `/check` + `/verify` 客户端）| `reporter.py` | 283 |
| 端到端联调脚本（mock/real/skip 三模式）| `integration_demo.py` | 574 |
| Mock 保障模块（FastAPI）| `mock_assurance_server.py` | 281 |
| 联调对接文档 | `INTEGRATION_GUIDE.md` | — |
| 单元测试（21 builder + 24 reporter）| `test_*.py` | — |

#### 9/7-9/8（真实联调启动）
| 任务 | 文件 | 行数 |
|---|---|---|
| state_history 历史系统 | `state_history.py` | — |
| 跨智能体对接文档（一致性模型）| `STATE_MODEL.md` | — |
| 一致性检验示例 | `consistency_check.py` | — |
| Dashboard 调试面板清理 + 自愈前后对比 | `dashboard.html` | — |
| 联调 plan v2 | `INTEGRATION_PLAN.md` | — |

#### 9/11（演示脚本集）
| 任务 | 文件 | 行数 |
|---|---|---|
| 12 个演示脚本（含 README + ui.sh）| `demo_scripts/` | 1823 |

#### 9/12（真实联调 + 根因归因 + 契约修复）
| 任务 | 文件 | 行数 |
|---|---|---|
| **OVS 详细日志采集**（谁让端口 up/down）| `ovs_logger.py` | 397 |
| **state_server 新增 OVS 端点** | `state_server.py`（+136 行）| — |
| **run_all 集成 OVSLogger 启停** | `run_all.py`（+21 行）| — |
| **完整意图生命周期 9 个联调脚本** | `demo_scripts/01-08 + 99` | — |
| **ActualState down_links 格式修复** | `actual_state_builder.py` | +91/-31 |
| **契约格式升级：sw:pN → s:ethN** | （含测试更新）| +8/-8 |

### 2.2 进行中 🔄

| 任务 | 谁 | 阻塞点 |
|---|---|---|
| A 同学 cloudflared tunnel 稳定性 | A 同学 | 域名 NXDOMAIN 时无法联调 |
| 真实联调实战 | 我们 | 等 tunnel 稳定后立即跑 `01-08` |
| 故障注入 REST 接口 `POST /api/v1/test/inject-fault` | 我们 | intensure 还没实现（脚本已 fallback 到 VM 手动执行）|

### 2.3 待办（按优先级）

#### 🔴 P0（演示前必做）

- [ ] **演示讲稿**（15-20 分钟完整剧本，含每步讲什么 / 看什么 / 解释什么）
- [ ] **Q&A 预案**（15-20 个老师可能问的问题 + 答案）
- [ ] **README 重写**（当前 README 还写"等同学 A"，已过时）
- [ ] **跑通真实联调** 至少 1 次（确认 A 同学接口真能用）

#### 🟡 P1（演示加分项）

- [ ] **CHANGELOG.md 更新**（v3.x：OVS logger + 联调脚本 + 契约格式升级）
- [ ] **架构图**（3 智能体 + intensure 的位置，用 diagram-maker 画）
- [ ] **PPT 准备**（如老师要求幻灯片，create-pptx + Apple 风）
- [ ] **故障注入 REST 路由**（`/api/v1/test/inject-fault` Flask 路由，15 分钟工作量）
- [ ] **跨智能体对接文档 v2**（OVS log 端点 + 新 down_links 格式加入 STATE_MODEL.md）

#### 🟢 P2（演示后优化）

- [ ] 真实园区迁移路径文档（已在 9/8 写完，需更新到 INTEGRATION_GUIDE.md）
- [ ] collector 多协议适配器（SNMP / CLI / OpenFlow）骨架
- [ ] OVS log → 根因归因的 demo（直接展示 `ovs_logger` 抓到"是谁下的 ofctl 命令"）
- [ ] Dashboard 加联调统计面板（上报成功率 / 响应时间 / ActualState 样例）

---

## 3. 日志目录

### 3.1 实时运行日志（/tmp/）

> 📁 每次跑 demo 脚本的实时输出都在这里，**带时间戳**，方便事后追溯。

| 文件名模式 | 来源 | 内容 | 保留策略 |
|---|---|---|---|
| `/tmp/0[0-9]_out.log` | `demo_scripts/0*.sh` 主输出 | curl 响应 + UI 提示 + verdict | 每次跑前覆盖 |
| `/tmp/0[0-9]_trace.log` | `set -x` 跟踪 | 完整命令跟踪（每条 shell 命令 + 参数）| 每次跑前覆盖 |
| `/tmp/demo_dryrun.log` | `SKIP_WAIT=1 bash demo_all.sh` | 完整 dry-run 演示（748 行）| 长期保留 |
| `/tmp/intensure_*.log` | `run_all.py` 内 | 控制器启动日志（待确认）| — |
| `/tmp/controller_app*.log` | os-ken 控制器 | OpenFlow 消息 + 端口事件 | — |

**典型 trace 例子**（来自 `/tmp/03_trace.log` 末尾）：

```bash
+ ui_section 'POST /api/v1/assurance/check (Baseline)'
+ RESP=$(curl -s -X POST "$BASE/api/v1/assurance/check" ...)
+ ui_pause
+ [[ 0 == \1 ]]
+ echo '  ⏎ 回车继续，Ctrl+C 中断 ...'
+ read -r _
```

### 3.2 正式测试报告（test_reports/）

> 📁 完整测试报告 + 原始日志，**演示前必看**。

| 文件 | 生成时机 | 内容 |
|---|---|---|
| `2026-09-03_integration_test_report.md` | 9/3 mock 联调 | 端到端 100% PASS，含 4 步详细结果 |
| `integration_demo_mock.log` | `integration_demo.py --mode mock` | mock 模式原始输出 |

**报告结构**：
1. 测试目标
2. 结果总览（成功率 / 响应时间 / 单元测试）
3. 详细 Step 结果（每个 step 的 stdout + 验证点）
4. 字段格式验证（ActualState 是否符合 §4.2）

### 3.3 演示截图（demo_screenshots/）

> 📁 7 张历史截图（9/3 演示准备时生成）

| 文件 | 内容 |
|---|---|
| `00_dashboard_before_fix.png` | 修复前的 dashboard |
| `01_dashboard_topology.png` | 拓扑视图 |
| `02_dashboard_integration_panel.png` | 联调面板（已删）|
| `03_dashboard_flow_table_traffic.png` | 流表 + 流量 |
| `04_dashboard_link_detail.png` | 链路详情 |
| `05_demo_full_log.txt` | 端到端日志 |
| `06_dashboard_fixed_top.png` | 修复后的 dashboard |

### 3.4 脚本环境配置（demo_scripts/.env）

> 📁 每次跑脚本时自动写入，下一步骤依赖前一步的产出。

```bash
# 内容示例（来自 .env，41 字节）
INTENT_ID=test-intent-001
BASE=https://x
```

| 字段 | 来源 | 消费者 |
|---|---|---|
| `INTENT_ID` | `01_register_intent.sh` 写入 | `03` ~ `08` 所有脚本读 |
| `BASE` | `00_pre_check.sh` 写入 | `01` ~ `08` 所有脚本读（assurance-agent URL）|

### 3.5 项目代码内嵌日志

| 来源 | 输出位置 | 用途 |
|---|---|---|
| `controller_app.py` | stdout + os-ken 日志 | OpenFlow 握手、LLDP 发现、PortStatus 事件 |
| `state_collector.py` | stdout | 端口统计、流表 dump |
| `live_env.py` | stdout | 周期探测 + ActualState 上报 |
| `state_history.py` | 内存 + `/api/history?key=***` | 历史数据查询 |
| `ovs_logger.py` | 内存 + `GET /api/ovs/log` + `POST /api/ovs/log/correlate` | OVS 详细日志 + 事件关联 |
| `reporter.py` | stdout + `test_reports/` | HTTP POST 状态码 + 响应时间 |

---

## 📌 使用建议

### 演示当天流程

```bash
# 1. 预检环境（必须 5 项全绿）
cd ~/workspace/projects/intensure
bash demo_scripts/00_pre_check.sh

# 2. 提前 10 分钟跑预演（不带回车）
SKIP_WAIT=1 bash demo_scripts/demo_all.sh

# 3. 演示当天按顺序（每步按回车讲一段）
bash demo_scripts/01_register_intent.sh
bash demo_scripts/02_collect_state.sh
bash demo_scripts/03_check_baseline.sh
bash demo_scripts/04_inject_fault.sh link_down
bash demo_scripts/05_check_violated.sh
bash demo_scripts/06_diagnose.sh
bash demo_scripts/07_heal.sh
bash demo_scripts/08_verify_recovered.sh

# 4. 演示后清理
bash demo_scripts/99_reset.sh
```

### 问题排查顺序

1. **环境问题** → `00_pre_check.sh` 看哪项红
2. **脚本错误** → 看 `/tmp/0X_out.log`（主输出）+ `/tmp/0X_trace.log`（命令跟踪）
3. **历史测试结果** → 看 `test_reports/*.md`
4. **之前怎么解决的** → 看 `memory/2026-09-XX.md`
5. **项目本身配置** → 看 `INTEGRATION_GUIDE.md` / `STATE_MODEL.md`

---

## 📐 v3.1 ActualState 契约（2026-09-12 升级）

> ⚠️ A 同学的智能体反馈：down_links / down_ports 格式不对，已升级

### 旧格式（已废弃）❌
```json
{
  "down_links": ["sw1:p2-sw2:p3"],
  "down_ports": ["sw1:p1"]
}
```

### 新格式（v3.1）✅
```json
{
  "down_links": ["s1:eth2-s2:eth3"],
  "down_ports": ["s2:eth3"]
}
```

**规则**：
- `设备名` 默认从 dpid 末位推断（Mininet 约定：`s1`, `s2`）
- `端口名` 优先从 `port.name` 字段提取（`s1-eth2` → `eth2`），fallback 到 `p{port_no}`
- 整体格式：**`设备:端口-设备:端口`**（链路）/ **`设备:端口`**（端口）

---

_📝 最后更新：2026-09-12 17:10 · 庄英琪 整理_