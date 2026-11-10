# 采集模块 × 意图保障模块 · 联调协作说明

> 面向对象：**意图保障模块**的开发者（下面简称"对方"）
> 撰写方：采集模块（intensure）· 版本 v3.22.0 · 2026-11-06
> 本文所有数字均为**实测值**（VM 内 `env_doctor.py` / `pytest` / `psql` 现场核对），
> 不是设计值。标 ⚠️ 的地方是**需要对方配合**才能推进的。

---

## 0. 一分钟版

我们（采集模块）**已经可联调**：24 个 REST API 端点、5 个跨模块契约端点、346 例单测全绿、
CI 在 GitHub 上全绿、故障注入 → 一致性判定 → 根因诊断 → 恢复的**完整闭环在本地已跑通**
（8 步脚本，实测注入故障 → 2 条 VIOLATED → 根因 `port_failure` 置信度 0.85 → 恢复 `pass=2`）。

**现在只差一个变量：对方能给一个我们**此刻**能访问到的地址。**
（我们之前用的 `*.trycloudflare.com` 是**联调测试用的临时隧道**，不是最终部署地址；
它 2026-11-06 复测为 **HTTP 000**（直连也是 000）→ **那个隧道已失效**，
不是我们这边网络的问题，换节点、换协议都不通。）

地址是运行时配置，我们**不改代码**就能从测试地址切到最终地址 —— 详见 §2.1。

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
| **操作审计** | `shared.audit_log`（append-only：各模块角色只有 SELECT+INSERT）—— `inject_fault` / `clear_faults` | ✅ |
| 连通性探测 | 4 种驱动：mininet / icmp / **snmp** / cli | ✅ |
| 故障注入 | **9 种故障 + clear**：link_down / link_up / delay / loss / rate_limit / burst / reorder / vlan_mismatch / flow_race（业务别名 high_latency→delay、packet_loss→loss、flow_missing→flow_race） | ✅ |
| OVS 日志采集 | `/var/log/openvswitch/ovs-vswitchd.log` 实时 tail + 解析 | ✅ |
| 契约端点 | `/api/intensure/v1/*` 5 个，带 `version` 乐观锁 | ✅ |
| DB 写入 | 6 schema / 26 逻辑表 / 47 物理表（21 个分区子表） | ✅ |

### 1.2 数字（实测）

| 项 | 值 | 怎么核的 |
|---|---|---|
| 代码版本 | v3.24.0 | `git describe --tags`（截至 `05e39ae`）|
| 自营 REST API 端点 | **24 个** | `grep -c "^@app.route" state_server.py` = 25，扣掉 1 条 `/static/<path:filename>` 静态资源路由 |
| 其中跨模块契约端点 | **5 个** | `/api/intensure/v1/*` |
| 单元测试 | **346 例全绿** | VM 内 `pytest` / GitHub CI（2026-11-10 实测）|
| CI | GitHub Actions **success** | 每次 push 必跑 |
| 契约路径版本 | 我方 `1.0` | `GET /api/capabilities` |
| 期望对方契约版本 | `0.4.1` | `assurance_bridge.DEFAULT_EXPECTED_VERSION`（2026-11-10 对齐对方实际版本）|
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

### 1.4 证据链三环（回答"这条链路到底怎么断的"）

| 环 | 出处 | 回答什么 |
|---|---|---|
| ① 设备侧事实 | 交换机给的 OpenFlow 端口状态（`4=UP` → `1=DOWN`）+ OVS 日志 | 链路**真的**断了，不是我方数据造假 |
| ② 我方事件 | `intensure.port_events`（带精确时间戳） | **什么时候**断的、哪个接口 |
| ③ 操作记录 | `shared.audit_log`（`inject_fault` / `clear_faults`） | **谁**在什么时候动了哪个接口 |

> ⚠️ ②③ 在 **2026-11-06 之前一直是空的**（模块级包装函数漏传参数 / 审计根本没接线），
> 是这一轮挖出来并修好的 —— 对方若拿到旧版本的数据，会看到空表。

---

## 2. ⚠️ 需要对方配合的三件事

### 2.1 给两个地址：**联调测试用**的 + **最终要用**的

**先说清楚一个容易混淆的点**：我们之前用过的
`https://westminster-accounts-permitted-pursuant.trycloudflare.com`
是**联调测试用的临时隧道地址**（cloudflared 起的临时域名，重启就换），
**不是最终部署要用的地址**。它只是为了在两边还没部署好的阶段先把链路打通。

