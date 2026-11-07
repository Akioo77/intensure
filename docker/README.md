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

### 2.1 构建镜像

```bash
cd ~/projects/intensure

# 国内网络：Docker Hub 直连不通，用镜像源
docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim \
  -t intensure:local .
```

> `BASE_IMAGE` 是 build-arg，默认 `python:3.12-slim`。
> Colima 的 docker daemon 本身配了代理（`/etc/docker/daemon.json` → `192.168.5.2:1082`），
> 所以官方源在代理正常时也能直接拉；代理抖动时换镜像源更稳。

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
> 或手工跑等价命令：
> ```bash
> docker run -d --name intensure-pg --network intensure-net \
>   -e POSTGRES_DB=intensure_dev -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \
>   -v "$PWD/docs/database-schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro" \
>   -v "$PWD/docs/access-control-grants.sql:/docker-entrypoint-initdb.d/02-grants.sql:ro" \
>   postgres:16
>
> docker run -d --name intensure-collector --network intensure-net \
>   -p 8080:8080 -p 6653:6653 --cap-add NET_ADMIN \
>   -e INTENSURE_DB_DSN="postgresql://intensure_user:intensure_dev_pwd@intensure-pg:5432/intensure_dev" \
>   -e ASSURANCE_DB_DSN="postgresql://assurance_user:assurance_dev_pwd@intensure-pg:5432/intensure_dev" \
>   intensure:local
> ```

### 2.3 把拓扑接进容器

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

### 2.4 联调时记得加 `--no-assurance`

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
