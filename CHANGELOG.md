# CHANGELOG — 网络意图 SDN 采集模块

> 历史变更记录。所有变更都有对应 git commit 与测试验证。
> **最新版本在最上面**（按版本号降序排列，非按日期）

---


---


## v3.21.0 - 2026-10-31 环境配置化 + 能力自检 + 只读模式

**触发**：最终验收环境还不确定 —— 三个人可能各配各的 Mininet，也可能接真实 OpenFlow 网络。
原先「本地 Mininet 实验环境」在代码里**写死了 7 处**（拓扑文件、IP 段、交换机命名、探测方式、
注故障方式、状态校验基线、端口名格式），**换一次环境就要改一轮代码**，而且往往是在赶演示的时候改。

这轮把「环境差异」从代码搬到配置文件，**环境能力变成运行时可查的事实**。

### 新增

**`env_config.py`（329 行）—— 环境配置层**
- 读 `env.json`（标准库可读，CI 也能跑）/ `env.yaml`（装了 PyYAML 才读）
- 路径用 `$INTENSURE_ENV_FILE` 覆盖；查找顺序：显式路径 → 环境变量 → `env.json` → `env.yaml`
- **配置缺失时用内置默认值，而默认值 == 改造前的行为** —— 开这个功能不改变任何现有行为，回归测试照常全绿
- `env.json` 里的 `//` 注释由 `_strip_json_comments()` 剥掉，所以配置文件能写注释

**`capabilities.py`（143 行）—— 能力自检**
输出：OpenFlow 协议与已连交换机数、拓扑发现方式（LLDP / degraded）、主机与拓扑期望比对、
探测驱动与参数、故障注入可用性（含不可用的原因）、契约路径版本与对方期望版本、只读标志。

**`env_doctor.py`（323 行）—— 体检 CLI**
一条命令回答「当前环境能不能干活、缺什么」；`--json` 给脚本用；退出码 **0 / 1 / 2**
（0 = 全通过，1 = 有 error 级失败，2 = 采集层没跑起来，连状态都拿不到）。

**`env.json` / `env.topo_simple.json`** —— 两份默认环境配置。

**`GET /api/capabilities`** —— 能力自检端点（挂在 `state_server.py`）。

### 改造

- `state_server` 的 `/api/validate` 改用配置里的拓扑期望（不再写死 2 交换机）
- **只读模式**：`mode: read-only` 时 `POST /api/v1/test/inject-fault` 一律 **403**
- `actual_state_builder` 的 `dpid_to_switch` 没显式传时从配置读
- `state_model` 的校验基线可注入
- CI pytest 列表补到 **14 个文件**

### 🐛 顺手修的两个真 bug（都是 env_doctor 在真实环境里抓出来的）

**1. 状态文件写不进去被静默吞**
旧 `/tmp/network_state.json` 是 **root 属主**（以前 sudo 起的控制器留下的），当前用户覆盖不了 →
控制器写盘失败被 `except: pass` 吞掉 → **状态永远停在 10-25 的旧快照**，
`state_server` / dashboard 读到过期数据而且**查不出原因**。
修：控制器写失败**只报一次**并给修复命令（`sudo rm -f /tmp/network_state.json`）；
`start_all.sh` 启动前删掉不可写的旧文件。

**2. 体检工具自己的 urllib 也被代理劫持**
VM 的 `http_proxy` 没配 `no_proxy` → 连 `127.0.0.1` 都走代理 → HTTP 不可达。
curl 那边 v3.19.0 已经修过，**Python 侧漏了**。修：`urllib` 也用 `ProxyHandler({})`（跟 `reporter.py` 一致）。

### 🧪 测试

- 新增 `test_env_config.py`（**15 例**），CI pytest 列表补到 **14 个文件**
- **测试总数 196 → 211**，本地 **211 passed**，`ruff check` 全过

### 📝 文档

`README.md`（文件清单 / 对外接口 / 环境配置小节 / 踩坑）、`docs/DEVELOPMENT_LOG.md`（时间线 +
`env_doctor` 用法 + 新增一条经验）、`docs/architecture.html` + `.png`（采集层加「环境配置 + 能力自检」）。

---


## v3.20.0 - 2026-10-31 落地 /api/intensure/v1/* 契约端点

**触发**：架构图（`docs/architecture.html`）里那个红框「⚠️ 待解决：契约路径不匹配」
—— 契约要求我们暴露 `/api/intensure/v1/state/current`，我们实际暴露 `/api/state`，
**对方按契约调 → 全部 404**。

### 新增

**`contract_api.py`（238 行）—— 显式转换层（纯函数）**

不是换个 URL 直接透传 —— 内部 state 是「我们自己写的数据结构」，契约要的是
「对方要看的结构」，两者不是一回事：

- `switches`：内部是 dict（按 dpid 键）→ 契约要 **list**
- `version`：用 `meta.update_count` 当**乐观锁**（对方靠它判断采集是否在更新）
- `captured_at`：UTC ISO 8601 带 `Z`（毫秒精度）
- `down_ports` / `down_links`：复用 `ActualStateBuilder` 的 `"s1:eth2"` 可读格式
- `speed_mbps` = OpenFlow `curr_speed`(kbps) ÷ 1000
- `OFPP_LOCAL`（`0xfffffffe`）端口剔除 —— 它永远 LINK_DOWN、没有线缆
- 不读网络、不发请求、不依赖 Flask

**`state_server.py` 新增 5 个契约端点**

| 端点 | Query / Body | 备注 |
|---|---|---|
| `GET /api/intensure/v1/state/current` | — | 未初始化返回 **424 DEPENDENCY_NOT_READY**（不是 200 空快照）|
| `GET /api/intensure/v1/state/history` | `?since=&until=&limit=` | summary 历史 |
| `GET /api/intensure/v1/port-events` | `?dpid=&since=&limit=` | 端口 up/down + 交换机连接事件 |
| `GET /api/intensure/v1/link-events` | `?since=&limit=` | LLDP 链路发现/丢失 + 交换机连接事件 |
| `POST /api/intensure/v1/probe-results` | `{"reachable":[],"unreachable":[],"loss_pct":0,"latency_ms":0}` | 写入 state_history |

**旧路径全部保留、行为不变** —— `/api/state` / `/api/links` / `/api/health` 等一个没动。
做法是**并行提供**：契约路径给对方按文档调，旧路径留给现有联调、dashboard、演示脚本。

### 测试

- 新增 `test_contract_api.py`（**17 例**），CI pytest 列表补到 **13 个文件**
- **测试总数 179 → 196**，本地 **196 passed**，`ruff check` 全过

### 📝 文档

- `docs/cross-module-api-contract.md`：§5.6 表格改成「契约路径 + 旧路径」双列，
  5 个 ⚠️ 改为 ✅ v3.20.0 已落地；新增 **§5.8**（5 个端点的实际请求/响应，VM 实测抓取）
- `README.md`：完成状态表补 v3.19.0 / v3.20.0；「对外接口」章节加契约端点表
- `INTEGRATION_HANDBOOK.md`：新增一节说明对方应该调哪套路径
- `docs/architecture.html` + `.png`：红框改为「✅ 已解决」

---


## v3.19.0 - 2026-10-29 故障注入 REST 接口

**触发**：联调手册里一直挂着一项「故障注入 REST 接口 ❌ 仍未实现」——
`demo_scripts/04_inject_fault.sh` 一直在调一个 404 的端点，只能 fallback 到
`sudo ./fault_cli.sh` 手动执行。演示时这一步很脆。

### 🆕 `POST /api/v1/test/inject-fault` + `GET/DELETE /api/v1/test/faults`

**新增 `fault_api.py`**（~340 行，不依赖 Mininet `net` 对象）

为什么不用现成的 `fault_injector.FaultInjector`：它需要 Mininet 的 `net` 对象
（**Mininet 进程内**才有），而 `state_server.py` 是**独立进程**，拿不到 net。
所以改用底层命令（`ip link` / `tc qdisc` / `ovs-ofctl` / `ovs-vsctl`）实现，
语义与已实测验证过的 `fault_cli.sh` 一致。