所以需要对方给**两个地址**：

| 用途 | 要求 | 备注 |
|---|---|---|
| **① 联调测试地址**（现在就要） | 一个我们**此刻**能访问到的地址 | 临时隧道就行；2026-11-06 复测旧的那个已是 HTTP 000（直连也 000）→ **隧道本身失效了**，请重新起一个并把新域名给我们 |
| **② 最终环境地址**（后续） | 对方服务**正式部署**后我们长期使用的地址 | 可以是固定域名 / 内网 IP / 服务发现名，取决于对方的部署形态 |

> 我们这边**不需要为地址切换改任何代码** —— 地址是运行时配置：
>
> ```bash
> export ASSURANCE_BASE=<联调测试地址>        # 当前在用的
> export ASSURANCE_BASE_FALLBACK=<备用地址>   # 隧道重启换域名时兜底
> ```
>
> 正式环境地址到位后，改一次环境变量即可（`env.json` / 启动脚本里改也行）。
> 也就是说：**测试地址可以随便换，正式地址我们也能无痛切过去**。

**如果联调阶段希望更稳**，以下三种都可以作为「① 联调测试地址」：

| 方式 | 做法 | 备注 |
|---|---|---|
| **A. 临时隧道** | 起一个 cloudflared，把新域名给我们 | 最快；域名会变，兜底地址要跟上 |
| **B. 局域网直连** | 给一个我们能路由到的 IP:端口 | 联调时最稳，不依赖公网 |
| **C. 反向隧道** | 对方起隧道连我们 | 我们这边要额外开入口，**请先说一声** |

> 「② 最终环境地址」请对方明确一下形态（固定域名 / 内网 IP / 服务发现），
> 我们据此确认接入方式（`target.type=ssh` 还是未来的 agent 通道）。

> 💡 我们也支持 **SSH 方式接入**（`target.type=ssh`）：如果对方的保障模块跑在
> 对方那台 Mininet 机器上、需要我们过去执行动作，这条路已经打通并实测过
> （见 §6.2）。

### 2.2 共享库权限（**我们已经建好了，只差把密码告诉对方**）

> ⚠️ **先说清「库在哪」**（2026-11-10 实测）：
> 数据库**完全在我方本地** —— Colima VM 里的 PostgreSQL 16.15，
> **只监听 `127.0.0.1:5432`，外部机器连不上**，也没有任何云端实例。
> 所以对方拿到密码**也不能直接连** —— 需要先开隧道（见下）。
> 完整步骤（隧道 + 换密码 + 验收 + 越权自检 + 排错表）见
> **`docs/DB_ACCESS_HANDOFF.md`**，那份文档里每条命令都实测过。

- 账号：`assurance_user`
- 库：`intensure_dev`
- 授权范围：`assurance` schema 的**全部 20 张表**（18 张普通表 = 12 逻辑表 + 6 个分区子表，
  加 2 张分区父表），共 **80 条授权** —— 实测每张表都是 SELECT / INSERT / UPDATE / DELETE 四件套
  （2026-11-06 `information_schema.role_table_grants` 实测：20 张表 × 4 件套 = 80；
  2026-11-10 复测：该账号在**全库**共 107 条授权）
- **现状：授权已经在库里就位**，对方拿到密码 + 隧道即可直接连

**对方接入方式（推荐 SSH 隧道，不用改我方 PG 配置、不用开防火墙口子）**：

```bash
# 对方在自己机器上执行，把我方的 5432 映射到自己本机的 15432
ssh -N -L 15432:127.0.0.1:5432 <我方用户>@<我方可达地址>
```

连接串形如（端口是 **15432**，不是 5432 —— 免得和对方自己的 PG 撞）：

```
postgresql://assurance_user:<密码>@127.0.0.1:15432/intensure_dev
```

> 💡 对方的密码我会**单独给**（现在库里是明文硬编码的开发默认值，
> 联调前会换成随机值）。
> 验证接入的四条命令见 `docs/DB_ACCESS_HANDOFF.md` §3.4。

> 我们这边写 `intensure.*`（自己的表）+ 读 `shared.*`；
> **不写** `assurance.diagnoses`（诊断归对方，职责分离 —— 见 §3.1）。

### 2.3 按契约调我们的端点 + 回一个版本号

