# 数据库交接文档 —— 对方（意图保障模块）如何接入

> **本文写给谁**：对接「意图保障模块」那位同学 + 后续接手的同学。
> **一句话**：数据库**完全在我方本地**（Colima VM 里的 PostgreSQL 16），
> **本轮联调不需要接库**（HTTP 对接、各自持久化）；
> 本文说明的是「后续真要接库时怎么做」，**命令全部实测过**。
>
> 最后实测：2026-11-10 ｜ 版本：v1.0

### 实测记录（2026-11-10，本文每条命令都跑过）

| 检查项 | 结果 |
|---|---|
| SSH 隧道 `-L 15432:127.0.0.1:5432` | ✅ 建起来后外部主机能连到隧道端口（PG 回了 134 字节启动包）|
| 认证方式 | ✅ `AuthenticationSASL` → **SCRAM-SHA-256**（与 `pg_hba.conf` 一致）|
| `assurance_user` 连库 | ✅ `assurance_user \| intensure_dev` |
| 读我方观测 | ✅ `network_state` 1 行 / `port_events` 102 行 / `state_history` 52932 行 |
| 写 `assurance.consistency_checks` | ✅ 写入成功（id=21240），清理后剩余 0 行 |
| 越权：UPDATE `intensure.network_state` | ✅ `permission denied for table network_state` |
| 越权：UPDATE / DELETE `shared.audit_log` | ✅ `permission denied for table audit_log`（审计不可改不可删）|

> ⚠️ 踩坑记录：`consistency_checks` 的三个 NOT NULL 列是
> `expected_state` / `actual_state` / `result` —— 漏了后两个会报
> `not-null violation`，那个错**看着像权限问题其实不是**，容易误判。

---

## 0. 先看这个：本轮联调**不需要数据库连接**

2026-11-10 对方答复已确认：

> 这轮先保持 HTTP 对接、各自持久化，共享数据库及指定表落库暂不接入。

所以本轮联调时**对方不需要** DSN、不需要密码、不需要网络打通。数据流是：

```
我方采集层  ──POST /api/v1/assurance/check──►  对方保障模块
              ActualState = {
                reachable, latency_ms, packet_loss,
                expected_flow_present,
                down_links:  ["s1:eth2-s2:eth3"],
                down_ports:  ["s1:eth2"]
              }
              ──POST /api/v1/assurance/verify──►
```

对方**只收 HTTP**，我们**只 POST 不读 GET** 对方状态，各自写各自的库。

---

## 1. 数据库现状（实测）

### 1.1 在哪：完全本地，无任何云端成分

```
Colima VM（192.168.5.1，Ubuntu 24.04）
└── PostgreSQL 16.15
    ├── 数据目录   /var/lib/postgresql/16/main
    ├── 配置文件   /etc/postgresql/16/main/postgresql.conf
    ├── 监听       127.0.0.1:5432   ← 只听回环，外部机器连不上
    ├── 库         intensure_dev
    │                6 schema / 26 张逻辑表（物理表 47，分区子表 21）
    └── 业务账号   intensure_user / assurance_user
                   implementation_user / translation_user
```

**没有云数据库、没有 RDS、没有托管实例** —— 数据文件就在这台 Mac 的 Colima VM 磁盘上。

> ⚠️ **这是开发环境，不是生产架构**。正式环境这套不能直接用：
> 数据在个人开发机磁盘上 → 机器关机 / VM 重建 / 换人就丢。
> 迁移到真实园区时数据库要换成对方提供的实例，**代码不用改**（DSN 是环境变量）。

### 1.2 连接配置

我方代码里 DSN 都是**环境变量优先**，不用改代码就能切：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `INTENSURE_DB_DSN` | `postgresql://intensure_user:intensure_dev_pwd@127.0.0.1:5432/intensure_dev` | 采集层写 `intensure.*` |
| `ASSURANCE_DB_DSN` | `postgresql://assurance_user:assurance_dev_pwd@127.0.0.1:5432/intensure_dev` | 我方 mock 保障模块写 `assurance.*` |

> ⚠️ `ASSURANCE_DB_DSN` 是**我方 mock**（`assurance_mvp.py`）用的。
> 联调时 mock 要关（见 §4.1），**不要**把对方的库配到这个变量上。

### 1.3 认证方式

`/etc/postgresql/16/main/pg_hba.conf` 生效规则：