**支持 9 种故障全覆盖** + `clear`：

| 类型 | 实现 | 参数 |
|---|---|---|
| `link_down` / `link_up` | `ip link` + `ovs-ofctl mod-port` | — |
| `delay` | `tc netem delay` | `delay_ms`, `jitter_ms` |
| `loss` | `tc netem loss` | `percent` |
| `rate_limit` | `tc tbf` | `rate_mbps`, `burst_kb` |
| `burst` | `tc netem rate+limit` | `rate_mbps`, `limit_kb` |
| `reorder` | `tc netem delay+reorder+gap` | `gap_percent`, `gap_packets` |
| `vlan_mismatch` | `ovs-vsctl set port tag=` | `vlan_id` |
| `flow_race` | 并发 `ovs-ofctl` 改流表 | `rounds` |

**别名**：演示脚本用的业务语义名也能用 ——
`high_latency`→`delay`、`packet_loss`→`loss`、`flow_missing`→`flow_race`。

**target 两种写法**：具体接口名（`s1-eth1`）或交换机名（`s1`，`params.port` 可限单个端口）。

### 🔧 实测踩出来的 3 个 bug

1. **tc 复合参数必须拆成独立 argv 元素**
   写 `f'rate {rate}mbit'` 会把整串当成**一个**参数，
   tc 报 `unknown parameter "rate 1.0mbit"`。
   → 改成 `['rate', f'{_num(rate)}mbit']`，并加 `_num()` 把 `1.0` 格式化成 `1`
   （tc 不认 `1.0mbit`）。
2. **清理必须幂等**：`tc qdisc del` 在「本来就没有 qdisc」时报
   `Cannot delete qdisc with handle of zero` —— 这是**正常**状态，
   不能让整个清理失败 → 清理类命令用 `check=False`。
3. **字典当列表迭代**：`_all_switch_ports()` 返回 `{sw: [ports]}`，
   直接迭代拿到的是 switch **名**（`'s1'`）而不是端口 →
   全量清理只清了 1 个「假接口」。→ 加 `_all_interfaces()` 扁平平铺。

### 🧪 新增 `test_fault_api.py`（12 例）

覆盖别名映射、接口解析、argv 拆分、幂等清理、全量清理、
「未知类型 / 不存在接口 → 抛错（路由层转 400）」。
所有特权命令全 mock —— **测试不该真的动网络**。

单测还抓出一个设计问题：`_all_interfaces()` 里硬编码 `FaultApi._all_switch_ports()`
（类级调用）绕过了实例覆盖、没法 mock → 改成 `self._all_switch_ports()`。

### 📡 可观测性

新增 Prometheus 计数器 `intensure_fault_injections_total{type,result}`。

### 📜 文档

- `INTEGRATION_HANDBOOK.md`：联调待办「故障注入 REST 接口」标记完成
- `README.md`：完成状态表加 v3.19.0；**修正一条过期待办** ——
  「`reporter.py` 频率 + DNS 老 bug（100% 失败）」实际 **v3.10 已修**
  （`ProxyHandler({})` 绕代理 + 默认 UA），优化清单 P0-2 也早就标了完成
- `demo_scripts/04_inject_fault.sh`：接上真端点（补 `target` 参数 +
  端口自适应 18080/8080 + 按 HTTP 状态码判定成功失败）

---


## v3.18.0 - 2026-10-29 P3-4 可观测性 + P3-3 契约版本管理

**触发**: 优化清单剩余 4 项中的 P3-3 / P3-4 —— 演示和联调需要能回答两个问题：
「现在每分钟多少上报、多少错？」以及「对方接口悄悄升级了怎么办？」

### 📊 P3-4 · Metrics / Observability

**新增 `metrics.py`（333 行，纯标准库）**

没用 `prometheus_client`，原因三条（写在模块 docstring 里）：
CI 是离线/受限网络环境，多一个第三方依赖就多一次装不上的风险；本项目只需要
counter / gauge / histogram 三种类型；不引入依赖 = 不引入版本升级与安全维护负担。

支持能力：
- `Counter` / `Gauge` / `Histogram` 三种指标
- 标签（labels）维度
- Prometheus 文本 exposition 格式（**version 0.0.4**）
- 线程安全（采集层与上报层是多线程的）

**已注册的指标**

| 指标名 | 类型 | 标签 | 含义 |
|---|---|---|---|
| `intensure_reports_total` | Counter | endpoint, result | 上报请求总次数（按端点与结果分类）|
| `intensure_report_failures_total` | Counter | endpoint, reason | 上报失败总次数（按失败原因分类）|
| `intensure_report_duration_seconds` | Histogram | endpoint | 上报耗时（秒）|
| `intensure_http_requests_total` | Counter | method, endpoint, status | 自营 REST 请求总数 |
| `intensure_http_request_duration_seconds` | Histogram | endpoint | 自营 REST 耗时（秒）|
| `intensure_assurance_contract` | Gauge | state | 与保障模块的契约版本状态（1=当前状态）|
| `intensure_build_info` | Gauge | version | intensure 构建信息（`3.18.0`）|

**`reporter.py` 埋点**（`_record_stats` 内，check / verify / record 三个入口共用）
- `REPORTS_TOTAL` 按 `endpoint` + `result` 计数
- `REPORT_DURATION` 直方图记录耗时（`latency_ms` / 1000 换算成秒）
- 非 ok 时额外记 `REPORT_FAILURES_TOTAL`，`reason` 取值：
  `ok`（2xx）/ `client_error`（4xx，含 409 状态机错）/ `server_error`（5xx）/ `network_error`（连不上，status_code == 0）

**`state_server.py` 埋点**
- 新增 `GET /metrics` 端点，输出 Prometheus 文本格式
  （`Content-Type: text/plain; version=0.0.4`），**没装 prometheus_client 也能被 Prometheus 直接抓取**
- `before_request` / `after_request` 钩子：每个请求记一次计数（method / endpoint / status）+ 耗时直方图
- `/api/health` 增加 `version` 字段（当前 `3.18.0`）

### 🔐 P3-3 · 跨模块 API 版本管理

**`assurance_bridge.py` 新增契约版本协商**

原来对方接口没有 version 字段，他升级到 v2 我们这边**没人通知就坏了**。
现在的做法是**可选协商**，不打断现有联调：

- 读对方 `/api/health` 的版本字段，按优先级依次尝试：
  `version` → `api_version` → `agent_version` → `assurance_version` → `schema_version`；
  也支持嵌在 `service` / `server` / `meta` / `info` 对象里
- 我方期望版本常量 `DEFAULT_EXPECTED_VERSION = '0.4.0'`，契约 schema 版本 `CONTRACT_SCHEMA_VERSION = '0.1.0'`
- **60 秒 TTL 缓存**（`VERSION_CHECK_TTL_S`），避免每次推送都多打一次 `/api/health`
- 状态机 6 态：

| 状态 | 含义 | 是否阻断上报 |
|---|---|---|
| `ok` | 对方版本 == 期望版本 | 否 |
| `compatible` | 同 major.minor（补丁级差异）| 否 |
| `unknown` | 对方没暴露版本字段 | **否**（默认兼容模式，只 warn）|
| `mismatch` | major.minor 不一致 | **是** |
| `unreachable` | 对方连不上 | 否（让真正的请求去报错）|
| `disabled` | 未配置 `ASSURANCE_BASE` | 否 |

> ⚠️ **为什么 `unknown` 不阻断**：对方**目前确实没有 version 字段**（`/api/health` 只返回
> `{"status":"ok"}` 一类内容）。如果把「读不到版本」也当成不兼容，现有联调会 100% 被打断。
> 所以默认走兼容模式，只在日志里 warn 一次。

**两个环境变量**

| 变量 | 作用 | 默认 |
|---|---|---|
| `ASSURANCE_STRICT_VERSION=1` | 严格模式：对方没暴露版本字段时也拒绝推送 | 关闭（不阻断）|
| `ASSURANCE_EXPECTED_VERSION` | 临时对齐对方版本（如对方升到 `0.5.0` 就设这个），**不用改代码** | `0.4.0` |