请对方在调我们之前先确认两件事：

1. ~~对方的契约版本是不是 `0.4.0`~~ → **已确认：对方是 `0.4.1`**（2026-11-10），
   我方期望值已对齐。对方再升版本时用 `ASSURANCE_EXPECTED_VERSION` 临时对齐即可（不硬编码）。
2. 对方按 §1.3 的 5 个契约路径调我们，返回结构以
   `docs/cross-module-api-contract.md` 为准。

### 2.4 ✅ 接口形态已确认：走 **② 套** `/api/v1/assurance/...`（2026-11-10 对方答复）

> **结论**：对方明确答复「保障侧继续使用 ② 套 `/api/v1/assurance/...` 接口，
> 路径和请求格式不变」。**接口形态这个悬念到此关闭**，我们代码零改动。

2026-11-07 对账时发现：仓库里关于"对方的接口"有**三种写法**，前缀和动词都不一样。
我们脚本与 `assurance_bridge.py` 用的是**第二套**（2026-09-12 真实联调跑通过的那套），
但契约文档 §5.4 写的是**第三套**，而我们自带的 mock 原本实现的是**第一套**。

| 动作 | ② 我们脚本 / bridge 实调（**已确认采用**） | ③ 契约文档 §5.4 的写法 | ① mock 原实现 |
|---|---|---|---|
| 注册意图 | `POST /api/v1/assurance/register`<br>body：`{"intent":{...},"deployment_id":...,"devices":[...]}` | `POST /api/assurance/v1/intents/register` | `POST /api/v1/assurance/register-request`<br>body：`{"request":{"child_intents":[...]}}`（注册一棵**意图树**） |
| 激活 | `POST /api/v1/assurance/activate` | `POST /api/assurance/v1/intents/{id}/activate` | 同② |
| 一致性检查 | `POST /api/v1/assurance/check` | `GET /api/assurance/v1/checks?intent_id=` | 同② |
| 根因诊断 | `POST /api/v1/assurance/diagnose?intent_id=` | `POST /api/assurance/v1/diagnoses` | 原先没有（已补） |
| 自愈决策 | `POST /api/v1/assurance/heal?intent_id=` | `POST /api/assurance/v1/healings` | 原先没有（已补） |
| 验证恢复 | `POST /api/v1/assurance/verify` | `POST /api/assurance/v1/healings/{id}/execute` | 同② |

> 📌 表格里的 ③ 是**契约文档里的旧写法**，实际联调按 ② 走。
> 若对方后续改用 ③，那是**改我们**（`assurance_bridge.py` + 8 个演示脚本的路径），不是改对方。
> mock 三套现在都能响应（2026-11-07 已补齐），所以**我们这边随时能自测**。

### 2.5 数据库也是联调通道 —— 而且联调前有一件事必须做

联调不只走 HTTP，还有**共享数据库**这条通道。三方表归属：

| 通道 | 谁写 | 谁读 | 表 |
|---|---|---|---|
| ① 我们的观测 | 采集模块（`intensure_user`） | 对方 | `intensure.*`（network_state / state_history / port_events / probe_results）|
| ② 对方的判定 | 对方（`assurance_user`） | 我们 | `assurance.*`（consistency_checks / diagnoses / healing_intents / intent_states）|
| ③ 公共 | 双方 | 双方 | `shared.*`（audit_log / module_health / schema_version）|

**权限现状（实测，全部就位）**：

- 对方 `assurance_user`：`assurance.*` 14 张逻辑表（12 普通表 + 2 张分区父表）全套 DML —— **该 schema 内 80 条授权**（`assurance_user` 全库授权共 107 条）；
  `shared.audit_log` 只有 INSERT+SELECT（**append-only 在权限层成立**）；
  `shared.module_health` INSERT/SELECT/UPDATE；`shared.schema_version` 只读
- 我们 `intensure_user`：能读对方写的 `assurance.*`（联调时用来核对对方落库结果）

**🔴 联调前必须做的两件事**：

**① 采集层要用 `--no-assurance` 起** —— 我们自带的 `assurance_mvp.py`（本地 mock 保障模块）
写的正是**对方要写的表**（`consistency_checks` / `diagnoses` / `intent_states`）。
不关掉的话两个进程抢写同一张表，对方会看到我们的假记录、假状态机轨迹。

```bash
bash start_all.sh --no-assurance
```

