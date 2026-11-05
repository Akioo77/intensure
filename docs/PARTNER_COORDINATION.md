# 采集模块 × 意图保障模块 · 联调协作说明

> 面向对象：**意图保障模块**的开发者（下面简称"对方"）
> 撰写方：采集模块（intensure）· 版本 v3.22.0 · 2026-11-06
> 本文所有数字均为**实测值**（VM 内 `env_doctor.py` / `pytest` / `psql` 现场核对），
> 不是设计值。标 ⚠️ 的地方是**需要对方配合**才能推进的。

---

## 0. 一分钟版

我们（采集模块）**已经可联调**：24 个 REST 端点、5 个跨模块契约端点、298 例单测全绿、
CI 在 GitHub 上全绿、故障注入 → 一致性判定 → 根因诊断 → 恢复的**完整闭环在本地已跑通**
（8 步脚本，实测注入故障 → 2 条 VIOLATED → 根因 `port_failure` 置信度 0.85 → 恢复 `pass=2`）。

**现在只差一个变量：对方能给一个我们能访问到的地址。**
旧地址 `https://westminster-accounts-permitted-pursuant.trycloudflare.com`
在 2026-11-06 复测为 **HTTP 000**（直连也是 000）→ **那个 cloudflared 隧道本身已失效**，
不是我们这边网络的问题（换节点、换协议都不通）。

拿到新地址后，联调脚本链**一条命令就能跑完**（见 §5）。

---

## 1. 我们这边现在到哪了

### 1.1 能提供什么（采集层）

| 能力 | 说明 | 状态 |
|---|---|---|
| 交换机发现 | OpenFlow 1.3 握手，拿到 dpid / 端口 / 速率 | ✅ |
| 链路发现 | **LLDP** 自动邻居学习（交换机间链路） | ✅ |
| 主机发现 | packet-in 学 MAC + ARP/IP 解析 | ✅（有前提，见 §3.2） |
| 流表采集 | OpenFlow flow stats | ✅ |
| 端口状态 | 端口 up/down、流量计数、丢包 | ✅ |
| **端口事件落库** | `intensure.port_events` 带精确时间戳（link_down / link_up） | ✅ |
| 连通性探测 | 4 种驱动：mininet / icmp / **snmp** / cli | ✅ |
| 故障注入 | **9 种故障 + clear**：link_down / link_up / delay / loss / rate_limit / burst / reorder / vlan_mismatch / flow_race（业务别名 high_latency→delay、packet_loss→loss、flow_missing→flow_race） | ✅ |
| OVS 日志采集 | `/var/log/openvswitch/ovs-vswitchd.log` 实时 tail + 解析 | ✅ |
| 契约端点 | `/api/intensure/v1/*` 5 个，带 `version` 乐观锁 | ✅ |
| DB 写入 | 6 schema / 26 逻辑表 / 47 物理表（21 个分区子表） | ✅ |

### 1.2 数字（实测）

| 项 | 值 | 怎么核的 |
|---|---|---|
| 代码版本 | v3.22.0 | `git describe --tags` |
| 自营 REST 端点 | **24 个** | `grep -c "^@app.route" state_server.py` |
| 其中跨模块契约端点 | **5 个** | `/api/intensure/v1/*` |
| 单元测试 | **298 例全绿** | VM 内 `pytest` / GitHub CI |
| CI | GitHub Actions **success** | 每次 push 必跑 |
| 契约路径版本 | 我方 `1.0` | `GET /api/capabilities` |
| 期望对方契约版本 | `0.4.0` | `assurance_bridge.DEFAULT_EXPECTED_VERSION` |
| 数据库 | 6 schema / 26 逻辑表 / 47 物理表（含 21 个分区子表） | `pg_class` 实测 |
| 心跳与联调状态 | `shared.module_health` / `shared.schema_version` | 对方可查 |

### 1.3 我们的对外契约（对方按这个调我们）

| 契约路径（对方调这个） | 旧路径（保留，仍在用） | 说明 |
|---|---|---|
| `GET /api/intensure/v1/state/current` | `GET /api/state` | 当前状态快照 |
| `GET /api/intensure/v1/state/history` | `GET /api/history` | 历史 |
| `GET /api/intensure/v1/port-events` | `GET /api/port_stats` | 端口事件 |
| `GET /api/intensure/v1/link-events` | 无对应 | 链路事件 |
| `POST /api/intensure/v1/probe-results` | `POST /api/probe` | 探测结果上报 |

两套并存、指向同一份底层状态。契约路径的返回结构**多一层转换**：
`switches` 是 list（带 `version` 乐观锁）、有 `down_ports` / `down_links` / `conflicting_flows`。