**`mock_assurance_server.py`**：加 `version` 字段（默认 `0.4.0`）+ `--version` 参数，
可模拟版本不兼容来验证降级路径。

**CLI 新增 `contract` 子命令**

```bash
python3 assurance_bridge.py contract
# 契约版本协商:
#   期望版本: 0.4.0
#   对方版本: （未暴露）
#   状态    : unknown
#   说明    : ...
#   可上报  : 是
```

### 🧪 测试

- 新增 `test_metrics.py`（**10 例**）、`test_contract_version.py`（**11 例**）
- CI 的 pytest 文件列表已补上这两个文件
- **测试总数 146 → 167**，本地跑 **167 passed**，`ruff check` 全过

### 📝 文档

本条目之外，同步更新了 README.md、docs/OPTIMIZATION.md、
docs/cross-module-api-contract.md、INTEGRATION_HANDBOOK.md。

---

## v3.17.0 - 2026-10-28 P2-3 复杂故障类型：burst / reorder / vlan_mismatch / flow_race

伙伴的诊断分支（port_failure / link_failure / 高延迟）之前只有「断链」一种真实样本可验，
其余分支根本没数据。现在补上 4 种。

- **burst** —— tc netem rate + 小 limit，队列被打爆（microburst）。
  与 `rate_limit` 的 tbf 平滑整形是不同故障
- **reorder** —— tc netem delay + reorder + gap
- **vlan_mismatch** —— 给交换机端口打 VLAN tag（发方带 tag / 收方期望 untagged）
- **flow_race** —— 两个 subprocess 真并发改流表

`clear_all` 同步补了 VLAN 清理，否则 vlan_mismatch 撤不掉。

### 🐛 实测踩出 4 个坑（实现里都注释了）

1. `netem reorder` 必须配 `delay` —— 只写 reorder 报
   `reordering not possible without specifying some delay`
2. `netem` 的 `gap` 单位是**包数**不是时间 —— `gap 20ms` 报 `Illegal "gap"`
3. 这个 ovs-ofctl 版本**没有 `mod-vlan` 命令**（unknown command），
   改用 OVS 原生 `ovs-vsctl set port <port> tag=999` 等价实现
4. `node.cmd()` **不是线程安全的** —— Mininet 只有一个 shell，并发调用撞
   `assert self.shell and not self.waiting`。flow_race 改用 subprocess 起两个
   独立进程，既是真并发也才真造得出竞态

### ✅ 验收

`demo_scripts/test_fault_types.py` 在真实 Mininet 拓扑上 **12/12 全过**
（4 种故障各生效 + 记进 active_faults + clear_all 全清 + 链路恢复）。
该脚本需 mininet/ovs-ofctl/控制器，属集成测试，不进 CI 单元测试套件。

---

## v3.16.0 - 2026-10-28 P1-2 日志统一：75 处 print → logging

**触发**: 主人「继续做计划内的」-> P1-2（代码库里 1 个文件用 logging、23 个用 print，
演示时输出噪声巨大且无法按级别过滤）。

### ✨ 新增 `logging_setup.py`

统一配置：级别 / 格式 / 文件 handler / **幂等**（重复调用不叠加 handler）。
环境变量 `INTENSURE_LOG_FILE` 指向的路径会启用 file handler（不设则不写文件，
测试/CI 不污染 /var/log）。

### 🔁 改造 7 个模块，75 处 print

`controller_app` / `live_env` / `assurance_mvp` / `db_writer` / `run_all` /
`reporter` / `state_server` / `fault_injector`

**输出走 stderr** —— demo 脚本全是 `cmd 2>&1 | tee xxx.log`，合并流所以照样拿得到，
不会因为改造日志而破坏演示脚本。

级别自动判定：❌/ERROR → error，⚠️/WARN → warning，`if DEBUG:` 块内 → debug，其余 info。

**CLI 工具故意不改造**（`demo.py` / `demo_full.py` / `verify_live.py` /
`seed_policies.py`）—— 它们的 stdout 就是对外接口，换成 logging 反而破坏使用方式。

### 🐛 自己踩的坑（已被测出来）

自动插入 `configure_logging()` 时**没换行**，把后面那行代码吞进了注释 ——
`setLogLevel('warning')`、`logger.info('=' * 60)` 各 1 处被注释掉。
是 ruff 报「setLogLevel 未使用」才暴露出来的 —— **这正是加 ruff 闸的价值**。

### ✅ 验收

- ruff 全绿、单元测试 **135 passed**
- **完整 demo 8/8 全绿、EXIT=0**（最关键 —— 改了输出路由）
- 日志文件实测落盘正常（`/var/log/intensure/intensure.log`）

### 📋 状态

优化清单 v1.0：**已完成 15 / 待办 5** —— **P0 与 P1 全部完成**。

---

## v3.15.0 - 2026-10-28 P2-4 DB 自动重连 + P2-5 历史归档定时清理

**触发**: 主人说「先继续做计划内的，不急着联调」。

### 🔌 P2-4 DB 自动重连

`db_writer._get_conn` 原来有两个问题：PG 重启后每次调用直接抛 OperationalError
（看起来像「永久失败直到重启 intensure」），且 `psycopg2.connect` **没设 timeout** ——
libpq 默认无超时，PG 真挂了会长时间挂起。

现在：
- 最多 3 次尝试，指数退避 **0.5 / 1 / 2 秒**，日志打印 `reconnecting in Xs`
- `connect` 带 `connect_timeout=5`
- **区分「连接死了」和「查询本身出错」**：前者重连，后者直接冒泡不重连
  （语法错/权限不足时重连只会掩盖真因）
- 退避等待在锁外，不阻塞其他线程
- 新增统计：`reconnects`（成功建连次数）/ `connect_retries`（失败重试次数）

**实测**：`systemctl restart postgresql` 后写入自动恢复（**0.91s**），
stats 显示 reconnects=3 / errors=0。另加 3 个回归测试锁住行为。

### 🗄 P2-5 历史数据归档（定时清理）

**先确认了问题真的存在**：VM 里既没有 crontab 也没有 systemd timer ——
文档写的「30/7 天保留」**从来没被自动执行过**。

- 新增 `db_maintenance/retention-cleanup.service` + `.timer`（进版本库，可复现）
- 每天 **04:07** 跑（避开整点不撞车）+ 随机延迟 5 分钟 + `Persistent=true`
  （关机/休眠错过了开机补跑 —— 笔记本场景重要）
- 跑 `db_maintenance/run_retention_cleanup.sh`（systemd 包装脚本）

**踩到并修掉的坑**：服务用 `User=postgres` + `psql -f` 报 `Permission denied` ——
项目目录 0700，postgres 读不到 .sql 文件。改成 `User=sunxiaoxuan` + **stdin 管道**
（`<` 重定向）：由项目属主打开文件喂给 psql 的 stdin，postgres 不需要读路径
—— 和 alembic 里 `_exec_raw` 同一思路。

**实测**：造了 2026_08 的过期分区 → 触发 service → **两个分区都被删掉**，只剩当月及以后。

### 📊 状态

- 优化清单 v0.9：**已完成 14 / 待办 6**（P0 全清 + P1-1/3/5 + P2-1/4/5 + P3-1）
- 单元测试 132 → **135**（新增 3 个重连回归测试）

---

## v3.14.0 - 2026-10-27 P3-1 alembic：schema 迁移工具 + 伙伴方案 A 达成

**触发**: 主人让「先做 3.1」，同时处理伙伴回传的新 SQL 文件。

### 🤝 伙伴新脚本 `assurance_schema-2.sql`（v1.1）

伙伴**已按方案 A 修订**：移除 `healing_intents` / `healing_history` 的 CREATE TABLE
（采用我方结构），确认我方已加 status/priority/affected_devices/expires_at，
自愈队列存 `_metadata`。