```
local   all             postgres             peer
local   all             all                  peer
host    all             all   127.0.0.1/32   scram-sha-256
host    all             all   ::1/128        scram-sha-256
```

- **TCP 连接一律 `scram-sha-256`**（密码认证），走 unix socket 才是 peer
- 目前只放行 `127.0.0.1` 与 `::1` → **外部来源被拒**

---

## 2. 对方的账号权限（**已就位，实测通过**）

`assurance_user` 现有权限（2026-11-10 用该账号实测连库 + 写库成功）。

> 授权规模：`assurance` schema 内 **80 条**、全库共 **107 条**（2026-11-10 实测）。

| 表 | 权限 | 说明 |
|---|---|---|
| `assurance.consistency_checks` | SELECT / INSERT / UPDATE / DELETE | 对方写一致性判定结果 |
| `assurance.diagnoses` | SELECT / INSERT / UPDATE / DELETE | 对方写根因诊断 |
| `assurance.healing_intents` | SELECT / INSERT / UPDATE / DELETE | 对方写自愈决策 |
| `assurance.intent_states` | SELECT / INSERT / UPDATE / DELETE | 对方写状态轨迹 |
| `assurance.violation_events` | SELECT / INSERT / UPDATE / DELETE | 违例事件 |
| `assurance.healing_history` | SELECT / INSERT / UPDATE / DELETE | 自愈历史 |
| `intensure.network_state` | SELECT | 对方读我方当前状态 |
| `intensure.state_history` (+分区) | SELECT | 对方读我方历史 |
| `intensure.port_events` (+分区) | SELECT | 对方读我方端口事件 |
| `intensure.probe_results` (+分区) | SELECT | 对方读我方探测结果 |
| `intensure.flows_snapshot` | SELECT | 对方读我方流表快照 |
| `intensure.link_events` (+分区) | SELECT | 对方读我方链路事件 |
| `implementation.policies` / `.deployments` | SELECT | 对方读策略与部署记录 |
| `translation.intent_templates` | SELECT | 对方读意图模板 |
| `shared.audit_log` | **INSERT + SELECT** | append-only（权限层成立）|
| `shared.module_health` | INSERT / SELECT / UPDATE | 双方写心跳 |
| `shared.schema_version` | SELECT | 只读 |

**关键隔离点**（别破坏）：

- `assurance_user` 对我方 `intensure.*` **只读** → 不能篡改我方观测数据
- `translation_user` 对 `assurance.diagnoses` **只有 SELECT** → 防误删
- `shared.audit_log` **不给 UPDATE / DELETE** → 审计追加语义在权限层成立

权限来源：`docs/access-control-grants.sql`（幂等，可重复执行）。
矩阵说明见 `docs/access-control-matrix.md`。

---

## 3. 后续接库：三步（**推荐走 SSH 隧道**）

### 3.1 为什么推荐隧道，而不是直接开端口

把 PG 直接暴露到网络上，等于把「一个能被 SQL 注入打到的东西」放到公网。
隧道的好处：

- ✅ **不用改 PG 配置**（`listen_addresses` 保持 `127.0.0.1`）
- ✅ **不用开防火墙口子**、不用改 `pg_hba.conf`
- ✅ 隧道断了库立刻不可达（可控）
- ✅ 与对方现在用 cloudflared 隧道是同一个思路，认知一致

### 3.2 第一步：我方开隧道（对方机器上执行）

对方拿到我方 SSH 访问权后，在**对方那台机器**上执行：

```bash
ssh -N -L 15432:127.0.0.1:5432 <我方用户>@<我方可达地址>
# 保持这个进程活着 → 对方的 127.0.0.1:15432 就是我方库
```

我方 VM 内 SSH 监听 `0.0.0.0:22`（实测），隧道可行。

> 端口 `15432` 是我方数据库在对方视角的端口，**不要用 5432**（避免和对方自己的 PG 撞）。

### 3.3 第二步：换掉开发密码

⚠️ **现在的密码是明文写在代码和 SQL 里的开发默认值**（`assurance_dev_pwd`），
不能直接用到联调环境。对方拿到后应立即换成随机值：

```sql
-- 以 postgres 身份执行（我方 VM 内）
ALTER ROLE assurance_user WITH PASSWORD '<随机长串>';
```

生成随机密码：

```bash
openssl rand -base64 24
```

**给对方的连接串**（密码占位，由我方单独给）：

