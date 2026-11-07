# 采集层容器化（Docker）

把采集层（os-ken 控制器 + REST API）连同数据库、看板一起容器化，**一处起、一处停**。

---

## 一、设计取舍（先看这段，能省很多困惑）

**容器里不装 Mininet、不造拓扑。**

采集层的定位是「观测别人的网络」——拓扑要么在宿主的 Colima VM 里，要么在对方的机器上
（联调），要么是真实园区设备。把 Mininet 塞进容器要 `--privileged` + netns，
代价大，而且和这个定位冲突。

| 组件 | 在容器里？ | 在外面？ |
|---|---|---|
| os-ken 控制器（OpenFlow 6653） | ✅ | |
| REST API（8080） | ✅ | |
| PostgreSQL | ✅（compose 里） | |
| Dashboard（nginx） | ✅（compose 里） | |
| **Mininet / 真实交换机** | ❌ | ✅ 在宿主 VM / 对方机器 / 园区 |

---

## 二、起法

### 2.0 先说三个实测踩过的坑

1. **本机没有 compose 插件**（只有 `docker` CLI）→ 要么装 compose 插件，要么用下面的
   `docker run` 等价命令。
2. **宿主 VM 的 os-ken 控制器占着 6653、state_server 占着 8080** → 直接映射会
   `address already in use`。要么先 `bash stop_all.sh` 停掉 VM 那套，要么换端口
   （`-p 18081:8080 -p 16653:6653`）。
3. **构建时 pip 不继承 docker daemon 的代理** → 必须显式传 `--build-arg HTTP_PROXY=...`；
   而且**代理会把 PyPI 索引 JSON 传输截断**（pip 报 "Unterminated string"），
   所以 pip 走国内镜像直连更稳（见 2.1）。

### 2.1 构建镜像

```bash
cd ~/projects/intensure

# Colima 的 docker daemon 配了代理（/etc/docker/daemon.json → 192.168.5.2:1082），
# 所以拉基础镜像能走代理。但**构建过程里的 pip 不会自动继承 daemon 的代理**
# → 必须显式把代理传进去，否则 pip 会直连 PyPI 超时。
docker build \
  --build-arg HTTP_PROXY=http://192.168.5.2:1082 \
  --build-arg HTTPS_PROXY=http://192.168.5.2:1082 \
  --build-arg NO_PROXY=localhost,127.0.0.1,192.168.5.0/24 \
  -t intensure:local .
```

> 基础镜像与 PyPI 源都是 build-arg：
> `BASE_IMAGE`（默认 `python:3.12-slim`）、`PIP_INDEX_URL`（默认清华源，
> 因为代理会截断 PyPI 索引）。
>
> 实测：官方源经 daemon 代理拉基础镜像可以；国内镜像源（daocloud）manifest 能通但
> **blob CDN 503**，代理正常时用官方源更稳。

### 2.2 三服务一起起

```bash
docker compose up -d
docker compose logs -f intensure
```

| 服务 | 端口 | 说明 |
|---|---|---|
| `postgres` | 5432 | 初始化时自动建 6 schema / 4 角色 / 35 条 GRANT |
| `intensure` | 8080（API）、6653（OpenFlow） | 采集层 |
| `dashboard` | 18080 | 静态看板 + 反代采集层 API（同源，无跨域问题）|

> ⚠️ 本机**没装 compose 插件**（只有 `docker` CLI）→ 用 `docker-compose` 独立版，
> 或手工跑等价命令（**已实测跑通**）：
> ```bash
> docker network create intensure-net
>
> docker run -d --name intensure-pg --network intensure-net \
>   -e POSTGRES_DB=intensure_dev -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
>   -v "$PWD/docs/database-schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro" \
>   -v "$PWD/docs/access-control-grants.sql:/docker-entrypoint-initdb.d/02-grants.sql:ro" \
>   postgres:16
>
> # ⚠️ --network-alias intensure：nginx 反代要按这个名字找采集层
> docker run -d --name intensure-collector --network intensure-net --network-alias intensure \
>   -p 18081:8080 -p 16653:6653 --cap-add NET_ADMIN \
>   -e INTENSURE_DB_DSN="postgresql://intensure_user:intensure_dev_pwd@intensure-pg:5432/intensure_dev" \
>   -e ASSURANCE_DB_DSN="postgresql://assurance_user:assurance_dev_pwd@intensure-pg:5432/intensure_dev" \
>   intensure:local
>
> docker run -d --name intensure-dashboard --network intensure-net -p 18080:80 \
>   -v "$PWD/dashboard.html:/usr/share/nginx/html/dashboard.html:ro" \
>   -v "$PWD/docker/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
>   nginx:alpine
> ```