**核对结果**：新旧脚本的 9 张公共表结构**逐张完全一致**（列数 2/5/5/17/12/19/15/12/12），
现库这 9 张表与新脚本一致 → **新脚本对我方库是 no-op，无需执行**。
我方库已处于双方约定的最终状态。

### ✨ P3-1 alembic

把「改 schema」从「手工 DDL + 手工同步两个文件」变成「一条命令 + 一个版本号」。
过去两天因此出过 3 次事故（error_archive 漏进规范文件 / RAISE 参数不匹配 /
分区删除用起始日会删掉当月分区），现在都有可执行检查兜着。

- `alembic.ini` + `migrations/env.py`（自定义，**不用 autogenerate** —— 本项目无
  SQLAlchemy 模型，autogenerate 没比对基准只产噪音）+ `script.py.mako`
- `0001_baseline.py`：**由 `db_maintenance/gen_baseline.py` 从 `docs/database-schema.sql`
  自动生成**（不手抄，保证基线 == 规范文件）
- `0002_retention_function.py`：只建函数。**刻意不含清理主流程** —— 迁移必须无副作用，
  把删分区的动作放进 upgrade 等于「升个版本就删数据」，清理留在
  `db_maintenance/run_retention_cleanup.sh`（cron 跑）
- **顺手修掉分区定时炸弹**：schema 把月分区写死 2026_10/11/12，2027 年 1 月建新库
  就没有当月分区 → 任何 INSERT 报 `no partition of relation ... found for row`。
  基线改为按「当前月 + 0..2」**动态创建**
- `docs/ALEMBIC_WORKFLOW.md`：用法 + 流程 + **8 个踩过的坑**
- `verify_fresh_db.sh` 改走 alembic 路径，新增「**基线 vs 规范文件漂移检查**」
- CI 新增「检查基线与 schema 规范文件一致」（纯文件比对，**不需要数据库**）

### ✅ 验收

- `alembic upgrade head` 从零建库 → 5 个 schema 表数与现库逐一相等
- `drop_old_partitions()` 函数已建、7/7 分区表有当月分区、7/7 写入路由正常
- `downgrade base` 干净清库（0 残留）→ 再 `upgrade head` 正常
- `verify_fresh_db.sh` → ✅ 全部通过 —— 新建库 = 现有库

### 🛠 本轮踩的坑（都记进 ALEMBIC_WORKFLOW.md）

1. `format('%I', 'a.b')` → `"a.b"` 是**一个带点的标识符**，PG 当成普通表名 →
   报 `relation "a.b" does not exist`。必须拆成 `%I.%I`
2. `op.execute(sa.text(sql))` → `::jsonb` 的冒号被当绑定参数
3. `exec_driver_sql` 传空 dict → psycopg2 多语句 SQL 报 `immutabledict is not a sequence`
4. `\set` 是 psql 元命令 → 走 DBAPI 语法错 → 整个事务回滚
5. alembic 会 **import `versions/` 下所有 .py** → 生成器放那儿会在 import 时炸
6. Python `%` 格式化模板里字面量 `%I` 必须处处转义 → 改用 `str.replace()` 占位符
7. rsync 不删文件 → 旧 revision 残留造成 multiple heads → 同步要加 `--delete`
8. 分区写死月份 → 2027 年建库就炸

---

## v3.13.1 - 2026-10-27 修「新建库 ≠ 现有库」+ 分区清理脚本致命 bug

**触发**: 复查 2026-10-27 00:03 的 heartbeat 改动，发现它引入/遗留了 3 个真问题。

### 🐛 修的真问题

1. **`db_maintenance/retention_cleanup.sql` · RAISE 参数不匹配（致命）**
   `RAISE NOTICE 'Dropped: % (range %, cutoff=%)'` 只有 3 个占位符，却传了 **4 个参数**
   → 脚本报 `ERROR: too many parameters specified for RAISE`，**整个脚本无法执行**。
   补成 4 个占位符。
2. **`docs/database-schema.sql` 缺 `shared.audit_log_error_archive`**
   该表在现库存在但规范文件里没有 → **「新建库 = 现有库」这条承诺是破的**。
   已从现库导出真实 DDL 补进规范文件（含主键 + 2 个索引）。
3. **分区清理的判定逻辑**（heartbeat 昨晚修的那版）
   `drop_old_partitions` 原按「分区**起始日** < cutoff」判断 → 保留 7 天时当月分区起始日
   (2026-10-01) < cutoff (今天-7)，**当月分区被删**，之后任何 INSERT 报
   `no partition of relation "state_history" found for row`。
   改为按**结束日**判断（只有整个区间都早于 cutoff 才删）。已用真实分区数据模拟验证：
   旧逻辑会删当月、新逻辑不删。

### 🔧 新增 `demo_scripts/verify_fresh_db.sh`

把「新建库 = 现有库」变成**可执行的验证**而不是口头承诺：
建临时库 → 套 schema → 套 retention → 5 个 schema 表数逐一对比 →
校验每个分区表都有覆盖 now() 的分区 → 7 张分区表写入路由冒烟 → 清理。

**同时修掉它自己的 2 处误报**：
- 步骤⑤ 原按「TO = 当月」匹配分区，但覆盖当月的分区 TO 是**下月**（10 月分区 TO 2026-11-01）
  → 逻辑反了，把 7/7 误报成 0/7。改为「区间包含 now()」判定。
- 步骤⑥ 用 `INSERT ... DEFAULT VALUES` 必然撞 NOT NULL，报的是「null 值违规」而非「分区缺失」，
  而**null 值违规恰恰证明分区是找到了的**。改为按错误类型分类，只有
  `no partition of relation` 才算真失败。

### 🛠 本轮自己的坑

- **heredoc 结束标记被我多缩进了 2 个空格** → bash 不认（结束标记必须在行首），
  把**后面整个脚本当 heredoc 内容吃掉**，脚本「静默退出 0」看起来像成功。
  → 改完必须 `bash -n` + 看 heredoc 标记是否在行首。

### ✅ 验收

`bash demo_scripts/verify_fresh_db.sh` → **✅ 全部通过 —— 新建库 = 现有库**
- 5 个 schema 表数逐一相等（assurance 20 / intensure 18 / shared 7 / translation 3 / implementation 5）
- 7/7 分区表有覆盖当月的分区
- 7/7 分区表写入路由正常（无 no partition found）

---

## v3.13.0 - 2026-10-26 P2-1 压测：实测基线 + 修正容量估算

**触发**: 剩下 10 项里唯一「高价值 + 完全自主」的一项，给「100 意图扛不扛得住」一份实测基线。

### 🆕 新增

- **`demo_scripts/load_test.py`** —— 压测工具，模拟线上负载（每周期 1 次 network_state upsert
  + N 个 consistency_check + 违例的再写 diagnoses + state_history 按 30s 节流）。
  采集写延迟 p50/p95/p99、周期耗时、吞吐、CPU/RSS、PG 连接数/锁等待/活跃事务、热表体积增长。
- **跑了两组对比**（100 意图）：
  - 最坏上界（每 intent 每周期都写）10 分钟 → 13,431 写，22.3 写/秒，**0 错误**
  - 真实模式（走 `should_write_consistency`，与线上 `assurance_mvp.py:382` 一致）3 分钟 → 1,047 写，5.8 写/秒，**0 错误**

### 📊 实测结论

**算力完全不是瓶颈**：CPU 平均 2.4% 峰值 28.3%，RSS 恒定 30.6 MB，
PG 锁等待 **0**，连接数全程 1，5s 周期实际只用 p50 395ms（预算 8%）。
**瓶颈是磁盘行数增长，不是 CPU。**

实测单行字节：`consistency_checks` **279 B**、`diagnoses` **426 B**、`state_history` **2,201 B**。
→ 最坏模式 482 MB/天 → 176 GB/年；真实模式 ~46 MB/天 → **~17 GB/年**。

### 📝 修正 retention-policy.md 的两处偏差