**② 清掉我们 mock 留下的记录** —— 那张表里现在有 **21241 行**是 mock 写的
（`intent_id` 形如 `live-AC-demo-001/002`），`assurance.diagnoses` 里 332 行也是。
表里**没有 source 字段能区分谁写的**，只能按 intent_id 区分：

```sql
-- 只删我们自己 mock 写的那几行（对方的数据不动）
DELETE FROM assurance.consistency_checks WHERE intent_id LIKE 'live-AC-demo-%';
DELETE FROM assurance.intent_states    WHERE intent_id LIKE 'live-AC-demo-%';
DELETE FROM assurance.diagnoses       WHERE intent_id LIKE 'live-AC-demo-%';
```

**联调时怎么验 DB 通道**：对方 check 一次 → `assurance.consistency_checks` 应该多一行
（`intent_id` = 联调用的那个 intent，不是 `live-AC-demo-*`）；
我们这边 `GET /api/intensure/v1/state/current` 的 `down_ports` 在故障窗口内非空。

> 📌 **2026-11-10 对方答复：本轮不接入共享数据库**。
> 保持 **HTTP 对接 + 各自持久化**，指定表落库的事后续再推进。
> → 上面这套 DB 通道的验证方式**本轮用不上**，联调时**只看 HTTP**；
> 但表结构与权限**已经就位**（`assurance_user` 在 `assurance` schema 内 80 条、全库共 107 条，均实测通过），
> 后续接入时不用再改代码。
>
> 📖 **后续要接库时怎么做的完整步骤**（隧道 + 换密码 + 验收 + 越权自检 + 排错表）
> 见 **`docs/DB_ACCESS_HANDOFF.md`** —— 那份文档里每条命令都实测过。
> 一句话：数据库在我方本地（Colima VM 的 PG16，只监听 `127.0.0.1:5432`），
> 对方在自己机器上开 SSH 隧道（`-L 15432:127.0.0.1:5432`）即可接入。

---

## 2.6 本轮口径澄清（2026-11-10 对方答复，逐条已对齐）

对方答复里的四条技术澄清，我们**逐条核对过代码并已对齐**。记在这里，
免得以后再走偏 —— 这几条都是「我方原本理解偏了」的地方。

### 2.6.1 契约版本：对方服务版本 **0.4.1**

- 对方健康接口**已补充 `version` 字段**，当前值 `0.4.1`
- 我方期望版本已从 `0.4.0` 改成 `0.4.1`（`assurance_bridge.DEFAULT_EXPECTED_VERSION`）
- ⚠️ **版本字符串不同不会导致停止上报**：我方 `_version_key()` 只取 (major, minor)，
  `0.4.1` 与 `0.4.0` 同为 `(0, 4)` → 判定 `compatible` → **继续推送**。
  只有 major/minor 不同才会 `mismatch` 阻断。
- 对方再升版本时用环境变量 `ASSURANCE_EXPECTED_VERSION` 对齐，**不用改代码**

### 2.6.2 自愈结论：**APPROVED / BLOCKED**（不是 ALLOW）

| | 字段 | 取值 |
|---|---|---|
| 自愈结论 | `verdict` | `APPROVED`（批准执行）/ `BLOCKED`（被闸门拦截）|
| 恢复成功 | **`status`** | `RECOVERED` |

- ⚠️ **`ALLOW` 是另一个字段**：意图里的 `action: "ALLOW"` 是**动作类型**，
  和自愈结论 `verdict` 完全是两回事，别混。
- 我方 mock 与演示脚本原本返回/判定 `ALLOW`，**已改成对方口径**（2026-11-10）。
- 我们的数据库 schema 本来就是 `verdict IN ('APPROVED','BLOCKED','PENDING')`
  —— 是 mock 和脚本跑偏了，现在一致了。

### 2.6.3 恢复判定：看 **`status=RECOVERED`**，不是 `healing_verdict=SUCCESS`

- 我方 `08_verify_recovered.sh` 原本两个都判、且把 `healing_verdict=SUCCESS` 当主要依据
- 现在**以 `status` 为唯一判据**；`healing_verdict` 只作展示（对方返回才显示）
- ⚠️ 对方接口**不返回** `healing_verdict` 时，脚本照样能正确判出恢复成功

### 2.6.4 连续确认次数：由**对方**负责，我方不实现