### 2.3 实测验证结果（2026-11-08）

| 检查项 | 结果 |
|---|---|
| postgres 初始化 | ✅ 5 个业务 schema + `intensure` 18 张表 + `assurance_user` **107 条授权**（与线上一致）|
| 采集层容器 | ✅ `healthy`（HEALTHCHECK 过）、`/api/health` → `version 3.22.0` |
| 控制器持续写库 | ✅ `intensure.network_state` 的 version 每 5s +1（实测 34 → 37 → 47）|
| 看板 | ✅ 首页 HTTP 200 |
| nginx 反代 | ✅ `/api/health`、`/api/state` 均 200（走容器网络内的 `intensure` 别名）|

> 💡 查"容器里的库有没有数据"时注意**时序**：控制器第一次写库在启动约 5 秒后，
> 刚起就查会看到 0 行 —— 那是还没写，不是写不进去。

### 2.4 把拓扑接进容器

容器里的控制器**等交换机来连**（OpenFlow 是反向连接模型），所以要起一个
**把控制器指向容器**的拓扑：

```bash
# ① 拿到容器在网络里的 IP
docker inspect intensure-collector -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'

# ② 在宿主 Colima VM 里起 Mininet，控制器指向容器 IP
colima ssh -- bash -lc 'cd /path/to/intensure && \
  sudo mn --topo single,4 --controller remote,ip=<容器IP>,port=6653 --switch ovsk,protocols=OpenFlow13'
```

> 容器 IP 在 Colima 的 docker 网络里，VM 能路由到 → 可以直接用。
> 如果拓扑在**对方机器**上：对方起 Mininet 时指向我们容器的可达地址
> （容器要有对外可达的端口映射，或走 host 网络 / 反向隧道）。

### 2.5 联调时记得加 `--no-assurance`

容器内**不会**起 `assurance_mvp.py`（本地 mock 保障模块）——
它写的表正是对方真模块要写的，联调时两个进程抢写会污染对方看到的记录。
详见 `docs/PARTNER_COORDINATION.md` §2.5。

---

## 三、联调地址与数据库

```bash
docker compose up -d
# 联调时把对方地址传进去（重启容器生效）
ASSURANCE_BASE=https://对方的联调测试地址 docker compose up -d
```

数据库连接串在 compose 里已经配好（容器内用服务名 `postgres`）：

| 用途 | DSN |
|---|---|
| 采集层写自己的表 | `postgresql://intensure_user:intensure_dev_pwd@postgres:5432/intensure_dev` |
| 读对方的表 | `postgresql://assurance_user:assurance_dev_pwd@postgres:5432/intensure_dev` |

---

## 四、常用命令

```bash
docker compose ps                      # 看状态
docker compose logs -f intensure       # 采集层日志
docker compose logs -f dashboard       # 看板日志
docker compose exec postgres psql -U postgres -d intensure_dev   # 进库
docker compose down                    # 停（保留数据卷）
docker compose down -v                 # 停并删数据卷（重置）
```

容器内日志落点：`/var/log/intensure/controller.log`、`/var/log/intensure/state_server.log`。

---

## 五、已知限制

1. **容器内没有 Mininet** → `demo_db_pipeline.sh`（纯本地闭环）在容器里跑不了，
   它需要在本机/VM 里跑。容器只承载"采集层"这一段。
2. **`start_all.sh` 不适用于容器** → 它依赖 tmux + Mininet，容器里用
   `docker/start-services.sh`。
3. **OVS 日志采集在容器里拿不到** → 读的是宿主/远端设备上的
   `/var/log/openvswitch/ovs-vswitchd.log`，容器内没有那个文件。
   远端设备场景走 `target.type=ssh`（`ovs_logger` 已支持远端 tail）。
4. **状态文件在容器内 `/tmp`** → 控制器与 API 同容器，共用一个 `/tmp/network_state.json`，
   没问题；但**容器重建会丢状态**（内存态本来也会丢）。
