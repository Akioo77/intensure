# intensure + assurance-agent 端到端演示脚本集

> **v2.0 · 2026-09-12**
> 适配：A 同学的 assurance-agent（17 个接口，0.4.0 版本）

## 🎯 演示目标

**展示 intensure（状态采集层）和 assurance-agent（一致性+诊断+自愈）之间的端到端协同**：

```
┌──────────────────┐         ActualState         ┌──────────────────────┐
│  intensure       │ ─────────────────────────→ │  assurance-agent     │
│  (状态采集层)    │ ←───────────────────────── │  (一致性+诊断+自愈)   │
│                  │   RecordView/HealView       │                      │
│  • 流量监控      │                             │  • 期望状态管理       │
│  • 拓扑发现      │                             │  • 一致性检查         │
│  • 故障注入      │                             │  • 根因诊断           │
│  • dashboard     │                             │  • 自愈执行           │
└──────────────────┘                             └──────────────────────┘
```

---

## 📂 文件清单

| 文件 | 行数 | 说明 |
|------|------|------|
| `README.md` | this | 完整使用文档 + 演讲稿速查 |
| `demo_all.sh` | 一键 | 一键演示（支持 `--auto` / `--skip=NN` / `--fault=TYPE`） |
| `00_pre_check.sh` | 5 项预检 | VM / 隧道 / intensure / assurance-agent / Mininet |
| `01_register_intent.sh` | Step 1 | register + activate（生成期望状态） |
| `02_collect_state.sh` | Step 2 | intensure 采集 ActualState |
| `03_check_baseline.sh` | Step 3 | 基线检查（建立健康基线） |
| `04_inject_fault.sh` | Step 4 | 故障注入（4 种可选） |
| `05_check_violated.sh` | Step 5 | 检测违规 → VIOLATED |
| `06_diagnose.sh` | Step 6 | 根因诊断（候选原因 + 修复策略） |
| `07_heal.sh` | Step 7 | 自愈评估（14 项 guard_checks） |
| `08_verify_recovered.sh` | Step 8 | 验证恢复 → RECOVERED |
| `99_reset.sh` | 清理 | 清空本地临时文件 |
| `lib/ui.sh` | UI 库 | 彩色输出 + JSON 解析 + status 徽章 |

---

## 🚀 快速开始

### 一键演示（自动模式）

```bash
cd ~/workspace/projects/intensure/demo_scripts
bash demo_all.sh --auto
```

### 一键演示（交互模式，每步按回车）

```bash
bash demo_all.sh
```

### 指定故障类型

```bash
bash demo_all.sh --auto --fault=high_latency
bash demo_all.sh --auto --fault=packet_loss
bash demo_all.sh --auto --fault=flow_missing
bash demo_all.sh --auto --fault=link_down   # 默认
```

### 跳过预检（联调日已经确认环境）

```bash
bash demo_all.sh --skip=00 --auto
```

### 单步运行（某个环节单独重演）

```bash
bash 01_register_intent.sh     # 重新注册
bash 04_inject_fault.sh link_down  # 单独注入链路故障
bash 05_check_violated.sh      # 单独检查违规
```

---

## 📊 演示流程全景

| Step | 动作 | intensure 角色 | assurance-agent 角色 | 关键产物 |
|------|------|---------------|---------------------|---------|
| 00 | 预检 | - | 健康检查 | `.pre_check.json` |
| 01 | 注册意图 | - | `POST /register` + `POST /activate` | `expected_state` |
| 02 | 采集状态 | **采集 ActualState** | - | `.actual_state.json` |
| 03 | 基线检查 | 推送 ActualState | `POST /check` → ACTIVE | 健康基线 |
| 04 | 故障注入 | 构造故障 ActualState | - | `.fault_state.json` |
| 05 | 检测违规 | 推送故障状态 | `POST /check` → VIOLATED | violations[] |
| 06 | 根因诊断 | - | `POST /diagnose` → DIAGNOSING | root_causes[] |
| 07 | 自愈评估 | - | `POST /heal` → guard_checks | healing_intent + verdict |
| 08 | 验证恢复 | 推送恢复状态 | `POST /verify` → RECOVERED | healing_verdict |

### 状态机轨迹

```
REGISTERED → ACTIVE → VIOLATED → DIAGNOSING → [HEALING] → VERIFYING → RECOVERED
                                            ↓ BLOCKED
                                          VIOLATED (重新触发)
```

---

## 🎤 演讲稿速查（演示当天打印）

### 开场（30 秒）

> "各位老师同学好。我们这套系统包含两个智能体协同工作：
> - **intensure** 负责"看见"网络——采集流量、发现拓扑、注入故障
> - **assurance-agent** 负责"思考和修复"——一致性比对、根因诊断、安全自愈
>
> 我现在演示的是：**当 intensure 检测到链路异常，怎么把信息交给 assurance-agent，由它自动诊断和修复**。"

### Step 1 注册意图

> "首先，我们向 assurance-agent 注册一个意图：客户端 192.168.10.0/24 要能访问服务端 10.0.50.10:443，延迟不超过 50ms，丢包不超过 1%。"

### Step 2-3 采集 + 基线

> "intensure 采集当前网络状态——一切正常。我们把这个 ActualState 推给 assurance-agent 做基线检查，返回 ACTIVE。健康基线建立。"

### Step 4-5 故障注入

> "现在我们注入一个链路故障：s1-eth2 端口 down。intensure 重新采集，把"链路断开"的 ActualState 推给 assurance-agent。"