1. 混合策略行数原估 17,000/天 → 实测 **144,000/天**（60s 心跳 × 100 意图是节流地板值，省不掉，
   原估低估 8.5 倍）→ 「1 年 ≈ 13 GB」修正为 **~17 GB**
2. 「够用 1.2 年+」修正为「够用 1 年」

**新增发现**：故障期 `is_diagnosing=True` 强制每次都写，**写放大翻倍**（实测 213 写/分 vs 稳态 100 写/分）
→ **容量规划要按故障态算，不是稳态**。

### ✅ 顺带修的 CI 问题

- `v3.12.1` 那次 CI **失败**了 —— 是我自己新加的一行模式提示超长（E501 147>100）。
  **这是昨天加的 ruff 闸第一次真发挥作用**（它自己抓到了我的 bug）。已修，CI 恢复 success。

### 📝 文档

- `docs/load_test_results.md`（新建）—— 完整基线报告
- `docs/loadtest_raw/`（新建）—— 两次压测原始输出
- `docs/retention-policy.md` —— 容量估算按实测修正 + 加附录
- `docs/OPTIMIZATION.md` v0.6 -> v0.7：P2-1 完成，已完成 10 -> 11，待办 10 -> 9

---

## v3.12.1 - 2026-10-26 让代码兑现契约：诊断表写入开关 + 脚本进文档

**触发**: 复查「跟伙伴承诺了但代码没做」的事。

### 🐛 修「文档与代码不一致」

跟伙伴的契约里写了「**生产路径我方不写 `assurance.diagnoses`**」，
但 `assurance_mvp.py` 当时是**无条件写**的 —— 文档一套、代码另一套
（跟我之前批评 README「虚报职责」是同一类问题，这次是我自己）。

**新增开关** `ASSURANCE_WRITE_DIAGNOSES`：
- `1`（默认）= 本地 mock 模式，写 `assurance.diagnoses`，离线 demo 自己跑通闭环
- `0` = 联调/生产模式，**不写** diagnoses，诊断归伙伴的 `diagnosis_results`，
  我方只写 `consistency_checks`

启动时会打印当前模式，避免「不知道自己在哪个模式」。

**实测**（monkeypatch 强制违例 + 记录写入调用）：
| 模式 | 违例数 | write_diagnosis 调用 |
|---|---|---|
| `1`（mock） | 2 | **2 次** ✅ |
| `0`（联调） | 2 | **0 次** ✅ |

### 📝 文档

- `README.md` 新增「1️⃣.5 一键启停 + 状态体检」小节；联调模式示例加 `ASSURANCE_WRITE_DIAGNOSES=0`
- `~/Desktop/intensure_操作手册.md` 新增「2.5 一键启停」小节（演示现场看的这份）

---

## v3.12.0 - 2026-10-26 落地伙伴 9 张无冲突保障表

**触发**: 伙伴发来 assurance_schema.sql（Downloads），实测冲突后决定先落无冲突的部分。

### ✅ 已落地（9 张新表 + 22 个索引）

intent_registry / device_inventory / assurance_records / violation_events /
diagnosis_results / audit_logs / fault_patterns / case_records / _metadata

流程：备份 → staging dry-run（9 张全过）→ 真库落地 → 精确授权 → 验收。
`assurance` schema 从 11 张表变 20 张。**原有数据零丢失**
（consistency_checks=538 / diagnoses=148 / network_state=1 / state_history=244）。

### 🔒 安全处理（3 层过滤）

落地前把伙伴脚本里不可直接执行的部分剔掉：
1. **2 张硬冲突表**（healing_intents / healing_history）的 CREATE TABLE + 6 个索引
2. 兜底剔除**所有仍引用这 2 张表的语句**（COMMENT ON 等）—— 第一版漏了，
   dry-run 时报 `relation does not exist`，补上后才通过
3. **伙伴的权限块 DO $$ ... $$;** —— 他说是「已注释」但实际是活代码，
   且含 `GRANT SELECT ON ALL TABLES IN SCHEMA intensure` 与
   `ALTER DEFAULT PRIVILEGES`，比我方逐表授权的设计更宽。改为我方口径精确授权。

### 🔐 授权

新表必须单独授权 —— PG 的 `GRANT ... ON ALL TABLES` **只覆盖授权时已存在的表**，
新建的表不会自动继承。已对 9 张新表单独授 SELECT/INSERT/UPDATE/DELETE 给
`assurance_user`，并对 assurance schema 的序列授权。

### ✅ 验收（关键：冲突表没被动过）

- `assurance` schema 20 张表
- `healing_intents` 仍是我们 **19 列**（含 status / priority）
- `healing_history` 仍是我们 **11 列**（含 result_details）
- **`healing_id` 列数 = 0** —— 证明伙伴脚本没有覆盖我方表
- `assurance_user` 对新表有完整读写

### 📝 文档

- `docs/database-schema.sql` 新增 5.1 节（9 张表的实际 DDL，从库里导出保证一致）
- `docs/access-control-grants.sql` 补 9 张新表的授权

---

## v3.11.2 - 2026-10-26 P1-5 一键启动脚本 + 按伙伴反馈补齐 healing 字段

### ✨ 新增（P1-5）

3 个脚本，均已在 VM 实测通过：
- **start_all.sh** —— 一键起 4 组件（tmux 窗口 controller/api/mininet/assurance，
  --with-live 加 live_env）。起完自动校验端口与 /api/health。
- **stop_all.sh** —— 一键优雅停止：先 SIGTERM（走 graceful.py 落盘/关DB/停拓扑），
  8 秒未退则强杀；同时清 tmux 与 Mininet，最后复查残留。
- **status.sh** —— 5 段体检：进程 / tmux 窗口 / 端口+健康 / 13 张表行数 / 汇总。

实测：start_all 起 4 组件全绿；status 全绿 exit 0；stop_all 三进程全部优雅退出、
tmux 与 Mininet 清理干净、DB 数据保留。

### 🤝 按伙伴反馈补字段（契约对齐）

**冲突1** —— 采用我方 healing_intents 结构，补齐伙伴要求的 4 个字段：
- status TEXT NOT NULL DEFAULT 'pending' + CHECK 约束（pending/approved/rejected/executed/failed）
- priority INTEGER NOT NULL DEFAULT 100（队列排序，越小越先）
- affected_devices TEXT[] NOT NULL DEFAULT '{}'（Guard G3 范围检查）
- expires_at TIMESTAMPTZ（写入时不传则自动 now()+ttl_seconds）
- 另补 2 索引：队列扫描 (status,priority,created_at) + 过期清扫 (expires_at)
- 其余 strategy_spec / guard_passed / has_conflict 放 metadata JSONB
- 语义区别：verdict 是 Guard 判定，status 是信封生命周期，两者独立

**冲突2** —— 职责分离不合并：伙伴写 diagnosis_results（含 CoT），我方写
consistency_checks；生产路径我方不写 diagnoses（表保留，仅本地 mock 用）。

**healing_history** —— 分区保留，补伙伴需要的 result_details JSONB + verified BOOLEAN；
healing_id 对应我方 healing_intent_id。

### 🐛 Bug 修复（实测发现的真 bug）

- **db_writer.query() 含字面量百分号静默返回空**：原实现 cur.execute(sql, params or ())，
  空元组会触发 psycopg2 的 %-插值，SQL 里 LIKE 的百分号被当格式符 → 报错 →
  except 吞掉 → 返回空列表。表现为「明明有数据，LIKE 查询却查不到」。
  query_one() 同样问题。已修为「空参数不传」，并加回归测试 test_query_with_literal_percent。

### 📝 文档

- docs/database-schema.sql 同步新字段（保证「新建库 = 现有库」）
- docs/partner_schema_confirmation.md（新建）—— 给伙伴的字段确认回复
- docs/OPTIMIZATION.md v0.5 -> v0.6：P1-5 完成，已完成 9 -> 10，待办 11 -> 10

### ✅ 验收

- healing 新字段 12 项实测全过（含 CHECK 拒绝非法 status、分区表写入、索引可用）
- 单元测试 131 -> 132（新增百分号回归测试）
- DDL 前已备份 ~/intensure-backups/pre_healing_ddl.sql