| 场景 | 确认条件 |
|---|---|
| 可达性 / 延迟 / 丢包 | **连续 3 次**异常才确认 |
| 策略漂移 / 未授权可达 | **单次**即确认 |
| 恢复 | **连续 2 次**健康 |
| 性能场景 | **需先配置相应阈值** |

这属于**一致性判定** —— 按 §3.1 的职责边界，那是**对方模块的职责**，
我方只提供事实（ActualState），不做判定。所以我方代码里**没有也不应该有**这套计数逻辑。

> 📌 我方提供的 ActualState 字段里带 `latency_ms` / `packet_loss` /
> `expected_flow_present` 等，够对方做这些判定。性能场景要的阈值，
> 需要对方先配置好 —— **我们这边不用改**，但联调前请确认对方已配。

### 2.6.5 端口名格式：**`s1:eth2`** —— 我方本来就对

我方 `down_ports` 产出 `["s1:eth2"]`、`down_links` 产出 `["s1:eth2-s2:eth3"]`，
与要求一致，**无需改动**。

### 2.6.6 联调时关掉我方保障 mock

`bash start_all.sh --no-assurance`（对方也明确提了这一条，见 §2.5 ①）。

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
- 历史分区由 systemd timer 清理：`OnCalendar=04:07` + `RandomizedDelaySec=300`（随机延迟 0~5 分钟）；**已实测自动触发**（2026-11-06 04:09、2026-11-07 04:11 两次，`Result=success`）
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

### 4.2 需要对方推进 / 提供的

| 事项 | 说明 |
|---|---|
| ⚠️ **① 联调测试地址**（新起一个临时隧道即可） | §2.1，现在唯一的硬阻塞 |
| ⚠️ **② 最终环境地址**（正式部署后长期使用） | §2.1，形态（固定域名 / 内网 IP / 服务发现）请先告知，我们据此定接入方式 |
| 🔴 **接口形态确认（哪一套？）** | §2.4 —— 仓库里有三套写法（路径前缀 + 动词都不一样）。**这是最容易在第一步就 404 的地方**，建议联调前就问清；确认是我们用的那套就不用改代码 |
| ⚠️ **`assurance_user` 密码回传 + 连通性确认** | 授权已就位，只差密码 |
| 契约版本号确认 | ✅ **已确认 0.4.1**（2026-11-10），我方期望值已对齐 |
| 对方的 intent 语义 / `expected_state` 结构 | 我们构造 ActualState 时要用到 |
| 对方的自愈 guard 规则（哪几项 evidence 是必需的） | 我们决定采集侧优先采什么 |

---

## 5. 联调怎么跑（拿到**联调测试地址**后）

### 5.1 一键预检（先跑这个，5 项）

```bash
cd demo_scripts
bash 00_pre_check.sh
```

检查：Colima VM / Dashboard 隧道 / 采集层服务 / **对方保障模块** / Mininet 环境。
预期输出 `4/5` 或 `5/5` 通过（**唯一可能不通的就是对方的联调测试地址**）。

> ⚠️ **联调时采集层要用 `bash start_all.sh --no-assurance` 起** ——
> 原因见 §2.5①：我们自带的本地 mock 保障模块会跟对方抢写 `assurance.*` 那几张表。

### 5.2 全链路（9 步，每步都有产物）

> 这一轮先跑在 **① 联调测试地址**上；全链路跑通后，把 `ASSURANCE_BASE` 换成
> **② 最终环境地址**，再跑一遍 5.1 预检确认连通即可 —— 脚本与配置都不用改。

```bash
bash 01_register_intent.sh    # 注册 + activate → ACTIVE
bash 02_collect_state.sh      # 采集 ActualState（.actual_state.json）
bash 03_check_baseline.sh     # 推给保障模块 → 期望 ACTIVE, violations=[]
bash 04_inject_fault.sh       # 注入故障（link_down/high_latency/packet_loss/flow_missing）
bash 05_check_violated.sh     # 推故障态 → 期望 VIOLATED, violations≥1
bash 06_diagnose.sh           # 根因诊断 → root_causes + confidence
bash 07_heal.sh               # 自愈 → verdict + guard_checks
bash 08_verify_recovered.sh   # 验证恢复 → status=RECOVERED
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

**⭐ 联调前的自检（强烈建议先做这一步）** —— 用本地 mock 把 8 步链路先跑一遍，
这样联调时**出问题能立刻分清是"我方脚本"还是"对方服务"**：

```bash
cd ~/projects/intensure
python3 mock_assurance_server.py --port 8000 &     # 本地 mock 保障模块（version 0.4.1）
export ASSURANCE_BASE=http://127.0.0.1:8000
cd demo_scripts
for s in 01_register_intent 02_collect_state 03_check_baseline 04_inject_fault \
         05_check_violated 06_diagnose 07_heal 08_verify_recovered; do
  bash $s.sh