### Step 6 根因诊断

> "assurance-agent 进入 DIAGNOSING 状态。
> 诊断器返回**两个根因**：物理链路中断（置信度 0.85）+ 流表条目缺失（置信度 0.85）。
> 每个根因还给出**候选原因**（光纤断裂/光模块故障/管理性关闭）和**修复策略**（切换冗余链路/重置光模块/重新启用端口）。"

### Step 7 自愈评估

> "接下来进入 HEALING 状态。
> assurance-agent 评估**14 项安全闸门**（G1-G4 四道）：
> - G1 战略层：策略是否合理
> - G2 置信度层：诊断是否够确定
> - G3 安全层：是否会引入安全问题
> - G4 限流层：是否超过重试/冷却/并发上限
>
> 如果任何一项 BLOCK 级别闸门不通过，自愈被拒绝（设计预期——安全第一）。"

### Step 8 验证恢复

> "最后，intensure 重新采集 ActualState（这次是"已恢复"的状态），推给 assurance-agent 做 verify。
> 如果自愈实际修复了问题，状态机回到 RECOVERED，闭环完成。"

---

## 🤔 老师可能问的 5 个问题

### Q1: 为什么不用大模型做根因诊断？

> A: 我们用**确定性算法 + 知识库**（故障库 + 候选原因 + 修复策略）。
> 优点：
> 1. **确定性**——同样的输入永远给出同样的诊断（可解释、可审计）
> 2. **延迟低**——平均 <100ms，不依赖外部 API
> 3. **不消耗 token**——演示时不依赖网络
> 4. **风险可控**——不会出现 LLM 幻觉导致误诊断
>
> 知识库设计成可扩展的，未来可以接 LLM 做"非典型故障"的二次确认。

### Q2: 自愈会不会做傻事？

> A: 不会。我们有 **5 重保护**：
> 1. **14 项 guard_checks**（G1-G4 四道闸门）
> 2. **重试上限**（默认 3 次，避免反复自愈）
> 3. **冷却时间**（cooldown，避免频繁触发）
> 4. **并发限制**（全局 HEALING 数 ≤ 2，避免雪崩）
> 5. **影响域限制**（scope_devices 必须明确）
>
> 任何一项失败 → BLOCKED，绝不执行。

### Q3: 一致性检查频率是多少？

> A: 默认 5 秒一次。但有**自适应**机制：
> - 健康时降低频率（节能）
> - 检测到违规时提高到 1 秒（快速响应）
> - 自愈期间高频验证

### Q4: intensure 和 assurance-agent 怎么通信？为什么不直接嵌在一起？

> A: 这是**多智能体架构**的核心理念——**职责分离**：
> - intensure 是"基础设施层"（采集/拓扑/可视化），可以独立运行、独立测试
> - assurance-agent 是"业务逻辑层"（一致性/诊断/自愈），可以被多个采集器复用
> - 通过 HTTP + JSON Schema 解耦，未来可以替换采集器（如换成 P4、Telemetry）
>
> 这种解耦也是为了**可研究**——我们可以单独评测"采集精度"和"诊断算法"。

### Q5: 和 ONOS/ODL 这些 SDN 控制器的差异化？

> A: ONOS/ODL 做的是**流表层**（OpenFlow/P4 编程），我们做的是**意图层**：
> - 用户只说"我要访问 10.0.50.10"，不需要写流表
> - 系统自动翻译意图 → 部署策略 → 持续保障
> - 出问题自动诊断 + 自愈，不需要人工介入
>
> 我们和 ONOS 是**互补**的——intensure 未来可以调用 ONOS API 来执行实际的流表操作。

---

## ⚙️ 故障注入的 4 种类型

| 类型 | ActualState 特征 | 预期 violation_types | 演示效果 |
|------|------------------|----------------------|---------|
| `link_down` | `reachable=false`, `down_links=[...]` | `REACHABILITY_FAILURE, POLICY_DRIFT` | **最直观**，链路断开看得见 |
| `high_latency` | `latency_ms=300` | `LATENCY_EXCEEDED` | 延迟飙升，看得见数字 |
| `packet_loss` | `packet_loss=0.5` | `PACKET_LOSS_EXCEEDED` | 丢包模拟 |
| `flow_missing` | `expected_flow_present=false` | `POLICY_DRIFT` | 策略漂移场景 |

**推荐演示**：`link_down`（视觉效果最强）→ `high_latency`（演示自适应）→ `flow_missing`（演示策略层修复）

---

## 🔧 演示当天清单

- [ ] 提前 30 分钟：跑 `bash 00_pre_check.sh` 确认环境
- [ ] 提前 10 分钟：跑 `bash demo_all.sh --auto --fault=link_down` 预演
- [ ] **打印这份 README 的"演讲稿速查"那一节到 A4 纸** ⭐ 最重要
- [ ] 准备 3 个故障类型的 quick switch（看老师兴趣切换）
- [ ] 演示后：跑 `bash 99_reset.sh` 清理本地文件
- [ ] 应急：如果 cloudflared 挂了，演示 intensure 自身的 dashboard（降级方案）

---

## 📞 联系信息

- **assurance-agent 地址**：`https://westminster-accounts-permitted-pursuant.trycloudflare.com`（⚠️ 临时域名，可能变）
- **环境变量覆盖**：`ASSURANCE_BASE=https://新地址 bash demo_all.sh`
- **A 同学**：负责 assurance-agent，故障找他

---

_最后更新：2026-09-12 16:48_