---

## v3.11.1 - 2026-10-26 P1-3 代码风格：ruff 接入，CI 加防退化拦截

**触发**: 继续推进不依赖伙伴的工作 -> P1-3（代码风格靠作者自律）。

### ✨ 新增

- **`pyproject.toml`** —— ruff 配置
  `line-length=100` / `target-version=py312` / `extend-exclude=["legacy", ...]`（legacy 是 v2.0 归档代码，不维护）。
- **`.pre-commit-config.yaml`** —— ruff-pre-commit v0.6.9，钩子 `ruff --fix`，排除 `legacy/`。
  故意**不加** `ruff-format` hook：那会重排整个文件，制造与本次改动无关的大 diff。

### 🔧 一次性整改（76 处）

- `ruff check --fix` 自动修 **52 处**：未用 import（19）、f-string 无占位符（25）、
  文件尾缺换行（10）、多 import 挤一行（5）、未用变量（5）等。
- 手工拆 **24 处 E501 长行**：只改排版不改语义 —— 字符串用隐式拼接，
  SQL 用换行等价替换（`INSERT INTO t` / `CASE WHEN` 拆行后语义完全一致）。

### ⚠️ 关键豁免（不是风格问题，是硬约束）

- **E402（module-import-not-at-top-of-file）必须豁免**：
  `eventlet.monkey_patch()` 必须在 `os_ken` 之前调用，否则 eventlet 不会 patch
  os_ken 用到的 socket，**控制器直接挂**。涉及 `controller_app.py` / `live_env.py` /
  `run_all.py` / `integration_demo.py` 共 27 处。
- **E741（单字母变量名）豁免**：解析器里 `l`/`i` 表达索引是领域惯例。
- `legacy/` 整个目录排除在 lint 之外。

### 📝 其他

- **CI 新增 `ruff check .` 步骤** —— 防止风格退化。
- `docs/OPTIMIZATION.md` v0.4 -> v0.5：P1-3 移入「✅ 已完成」段；
  已完成 8 -> 9 项，待办 12 -> 11 项。

### ✅ 验收

- `ruff check .` → **All checks passed!**
- 单元测试 131(host) / 142(VM) 全绿（改行前后各验证一次）
- F821 undefined-name 与 F811 redefinition 排查后确认**只存在于 `legacy/`**（v2.0 归档代码），活代码无此问题。

---

## v3.11.0 - 2026-10-26 P1-1 测试覆盖：+131 个用例，CI 修复

**触发**: 主人说「先在没有其他人的情况下推进我们自己的工作」-> 挑 P1-1（测试覆盖率极低）做，
全程不依赖伙伴。

### ✨ 新增测试（6 个文件 / 131 用例，host）

- **`test_db_writer.py`**（35）—— 写库关口
  12 个 `write_*` 方法的成功+失败路径（失败返回 False/None 不抛、errors 计数 +1）、
  连接懒加载/复用/探活失败重连、state_history 30s 节流 + 变化触发 + force、
  `should_write_probe` 四种 anomaly_type、`should_write_consistency` 五种判定。
  含真实库集成用例（无 PG 时自动 SKIP）：upsert 幂等（version 递增且只有一行）+ 节流只落一行。
- **`test_assurance_mvp.py`**（24）—— 状态机核心
  3 个 extract 边界（按 IP/MAC 索引、/mask 剥离、OFPP_LOCAL 跳过、链路双向去重）+
  `check_one_policy` 7 种转换 + `diagnose_violation` 4 种诊断规则。
  重点覆盖「ARP 未触发时不算违例」这个易错边界。
- **`test_state_server.py`**（11）—— 11 个 REST 端点
  状态码 / JSON 格式 / since+limit 参数过滤 / 缺 key 返回 {} 而非 500 / links 双向去重成对。
- **`test_state_history.py`**（10）—— 环形缓冲 + 原子持久化
  MAX_ENTRIES 封顶、get 语义（key/limit/limit=0/未知 key）、dump 先写 .tmp 再 rename、load 往返。
- **`test_ovs_logger.py`**（15）—— 日志解析
  行解析、空行/畸形行返回 None、**UTC 时间戳不偏 8 小时**（这处错了归因会全错）、
  since/until/module/level/keyword/limit 六种过滤、buffer 定长、关联函数结构。
- **`test_state_model.py`**（26）—— 5 个验证器 + 主入口
  schema/sanity/consistency/completeness/anomaly 各自边界 +
  `validate_state` 的 ok/issue_count/by_level 汇总。

### 🐛 Bug 修复

- **CI 形同虚设**：`ci.yml` 原本只跑 `test_actual_state_builder.py test_reporter.py`，
  新增的测试文件根本不会被执行 —— 名义上有 CI，实际漏掉全部新增覆盖。
  现改为跑 9 个测试文件（`test_recovery.py` 排除：它是手动脚本，硬编码 VM 路径 + 需要真实 mininet 拓扑）。
- **`test_db_writer.py` 类名触发 pytest 误收集**：`TestWriter` 会被 pytest 当测试类收集并告警，
  改名为 `FakeWriter`。
- **真实库用例不幂等**：连续跑第二次会因上次的残留行而失败。
  改为跑前清理 + `finally` 清理（`intensure_user` 有 DELETE 权限）。

### 📊 统计

- 单元测试 **21 -> 131**（host）/ **142**（VM，多 11 个 Flask 用例）
- 6 个裸奔模块补齐：`db_writer` / `assurance_mvp` / `state_server` /
  `state_history` / `ovs_logger` / `state_model`
- 关键模块从 3/9 提升到 **9/9 有测试**
- CI 从 2 个测试文件提升到 9 个

### 📝 文档

- **`docs/OPTIMIZATION.md`**：v0.3 -> v0.4。P1-1 移入「✅ 已完成」段；
  **P0 全部完成 + P1-1 完成**，已完成 7 -> 8 项，待办 13 -> 12 项。

---

## v3.10.4 - 2026-10-25 P0-3 graceful shutdown + P0 全部收口

**触发**: 主人问「接下来应该做什么」-> 盘点 P0 剩余项，发现 P0-2/P0-4 其实早已实现（只是清单没记账），P0-3 是真缺口。

### ✨ 新增

- **`graceful.py`** —— 统一优雅退出（信号处理）：
  `install_graceful_shutdown(cleanups, name)` 注册 SIGTERM/SIGINT；
  收尾逻辑全包 try/except（一个失败不阻断其余）；打印 `GRACEFUL_SHUTDOWN` 标记便于日志断言；
  重复信号幂等（第二次直接 exit 130 强退）。
- 接入 3 个长驻进程：
  `controller_app.py`（落盘 state + 关 DB 连接）、`live_env.py`（停 Mininet 拓扑）、`assurance_mvp.py`（关 DB 连接）。
- **`test_graceful.py`** —— 4 个用例：注册 handler / cleanup 按序执行 / 异常不阻断后续 / 重复信号强退。

### 🐛 Bug 修复

- **`graceful.py` force-exit 后缺 `return`**：第二次信号走 `os._exit(130)` 后仍会继续往下跑。
  真实场景 os._exit 不返回所以无实际影响，但被单元测试逮到，已修。
- **`test_graceful.py` 的 check() 假绿**：原本只记录 FAILURES 不 raise，导致 pytest 把失败当通过。
  已改为真的 raise AssertionError —— **测试能失败才有意义**。

### 📝 文档

- **`docs/OPTIMIZATION.md`**：v0.2 -> v0.3。
  P0-2 / P0-3 / P0-4 移入「✅ 已完成」段并补说明与提交号；
  **P0 全部完成**（5/5），待办 16 -> 13，已完成 4 -> 7。
- **P0-2 说明**：代码早在 `ca5abc9`(10-09) 就修好了（User-Agent + 绕代理 + 单连接复用 + 错误不吞），
  但清单第 5 步「验证 reporter.check() 6/6 = 200 ACTIVE」**仍待办** —— 需要 A 同学 cloudflared 隧道（当前 HTTP 000）。