**版本协商**：对方的契约版本与我们不匹配时，**我们自动降级、不上报**
（避免用错格式的字段污染对端）。状态可在 `GET /api/capabilities` 里查。

---

## 2. ⚠️ 需要对方配合的三件事

### 2.1 给一个可访问的地址（**最高优先级，现在就卡在这**）

**现状**：旧隧道 `westminster-accounts-permitted-pursuant.trycloudflare.com`
在 2026-11-06 复测为 HTTP 000（直连 / 走代理都试过）→ 隧道已失效。

**请对方提供其一**：

| 方式 | 做法 | 备注 |
|---|---|---|
| **A. 新隧道** | 起一个 cloudflared，把新域名给我们 | 临时域名会变，建议同时给一个固定入口 |
| **B. 局域网直连** | 给一个我们能路由到的 IP:端口 | 联调时最稳，不依赖公网 |
| **C. 反向隧道** | 对方起隧道连我们 | 我们这边要额外开入口，**请先说一声** |

拿到后我们这边只要设一个环境变量：

```bash
export ASSURANCE_BASE=https://新地址
# 旧域名留作兜底（cloudflared 重启换域名时不至于断链）
export ASSURANCE_BASE_FALLBACK=https://westminster-accounts-permitted-pursuant.trycloudflare.com
```

> 💡 我们也支持 **SSH 方式接入**（`target.type=ssh`）：如果对方的保障模块跑在
> 对方那台 Mininet 机器上、需要我们过去执行动作，这条路已经打通并实测过
> （见 §6.2）。

### 2.2 共享库权限（**我们已经建好了，只差把密码告诉对方**）

- 账号：`assurance_user`
- 库：`intensure_dev`
- 授权范围：`assurance` schema 的**全部 14 张逻辑表**（含分区子表共 **107 条授权**，
  实测每张表都是 SELECT / INSERT / UPDATE / DELETE 四件套）
- **现状：授权已经在库里就位，对方拿到密码即可直接连**

请对方确认收到了密码；连接串形如：

```
postgresql://assurance_user:<密码>@<主机>:5432/intensure_dev
```

> 我们这边写 `intensure.*`（自己的表）+ 读 `shared.*`；
> **不写** `assurance.diagnoses`（诊断归对方，职责分离 —— 见 §3.1）。

### 2.3 按契约调我们的端点 + 回一个版本号

请对方在调我们之前先确认两件事：

1. 对方的契约版本是不是 `0.4.0`（我们的期望值）。不是的话告诉我们实际版本，
   我们改 `ASSURANCE_BASE` 之外的那个期望值即可（不硬编码）。
2. 对方按 §1.3 的 5 个契约路径调我们，返回结构以
   `docs/cross-module-api-contract.md` 为准。

---

## 3. 我们的局限与边界（请对方心里有数）

### 3.1 职责边界：我们**不做**这些

| 不做 | 归谁 |
|---|---|
| 意图翻译（用户意图 → 策略） | 意图翻译模块 |
| 意图实现（策略 → 流规则 / 配置下发） | 意图实现模块 |
| 公共模块的 `/api/shared/v1/*` | 公共模块 |
| **一致性判定**（expected vs actual） | 对方（保障模块） |
| **根因诊断** | 对方 |
| **自愈决策 / 冲突消解** | 对方 |
| `translation.*` / `implementation.*` 表的写入 | 翻译 / 实现模块 |

**接口方向**：我们**只 POST 不读 GET** 对方的状态 ——
当前状态由对方自己管（`/record/{intent_id}`），我们不越界去读。

### 3.2 ⚠️ 三个真实的技术局限（会被追问的，先说清）

**① 关掉流表下发就没有 MAC 级观测**

`env.json` 的 `flow_rule_write: false`（接真实园区**必须**关）会让交换机不再把
未匹配包送回控制器 → **没有 packet-in → 学不到 MAC → 拿不到主机级观测**。
关掉后仍可采到：交换机、端口状态、链路（LLDP）、流表。
→ 真实园区只读接入时，我们的交付口径是**链路/端口级感知**，不是主机级。

**② 目前没有真实网络环境，只有实验室 Mininet**

v3.21.0 起配置层、只读模式、`flow_rule_write` 开关、能力自检（`env_doctor.py`）
都已就位，但**从没在真实园区网络上跑过**。接入适配层（远端执行）已实测可用，
但真实设备的**端口名映射**（`port_label_map`）和 **dpid→设备名映射**
（`dpid_to_switch`）还没在真机上验证过。