```
postgresql://assurance_user:<新密码>@127.0.0.1:15432/intensure_dev
```

> ⚠️ 改完密码后，**我方也要同步**：
> `db_writer.py` 里的 `DEFAULT_DSN_ASSURANCE` 是开发默认值（只影响 mock），
> 真实环境用 `ASSURANCE_DB_DSN` 环境变量注入，**不要**把密码提交进代码库。

### 3.4 第三步：对方验证接入

对方在**自己机器**上执行（隧道已建好）：

```bash
# ① 连得上、认证通过
PGPASSWORD='<新密码>' psql -h 127.0.0.1 -p 15432 -U assurance_user -d intensure_dev \
  -c "SELECT current_user, current_database(), now();"
# 期望：assurance_user | intensure_dev | <当前时间>

# ② 能读我方观测数据
PGPASSWORD='<新密码>' psql -h 127.0.0.1 -p 15432 -U assurance_user -d intensure_dev \
  -c "SELECT count(*) FROM intensure.network_state;"

# ③ 能写自己的判定结果（写入一张**只有对方能写**的表）
#    ⚠️ consistency_checks 有三个 NOT NULL 列：expected_state / actual_state / result
#       漏任何一个都会报 not-null violation（那是业务约束，不是权限问题）
PGPASSWORD='<新密码>' psql -h 127.0.0.1 -p 15432 -U assurance_user -d intensure_dev \
  -c "INSERT INTO assurance.consistency_checks(intent_id, expected_state, actual_state, result)
      VALUES ('conn-test', '{}'::jsonb, '{}'::jsonb, 'pass')
      RETURNING id;"
# 期望：返回一行 id

# ④ 清理测试数据
PGPASSWORD='<新密码>' psql -h 127.0.0.1 -p 15432 -U assurance_user -d intensure_dev \
  -c "DELETE FROM assurance.consistency_checks WHERE intent_id='conn-test';"
```

> 💡 字段名以实际 schema 为准 —— `docs/database-schema.sql` 是权威定义。
> 上面的 `expected_state` / `actual_state` / `result` 三个都是 NOT NULL 列。
> 查字段：`psql -c "\d assurance.consistency_checks"`

> 💡 **对方机器没装 psql 怎么办**（不一定要装）：
> 用 Python 也一样（`pip install psycopg2-binary`，或对方项目本来就依赖它）：
>
> ```python
> import psycopg2
> conn = psycopg2.connect(
>     host="127.0.0.1", port=15432,
>     user="assurance_user", password="<新密码>",
>     dbname="intensure_dev")
> cur = conn.cursor()
> cur.execute("SELECT current_user, current_database();")
> print(cur.fetchone())
> ```

### 3.5 越权自检（**可选但建议做一次**）

确认隔离没破 —— 对方账号**不应该**能改我方的观测表：

```bash
# 期望：ERROR: permission denied for table network_state
PGPASSWORD='<新密码>' psql -h 127.0.0.1 -p 15432 -U assurance_user -d intensure_dev \
  -c "UPDATE intensure.network_state SET version = 0;"

# 期望：ERROR: permission denied for table audit_log  ← 审计不可改
PGPASSWORD='<新密码>' psql -h 127.0.0.1 -p 15432 -U assurance_user -d intensure_dev \
  -c "UPDATE shared.audit_log SET action = 'x';"

# 期望：ERROR: permission denied for table audit_log  ← 审计不可删
PGPASSWORD='<新密码>' psql -h 127.0.0.1 -p 15432 -U assurance_user -d intensure_dev \
  -c "DELETE FROM shared.audit_log;"
```

> 以上三条在 2026-11-10 实测过，**全部正确报 permission denied**。

---

## 4. 联调时的注意事项

### 4.1 ⚠️ 必须关掉我方保障 mock

```bash
bash start_all.sh --no-assurance
```

我方自带的 `assurance_mvp.py`（本地 mock 保障模块）写的**正是对方要写的表**
（`consistency_checks` / `diagnoses` / `intent_states`）。
不关掉的话两个进程抢写同一张表，对方会看到我们的假记录、假状态机轨迹。

表里现在还有 **21241 行**是我方 mock 写的（`intent_id` 形如 `live-AC-demo-001/002`）。
表里**没有 source 字段能区分谁写的**，只能按 intent_id 区分：