- **P0-4 说明**：`_get_conn()` 早已是懒加载 + 探活重连 + 线程锁，不是清单写的「每次新建连接」。

### ✅ 实测

`kill -TERM` 打在写入周期中途（update_count=20, switches=1, hosts=4）：
- 日志见 `[GRACEFUL_SHUTDOWN] controller_app received SIGTERM, running 2 cleanup(s)...`
- 进程干净退出
- 退出后 `network_state.payload` 仍是完整 JSON（`payload_valid=true`）
- `state_history` 232 行，**空 payload 0 行**，孤儿行 **0**
- `assurance.consistency_checks` 536 行（pass=388 / violated=148），无半截写入

### 📊 统计

- 7 文件改动（2 新代码 + 3 改代码 + 2 文档）
- 单元测试 17 -> 21 个，全绿

---

## v3.10.3 - 2026-10-25 伙伴 assurance_schema.sql 落地准备 + 2 个 bug 修复

**触发**: 伙伴(A 同学)回传数据库对接文档的反馈，要求在 PG 实例上执行他的 `assurance_schema.sql`

### 🐛 Bug 修复

- **`controller_app.py` · DEAD 事件空指针崩溃**（demo 实测暴露）
  `state_change_handler` 里 `format(datapath.id, '016x')`，但 DEAD_DISPATCHER 状态下
  `datapath.id` 可能是 `None` → `TypeError: unsupported format string passed to NoneType`，
  整个 handler 挂掉。已加 `getattr(datapath, 'id', None)` 守卫 + 提前 return。
  —— 这个 bug 在仓库里从 v3.0 起就存在，是 16 天没跑才第一次真正触发。
- **`assurance_bridge.py` · 补 `ASSURANCE_BASE_FALLBACK`**
  P3-5 原计划有 fallback 备用地址（cloudflared 换域名时不掉连），实际没实现。
  现在优先级：显式参数 > `ASSURANCE_BASE` > `ASSURANCE_BASE_FALLBACK` > 空(no-op)。

### 🔒 新增：伙伴 schema 落地工具

- **`demo_scripts/apply_partner_schema.py`** —— 安全落地伙伴的 `assurance_schema.sql`：
  - 三道闸：危险语句拦截 / 重写后校验 / 冲突未确认拒绝落地
  - dry-run 在独立 schema + 独立事务里跑，结束必删，真库零影响
  - 自动输出冲突表清单 + **列级差异**（谁多哪些列、谁缺哪些列）
  - 已用模拟脚本实测：9 新表 + 2 硬冲突，检测准确

### 📝 文档

- **`docs/partner_schema_review.md`**（新建）——给伙伴的回复：
  密码确认、2 个硬冲突（`healing_intents` / `healing_history`）、4 组语义重叠、3 个待确认问题
- **`docs/OPTIMIZATION.md`**：v0.1 → v0.2。P0-1 / P0-5 / P1-4 / P3-5 实际早已完成但没记账，
  现在移入「✅ 已完成」段并补提交号；待办数 20 → 16，计数表同步更新。
- **`README.md`**：补「联调环境变量」表 —— `ASSURANCE_URL`（live_env/reporter 读）
  vs `ASSURANCE_BASE`（assurance_bridge 读）vs `ASSURANCE_BASE_FALLBACK` 三者关系与用途。

### 📊 统计

- 6 文件改动（2 代码 + 1 工具 + 3 文档）
- 修 2 个真 bug + 建 1 个安全落地工具 + 补 4 项记账 + 写 1 份对伙伴的回复

---

## v3.10.2 — 2026-10-25 状态复查 + 修 3 个问题

**触发**: 16 天没动（上次提交 10-09），主人问进度 → 全量复查 git / 文档 / 环境，发现问题

### 🐛 Bug 修复

- **`controller_app.py` · DEAD 事件空指针崩溃**（demo 实测暴露）
  `state_change_handler` 里 `format(datapath.id, '016x')`，但 DEAD_DISPATCHER 状态下
  `datapath.id` 可能是 `None` → `TypeError: unsupported format string passed to NoneType`,
  整个 handler 挂掉。已加 `getattr(datapath, 'id', None)` 守卫 + 提前 return。
  —— 这个 bug 在仓库里从 v3.0 起就存在，是 16 天没跑才第一次真正触发。
- **`assurance_bridge.py` · 补 `ASSURANCE_BASE_FALLBACK`**
  P3-5 原计划有 fallback 备用地址（cloudflared 换域名时不掉连），实际没实现。
  现在优先级：显式参数 > `ASSURANCE_BASE` > `ASSURANCE_BASE_FALLBACK` > 空(no-op)。

### 📝 文档更新

- **`docs/OPTIMIZATION.md`**：v0.1 → v0.2。P0-1 / P0-5 / P1-4 / P3-5 实际早已完成但没记账，
  现在移入「✅ 已完成」段并补提交号；待办数 20 → 16，计数表同步更新。
- **`README.md`**：补「联调环境变量」表 —— `ASSURANCE_URL`（live_env/reporter 读）
  vs `ASSURANCE_BASE`（assurance_bridge 读）vs `ASSURANCE_BASE_FALLBACK` 三者关系与用途。

### 📊 统计

- 4 文件改动（2 代码 + 2 文档）
- 修 2 个真 bug + 补 4 项记账 + 补 1 段环境变量文档

---

## v3.10 — 2026-10-09 文档整理（2026-10-09）：补全 30 天工作记录 + 清理过期描述

**触发**: 主人物理 workspace 后发现多个文档过期（CHANGELOG 只到 v3.0 / README 待做项过期 / STATE_MODEL 缺 v3.5+）

### ✨ 新增

- `memory/2026-10-09.md`（新建）今日完整对话 + intensure 状态盘点 + 下一步边界

### 📝 文档更新

- `CHANGELOG.md`：补 v3.5 / v3.6 / v3.7 / v3.8 / v3.9 五个版本
- `README.md`：完成状态表加 v3.0-v3.9；改过期'待做'为新'下一步'清单（划清边界）
- `STATE_MODEL.md`：加第 11 节'持久化层'（v3.5+ 数据库架构摘要 + 字段落库对应）
- `docs/our_tables_and_methods.md`：去掉'明天演示前 5 分钟'措辞，改为常驻参考 + 版本号 v0.2

### 📊 统计

- 4 文件改动 +195/-36 行
- 修复 5 个文档过期问题

---


---


---


## v3.9 — 2026-10-08 (07:13) 关掉心跳 + 简化文档

**主要作者**: 开发团队
**触发**: 清理开发阶段遗留物，恢复「自愈前后结果」可视化（v2.0 误删）

### ✨ 新增

- **「自愈前后对比」面板**（dashboard.html）
  - 位置：事件流面板下方，跨整行显示
  - 数据源：纯本地（不依赖保障模块）
    - `/api/events` 解析 `port_link_status` 事件，配对 down→up
    - `/api/history?key=connectivity` 取 ping 历史
  - 显示内容：每个故障周期的「故障前 ping」「故障中 ping」「恢复后 ping」
  - 持续时间 + 验证状态（已自愈/未恢复/异常）
- **live_env.py → /api/probe 自动上报**
  - 每个探测周期自动 POST connectivity 数据到 controller
  - 让 dashboard 的连通性/自愈对比面板有数据

### 🗑️ 移除

- **ActualState 样例面板**（dashboard.html）
  - 用途：联调 §4.2 字段对齐参考（开发阶段）
  - 删除：`ACTUAL_STATE_SAMPLES` 数组 + `renderActualStates()` 函数 + 4 秒循环
- **3 个 mock 样例卡片**（intentional 演示数据）

### 📝 文档修正

- **REQUIREMENTS.md §1**：同学标签全面纠正
  - 之前（错）：同学 A = 意图保障 / 同学 B = 意图实现 / 同学 C = 意图翻译
  - 现在（对）：**同学 A = 状态采集（本系统）**；同学 B/C = 意图智能体
  - 同步修正 11 处相关引用（意图保障模块、IP、Swagger、防火墙等）