> ⚠️ **不填这两个映射的后果**：契约里的 `down_ports` 会报真实长名
> （`GigabitEthernet0/1`）而不是契约要的短标签（`eth1`）→
> **对方自愈判定会受影响**。这是我们目前最需要对方一起确认的一项。

**③ SNMP / CLI 探测驱动在真实设备上未验证**

驱动本身已实现并实测（SNMP 是纯标准库实现，BER 编解码，无第三方依赖），
但只在实验室 Mininet 上验证过。真设备的 SNMP 团体名、CLI 输出格式需要对方提供信息。

### 3.3 运维侧的已知边界

- 状态每 5s 全量写库；`state_history` 保留 **7 天**，`port_events` / `link_events` /
  `consistency_checks` / `probe_results` / `audit_log` 保留 **30 天**
- 历史分区由 systemd timer 清理：`OnCalendar=04:07` + `RandomizedDelaySec=300`（随机延迟 0~5 分钟，实测 2026-11-06 04:09 触发）；2026-11-06 已装到 VM 并验证 `enabled` + `active`
- 主机表按 TTL（默认 15 分钟）淘汰，不会无限增长

---

## 4. 待开发的部分

### 4.1 我们这边（采集模块）

| 事项 | 优先级 | 依赖 |
|---|---|---|
| **真实 OpenFlow 网络接入验证** | 🔴 最高 | **需要真实环境**（目前没有） |
| 真实设备的 `port_label_map` / `dpid_to_switch` 映射验证 | 🔴 高 | 真实环境 |
| `AgentExec`（生产网禁 SSH 时的远端 agent 通道） | 🟡 中 | 真实环境形态 |
| SNMP / CLI 驱动在真机上的输出适配 | 🟡 中 | 对方的 SNMP 团体名 / CLI 样例 |
| Dashboard 单文件拆分（1600+ 行） | 🟢 低 | 演示之后做，拆错伤演示 |
| Docker 化 | 🟢 低 | 无 |

### 4.2 需要对方推进 / 提供的

| 事项 | 说明 |
|---|---|
| ⚠️ **新的可访问地址** | §2.1，现在唯一的硬阻塞 |
| ⚠️ **`assurance_user` 密码回传 + 连通性确认** | 授权已就位，只差密码 |
| 契约版本号确认（期望 0.4.0） | 不一致我们改期望值，但请先告知 |
| 对方的 intent 语义 / `expected_state` 结构 | 我们构造 ActualState 时要用到 |
| 对方的自愈 guard 规则（哪几项 evidence 是必需的） | 我们决定采集侧优先采什么 |

---

## 5. 联调怎么跑（拿到地址后）

### 5.1 一键预检（先跑这个，5 项）

```bash
cd demo_scripts
bash 00_pre_check.sh
```

检查：Colima VM / Dashboard 隧道 / 采集层服务 / **对方保障模块** / Mininet 环境。
预期输出 `4/5` 或 `5/5` 通过（**唯一可能不通的就是对方地址**）。

### 5.2 全链路（9 步，每步都有产物）

```bash
bash 01_register_intent.sh    # 注册 + activate → ACTIVE
bash 02_collect_state.sh      # 采集 ActualState（.actual_state.json）
bash 03_check_baseline.sh     # 推给保障模块 → 期望 ACTIVE, violations=[]
bash 04_inject_fault.sh       # 注入故障（link_down/high_latency/packet_loss/flow_missing）
bash 05_check_violated.sh     # 推故障态 → 期望 VIOLATED, violations≥1
bash 06_diagnose.sh           # 根因诊断 → root_causes + confidence
bash 07_heal.sh               # 自愈 → verdict + guard_checks
bash 08_verify_recovered.sh   # 验证恢复 → RECOVERED, healing_verdict=SUCCESS
bash 99_reset.sh              # 清理
```

一键版：`bash demo_all.sh --auto`（不暂停）；`bash demo_all.sh --auto --fault=link_down`（指定故障类型）。

> 这些脚本 2026-11-06 才修好（`demo_scripts/lib/ui.sh` 从来没进过版本库，
> 导致整条链一跑就报 `command not found`）——**现在可以正常跑了**。

### 5.3 不依赖对方的本地闭环（对方一时给不了地址时也能演示我们这侧）

```bash
bash demo_scripts/demo_db_pipeline.sh     # 8 步闭环，纯本地
bash demo_scripts/ovs_attribution_demo.sh  # 证据链演示（故障 → 设备侧日志 → 事件 → 对齐）
```

### 5.4 联调成功判据（建议双方按这个验收）