```sql
-- 只删我们自己 mock 写的那几行（对方的数据不动）
DELETE FROM assurance.consistency_checks WHERE intent_id LIKE 'live-AC-demo-%';
DELETE FROM assurance.intent_states    WHERE intent_id LIKE 'live-AC-demo-%';
DELETE FROM assurance.diagnoses       WHERE intent_id LIKE 'live-AC-demo-%';
```

### 4.2 对方要调我方端点时，用契约路径

| 契约路径（对方调这个） | 旧路径（保留，现有链路在用） |
|---|---|
| `GET /api/intensure/v1/state/current` | `GET /api/state` |
| `GET /api/intensure/v1/state/history` | `GET /api/history` |
| `GET /api/intensure/v1/port-events` | `GET /api/port_stats`（部分语义）|
| `GET /api/intensure/v1/link-events` | 无对应 |
| `POST /api/intensure/v1/probe-results` | `POST /api/probe` |

两套并存、指向同一份底层状态，**返回结构不同**（契约路径多一层转换：
`switches` 是 list、带 `version` 乐观锁、带 `down_ports`/`down_links`）。

### 4.3 端口名格式

`down_ports` 用 `"s1:eth2"`、`down_links` 用 `"s1:eth2-s2:eth3"`（**不是** dpid / port_no 数字）。

真实设备上端口名是 `GigabitEthernet0/1` 这种长名时，
通过 `env.json` 的 `port_label_map` 映射成短标签：

```json
{
  "port_label_map": {"GigabitEthernet0/1": "eth1"},
  "dpid_to_switch": {"0000000000000001": "s1"}
}
```

---

## 5. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `connection refused` | 隧道没起 / 端口不对 | 确认 `ssh -N -L` 进程还活着；确认端口是 15432 不是 5432 |
| `no pg_hba.conf entry ... no pg_hba.conf entry for host` | 来源 IP 不在放行列表 | 走隧道时不该出现；直连则需加 `pg_hba.conf` 规则并 reload |
| `password authentication failed` | 密码还是旧的，或大小写/特殊字符被 shell 吃掉 | 用 `PGPASSWORD='...'` 单引号；确认已 `ALTER ROLE` |
| `permission denied for table X` | 权限缺失 | 查 `docs/access-control-grants.sql`，重跑（幂等）|
| `null value in column "expected_state"` | **不是权限问题** | 是业务字段 NOT NULL 约束 —— 写入要带必填字段 |
| 对方看到 `live-AC-demo-*` 的记录 | 我方 mock 写的 | 见 §4.1 清理 SQL |
| 连上了但读不到数据 | 我方采集层没在写 | 查我方 `GET /api/health` 与 `state_history` 行数 |
| 隧道关了端口还在监听 | 转发挂在**复用的 SSH 连接**上 | 用 `ssh -O exit -L 15432:127.0.0.1:5432 <host>` 单独关转发；**不要 kill 那个 ssh 进程**（可能是复用的主连接，杀了会连累其他会话）|

---

## 6. 相关文档

| 文档 | 内容 |
|---|---|
| `docs/PARTNER_COORDINATION.md` | 给对方的完整协作说明（接口形态、口径澄清、职责边界）|
| `docs/access-control-matrix.md` | 访问控制矩阵（为什么这么授权）|
| `docs/access-control-grants.sql` | GRANT 脚本（幂等，授权的唯一入口）|
| `docs/database-schema.sql` | 建表规范（字段级权威定义）|
| `docs/database-architecture.md` | 架构说明（6 schema / 分区策略）|
| `docs/retention-policy.md` | 保留策略（分区清理）|
| `docs/DEVELOPMENT_LOG.md` | 开发进度 / 环境坑 / 协作约定 |

---

## 7. 一句话总结（可直接转给对方）

> 数据库在我方本地（不是云端），目前只监听 `127.0.0.1:5432`，外部连不上。
> **本轮联调不用接库**，保持 HTTP 对接即可 —— 你们的账号权限（`assurance_user`）
> 已经配好并实测可读我方观测、可写自己的判定表。
> 后续真要接库时：你们在自己机器上开 SSH 隧道（`ssh -N -L 15432:127.0.0.1:5432 ...`），
> 我方给你换掉开发用的默认密码，你们用
> `postgresql://assurance_user:<新密码>@127.0.0.1:15432/intensure_dev` 连即可。
> 联调时请我方关掉本地 mock（`start_all.sh --no-assurance`），避免两边抢写同一张表。