done
```

期望输出（2026-11-07 实测，8 步全绿）：

```
01 → register 成功: REGISTERED  →  activate 成功（ACTIVE）
02 → ActualState 已保存
03 → 基线正常，状态: ACTIVE
04 → 故障态已保存
05 → 检测到违规！状态: VIOLATED（violations 3 条）
06 → 诊断完成，状态: DIAGNOSING
07 → 自愈决策: APPROVED
08 → 恢复成功！状态: RECOVERED  ✅ 端到端闭环演示完成！
```

> mock 已于 2026-11-07 补齐到与脚本一致（单意图注册 / query 参数 / diagnose / heal），
> 所以它现在可以当作**联调前的回归工具**，不只是离线演示。

### 5.4 联调成功判据（建议双方按这个验收）

- [ ] `00_pre_check.sh` 5 项全过
- [ ] `01` → intent 状态 `ACTIVE`
- [ ] `03` → `status=ACTIVE`，`violations=[]`
- [ ] `04`+`05` → `status=VIOLATED`，`violations≥1`
- [ ] `06` → 返回 `root_causes` + `confidence` + 候选原因 + 修复策略
- [ ] `07` → 返回 `verdict`（**APPROVED / BLOCKED**）与 `guard_checks`（被闸门拦截也是**预期结果**，不是失败）
- [ ] `08` → `status=RECOVERED`（⚠️ 判据只看 status，不看 healing_verdict）
- [ ] 我方侧：`intensure.port_events` 里有对应时间戳的 `link_down` / `link_up` 记录
- [ ] 我方侧：`GET /api/intensure/v1/state/current` 的 `down_ports` 在故障窗口内非空

> 💡 **这 8 条判据已在我们这边本地逐条实测过**（2026-11-06/07），
> 不用等联调才知道成不成。实测样例：
> - 注入前 `down_ports = []`；注入 `link_down @ s1-eth2` 后 `down_ports = ['s1:eth2']`
> - `down_ports` 用的是**契约短标签**（`s1:eth2`，冒号分隔），
>   不是 OpenFlow 原始端口名（`s1-eth2`）—— 对方按短标签匹配
> - 端口事件落库：`intensure.port_events` 里 `s1-eth2 | link_down` / `link_up` 带精确时间戳
> - 操作审计落库：`shared.audit_log` 里 `inject_fault` / `clear_faults` 各一条

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

**Q：cloudflared 那个临时域名是不是就是正式地址？**
不是。`*.trycloudflare.com` 是**联调测试用的临时隧道**（cloudflared 起的临时域名，
重启就换），只为了在两边还没部署好时先把链路打通；**正式地址要等对方服务正式部署后
再给**。我们这边地址是运行时配置，**从测试地址切到正式地址不用改代码**（见 §2.1）。

**Q：隧道域名换了我们会断吗？**
不会立刻断 —— 我们配了 `ASSURANCE_BASE_FALLBACK` 兜底。但请在换域名后
**知会我们一声**（一条消息即可），我们更新配置，免得日志里出现一堆 000。

---

## 8. 联系方式与下一步

- 采集模块代码：`github.com/Akioo77/intensure`（v3.22.0，CI 全绿）
- 契约文档：`docs/cross-module-api-contract.md`（跨模块契约，§4.1/§5.1 端点定义、§5.7 版本协商）
- 接入手册：`INTEGRATION_GUIDE.md` §6.7（真实园区迁移）、§6.8（接入适配层）
- 演示讲稿 + Q&A：`docs/speaker_notes.md`

**我们这边的下一步**：拿到地址 → 跑完整 9 步联调 → 按 §5.4 验收；
同时等真实网络环境到位后做接入验证（§4.1）。

**需要对方的第一件事**：§2.1 的 **① 联调测试地址**（临时隧道就行，重启换域名是正常的，
我们这边有 `ASSURANCE_BASE_FALLBACK` 兜底）；同时请顺带告知 **② 最终环境地址**的形态。