- [ ] `00_pre_check.sh` 5 项全过
- [ ] `01` → intent 状态 `ACTIVE`
- [ ] `03` → `status=ACTIVE`，`violations=[]`
- [ ] `04`+`05` → `status=VIOLATED`，`violations≥1`
- [ ] `06` → 返回 `root_causes` + `confidence` + 候选原因 + 修复策略
- [ ] `07` → 返回 `verdict` 与 `guard_checks`（被闸门拦截也是**预期结果**，不是失败）
- [ ] `08` → `status=RECOVERED`，`healing_verdict=SUCCESS`
- [ ] 我方侧：`intensure.port_events` 里有对应时间戳的 `link_down` / `link_up` 记录
- [ ] 我方侧：`GET /api/intensure/v1/state/current` 的 `down_ports` 在故障窗口内非空

---

## 6. 环境与协作说明

### 6.1 环境清单（对方复现时需要）

| 项 | 值 |
|---|---|
| 宿主 | macOS (Apple Silicon) + Colima |
| VM | Ubuntu 24.04 aarch64 / Python 3.12.3 |
| 仿真 | Mininet + Open vSwitch + os-ken 2.8.1（OpenFlow 1.3） |
| DB | PostgreSQL 16（`intensure_dev`） |
| 仓库 | `github.com/Akioo77/intensure`（v3.22.0） |
| 采集层 API | `http://127.0.0.1:8080`（宿主侧经隧道 `18080`） |

### 6.2 接入方式（我们支持三种，联调时可任选）

| 场景 | 配置 | 说明 |
|---|---|---|
| 本机自建 Mininet（默认） | `target.type=local` | 实验室现状 |
| **Mininet 在对方机器上** | `target.type=ssh` | 已实测：远端命令、端口枚举、注故障、跨机 ping 全部打通 |
| 真实园区设备 | `mode=read-only` + `target.type=ssh`（或 agent） | 配置层已就位，**待真机验证** |

> 联调时如果对方的 Mininet 就在对方机器上，**建议用这一种**：
> 对方起 Mininet 时把控制器指向我们（`mn --controller remote,ip=<我们>,port=6653`），
> 我们这边用 `target.type=ssh` 过去执行动作。联调前跑
> `python3 demo_scripts/cross_machine_check.py --host <对方> --user <用户>`
> 就能验证接入是否正常。

### 6.3 协作约定（双方都要遵守）

1. **同一个分支，同一时间只允许一个 agent 提交** —— 2026-11-05 发生过一次
   rebase 把对方的提交静默丢掉的情况（提交本身没冲突，是被 rebase 丢的）。
2. **接口以 `docs/cross-module-api-contract.md` 为准**，改契约要同步改文档并知会对方。
3. **版本号一致**：`/api/health` 的 `version` 与 CHANGELOG 标签要对得上
   （我们的口径已经修好，之前一直落后两版）。

---

## 7. 常见问题

**Q：你们的状态能采到我这边机器上的 Mininet 吗？**
能。OpenFlow 本身是网络协议，只要能 TCP 连到交换机的 6653 端口就能采，
不需要我们在同一台机器上。要执行动作（注故障/探测）时走 SSH。

**Q：为什么你们不读我的 `/record/{intent_id}`？**
职责边界。对方自己的状态由对方自己管，我们只负责提供 ActualState 与故障证据，
避免两边互相覆盖状态（这是 v2.0 定的口径，写在 `REQUIREMENTS.md` 里）。

**Q：`down_ports` 里的名字对不上怎么办？**
见 §3.2② —— 真实设备上需要在 `env.json` 填 `port_label_map`。
这是我们目前最需要一起确认的一项。

**Q：你们能自愈吗？**
不能，也不该由我们做。我们做**检测 + 证据采集 + 根因线索**，
自愈决策与执行是保障模块的职责（`docs/speaker_notes.md` Q4 专门讲了这个口径）。

**Q：联调时接口返回 503 / 000 怎么排查？**
先看 `demo_scripts/00_pre_check.sh` 的第 4 项；再查
`GET http://<我们>:8080/api/capabilities` 里的 `contract` 段（版本协商状态）。

---

## 8. 联系方式与下一步

- 采集模块代码：`github.com/Akioo77/intensure`（v3.22.0，CI 全绿）
- 契约文档：`docs/cross-module-api-contract.md`（跨模块契约，§4.1/§5.1 端点定义、§5.7 版本协商）
- 接入手册：`INTEGRATION_GUIDE.md` §6.7（真实园区迁移）、§6.8（接入适配层）
- 演示讲稿 + Q&A：`docs/speaker_notes.md`

**我们这边的下一步**：拿到地址 → 跑完整 9 步联调 → 按 §5.4 验收；
同时等真实网络环境到位后做接入验证（§4.1）。

**需要对方的第一件事**：§2.1 的新地址。