- 添加「v2.0 重构时误删同学 A 标签」备注
- 添加"3 大职责完成度"诚实复盘

### 🐛 Bug 修复

- **Dashboard JS 错误**：renderBeforeAfter 用 `key=***` 应为 `key=connectivity`（typo 导致 connectivity 历史永远显示空）
- **state_server.py /api/probe**：清理调试 print，恢复干净输出

### 📊 验收指标

- Dashboard 顶部 badge：「意图接口不可达」彻底消失
- 4 个 legacy endpoints（/api/intents, /api/conflicts, /api/healing, /api/anomalies）仍 404
- 12 个保留 endpoints 全部 200
- 自愈前后对比面板成功渲染（基于真实 link events + ping history）

### 📁 文件变更

| 文件 | 变更 |
|---|---|
| `dashboard.html` | 1432 → 828 行 (-42%) |
| `state_server.py` | 551 → 208 行 (-62%) |
| `live_env.py` | +18 行（connectivity 自动上报） |
| `REQUIREMENTS.md` | §1 同学标签修正 + 11 处引用同步 |
| `CHANGELOG.md` | 新建（本文件） |

---


---


---


## v3.8 — 2026-10-08 (06:06) 一键演示脚本 + 汇报材料

**触发**: 演示准备

### ✨ 新增

- **`demo_scripts/demo_db_pipeline.sh`**（225 行）一键 E2E 演示
  - 8 步全绿(DEMO_AUTO=1):启动控制器 → Mininet → seed 策略 → 保障模块 → 健康基线 → 故障注入 → 恢复
  - 0 错误 / 23 成功
- **3 个汇报文档**（770 行）
  - `docs/presentation.md`（247 行）汇报大纲
  - `docs/speaker_notes.md`（290 行）逐句话术 + Q&A
  - `docs/demo_runbook.md`（215 行）演示照本宣科版

### 📊 配套产出

- 汇报材料 4 个文件 977 行
- v3.7 Phase 1 MVP 完整闭环

---


---


---


## v3.7 — 2026-10-08 Phase 1.2-1.4 保障模块 MVP + seed + E2E 演示

**主要**: 保障模块 + E2E 演示

### ✨ 新增

- **`assurance_mvp.py`**（480 行）保障模块 MVP
  - 8 状态有限状态机(REGISTERED → ACTIVE → VIOLATED → DIAGNOSING → HEALING → VERIFYING → RECOVERED / BLOCKED)
  - 一致性检查 + 根因诊断 + 5 重保护自愈
- **`seed_policies.py`**（98 行）mock 2 条 ACCESS_CONTROL 策略
- **`db_writer.py`** 扩展:get_assurance_writer + query/query_one 读方法

### 🐛 修复

- 3 个 GRANT bug:translation_user / implementation_user / assurance_user 都缺自己 schema 的 USAGE
- intent_states.intent_id 加 UNIQUE 约束(让 ON CONFLICT 工作)

### 📊 验收

- 158 条 consistency_checks + 120 条 diagnoses
- confidence=0.85, affected={s1}, down_ports=['s1:eth1']
- 故障注入 → state=0/DOWN(intensure 真实捕获)

---


---


---


## v3.6 — 2026-10-08 Phase 1.1 intensure 选择性写库

**主要**: db_writer.py + controller 接入

### ✨ 新增

- **`db_writer.py`**（515 行）选择性写库核心
  - 12 个方法:10 写 + 2 读
  - 选择性写规则:30s 节流 + 变化才写 + 异常才写
  - 连接池(懒加载 + 自动重连)
- **`controller_app.py`** 新增:_stats_loop 5s 调 write_network_state + write_state_history;_log_event 写端口/链路事件
- **`live_env.py`** 新增:write_probe_with_decision(自动判断异常才写)

### 📊 验收

- 100 意图 1 年存储 ≈ 13 GB(对比全量 270 GB,节省 95%)
- 30s 节流 + summary 变化触发 on-change

---


---


---


## v3.5 — 2026-10-08 数据库架构 Phase 0 5 schema + 43 表 + 4 模块 GRANT + 保留策略

**主要**: 数据库架构落地

### ✨ 新增文档（2240 行 / 8 文件）

- **`docs/database-architecture.md`**（353 行）架构总览 + 决策记录
- **`docs/database-schema.sql`**（551 行）43 张表 DDL
- **`docs/access-control-grants.sql`**（117 行）4 模块 GRANT
- **`docs/access-control-matrix.md`**（233 行）权限矩阵
- **`docs/cross-module-api-contract.md`**（292 行）跨模块 API 契约
- **`docs/migration-strategy.md`**（309 行）迁移策略
- **`docs/retention-policy.md`**（250 行）数据保留策略
- **`db_maintenance/retention_cleanup.sql`**（135 行）保留清理 SQL

### 🎯 关键决策

- 部署位置:Colima VM 本地(主人指定)
- 范围:Phase 0 + 1,先跑通 MVP
- 重点:意图保障智能体(主人指定)
- 5 schema:intensure / translation / implementation / assurance / shared
- 43 张表,4 模块 GRANT 隔离

### 📊 关键约束落地

> 翻译模块可以查询有效规则,但不能因为生成了消解方案就把已部署规则删除。
>
> **实现**: GRANT 只给 translation_user 对 implementation.policies 的 SELECT 权限(无 INSERT/UPDATE/DELETE)。

---


---


---


## v2.0 — 2026-09-03 (02:50) 重构：与上游契约对齐

**触发**: 用户分享 `~/Desktop/网络智能体/采集模块联调说明.md`（同学 A 写）

### 核心变更

1. **职责重定义**：从"全包"变为"只采集 + 上报"
   - 删除：一致性校验、冲突检测、自愈回路、异常检测（其他同学负责）
   - 保留：Mininet 拓扑、状态采集、ActualState 组装、REST API、采集视角 Dashboard
2. **新增模块**：
   - `actual_state_builder.py`：内部 state → ActualState 格式转换
   - `reporter.py`：HTTP POST 客户端（指数退避重试 + 线程安全统计）
   - `integration_demo.py`：端到端联调 demo（real/mock/skip 三模式）
   - `mock_assurance_server.py`：本地 mock 保障模块（离线调试）
   - `live_env.py`：v2.0 重构（接受环境变量 + 周期上报）
3. **归档到 legacy/**：12 个旧模块（conflict_detector/healing_engine 等）
4. **测试**：21 + 24 单元测试全过
5. **文档**：REQUIREMENTS.md / INTEGRATION_PLAN.md / README.md v2.0 重写

---


---


---


## v1.0 — 2026-08-04 ~ 2026-09-02 初始开发

- Mininet + Colima + os-ken 环境搭建
- 自定义拓扑（simple/multi/mesh）
- 链路事件监听（LLDP + PortStatus）
- 故障注入（断链/时延/丢包/限速）
- 采集视角 Dashboard（单文件 SPA）
- 7 个 bug 修复（详见 `memory/2026-08-04.md` / `memory/2026-08-05.md`）

---



## 🎯 当前状态（v3.9）

| 同学 A 职责 | 状态 |
|------------|------|
| Mininet 场景搭建（拓扑 + 故障注入 5 类）| ✅ 100% |
| 网络状态采集（交换机/端口/流表/主机连接）| ✅ 100% |
| 实际状态模型（统一 JSON + 历史 + 接口）| ✅ 100% |
| 可视化（拓扑 + 链路 + 告警 + 自愈前后）| ✅ 100% |
| 数据库（5 schema + 43 表 + 选择性写库）| ✅ 100% |
| 一键 E2E 演示 + 汇报材料 | ✅ 100% |

**对外接口**：POST `/check` + `/verify`（v2.0）+ ActualState 主动推送（v3.6+）
**已联调**：A 同学 assurance-agent 0.4.0, register→check→diagnose 走通（2026-09-19）
**下一步**：三方联调真实数据完整闭环 + reporter.py 频率/DNS bug 修复

---

