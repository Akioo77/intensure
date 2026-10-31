# 开发进度与交接文档（intensure 采集模块）

> 最后更新：**2026-10-31**（对应代码版本 v3.20.0）
> 维护方式：每次做完一轮开发就更新「开发进度」与「待办清单」两节，**以代码实际状态为准**，不写没实现的东西。
> 本文回答三个问题：现在做到哪了 / 接下来做什么 / 别人怎么接手。

---

## 0. 一句话

intensure 是意图驱动网络管理系统的**采集层**（Mininet + OVS + os-ken），职责是采集网络真实状态并上报给**意图保障模块**。

**联调对象只有一个：意图保障模块的作者。** 翻译模块、实现模块与我们没有直接接口（详见 §5、§6）。

---

## 1. 当前状态速览（2026-10-31 实测）

| 指标 | 数值 | 怎么核的 |
|---|---|---|
| 代码版本 | v3.20.0 | `git describe --tags` |
| 自营 REST 端点 | **23 个** | `grep -c "^@app.route" state_server.py` |
| 其中跨模块契约端点 | 5 个 | `/api/intensure/v1/*`（v3.20.0 新增） |
| 单元测试 | **196 例全绿** | VM 内 `pytest` |
| CI 跑的测试文件 | **13 个** | `.github/workflows/ci.yml` |
| 代码风格 | `ruff check .` 全过 | 规则 E/W/F，E402 豁免（见 §8） |
| Python 代码量 | 12212 行（根目录 *.py） | `wc -l *.py` |
| 提交总数 | 54 个（v1.0 至今） | `git log --oneline \| wc -l` |
| 数据库 | 6 schema / 39 表 / 15 个月分区 | `docs/database-architecture.md` |
| 对外接口方向 | **只 POST 不读 GET** | 见 §6 边界 |

---

## 2. 开发进度时间线

> 只列「做成了什么」，每条都能在代码里验证到。

| 版本 | 日期 | 做了什么 |
|---|---|---|
| v3.0 | 09-03 | 范围收敛为「采集 + 上报」；dashboard 自愈前后对比面板 |
| v3.5 | 10-08 | 数据库架构 Phase 0：6 schema / 39 表 + 分区 + GRANT + 保留策略 |
| v3.6 | 10-08 | `db_writer.py` 选择性写库（异常才写、变化才写、30s 节流） |
| v3.7 | 10-08 | `assurance_mvp.py`：一致性检查 + 根因诊断 + `intent_states` 状态轨迹 |
| v3.8 ~ v3.9 | 10-08 | 一键 E2E 演示脚本；心跳默认关（生产才开） |
| v3.10.2 ~ v3.10.4 | 10-25 | 伙伴 `assurance_schema.sql` 落地准备；graceful shutdown |
| v3.11.x | 10-26 | 测试覆盖 +131 例；ruff 接入 CI 防退化；一键启动脚本 |
| v3.12.x | 10-26 | 落地伙伴 9 张无冲突保障表；诊断表写入开关（职责分离） |
| v3.13.x | 10-26/27 | 100 意图压测实测基线；修「新建库 ≠ 现有库」+ 分区清理致命 bug |
| v3.14.0 | 10-27 | alembic 迁移工具（改 schema 的唯一入口 + 基线漂移检查） |
| v3.15.0 | 10-28 | DB 自动重连（指数退避）；历史归档定时清理（systemd timer） |
| v3.16.0 | 10-28 | 日志统一：7 模块 75 处 print → logging |
| v3.17.0 | 10-28 | 复杂故障类型：burst / reorder / vlan_mismatch / flow race（9 种全覆盖） |
| **v3.18.0** | 10-29 | **可观测性**（`metrics.py` 纯标准库 Prometheus + `GET /metrics`）+ **契约版本协商**（6 态状态机，版本冲突自动降级不上报） |
| **v3.19.0** | 10-29 | **故障注入 REST 接口**（9 种故障 + 幂等清理，演示脚本接上真端点）+ 修 `http_proxy` 劫持导致健康检查假失败 |
| **v3.20.0** | 10-31 | **落地 `/api/intensure/v1/*` 跨模块契约端点**（5 个，旧路径全部保留不破坏现有联调） |

---

## 3. 现在能跑什么

### 3.1 环境（重要：代码在 macOS，跑在 VM 里）

```
宿主机：macOS + Colima（/usr/local/bin/colima，注意不在默认 PATH 里）
VM：Ubuntu 24.04 / Python 3.12.3 / pytest 9.1.1 / ruff 0.6.9
仓库（宿主机）：/Users/sunxiaoxuan/workspace/projects/intensure
仓库（VM 内） ：/home/sunxiaoxuan.guest/intensure
```

**同步代码到 VM**（改完代码必须做，否则 VM 跑的是旧代码）：

```bash
export PATH="/usr/local/bin:$PATH"
cd ~/workspace/projects/intensure
rsync -az --exclude '.git' --exclude '__pycache__' \
  -e "ssh -F /Users/sunxiaoxuan/.colima/ssh_config" ./ \
  colima:/home/sunxiaoxuan.guest/intensure/
```

> ⚠️ **不要用 `rsync --delete`**：alembic 的 `versions/` 目录删了会出 multiple heads（CHANGELOG 踩过的坑）。

### 3.2 跑测试 / lint（改动后必做）

```bash
export PATH="/usr/local/bin:$PATH"
colima ssh -- bash -lc 'cd /home/sunxiaoxuan.guest/intensure && ruff check . && \
  python3 -m pytest test_actual_state_builder.py test_reporter.py test_graceful.py \
  test_db_writer.py test_assurance_mvp.py test_state_server.py test_state_history.py \
  test_ovs_logger.py test_state_model.py test_metrics.py test_contract_version.py \
  test_fault_api.py test_contract_api.py -q'
```

> CI 跑的就是这 13 个文件（`.github/workflows/ci.yml` 里是显式列表，**新增测试文件必须手工加进去**，否则不会被执行——这坑踩过两次）。

### 3.3 演示 / 联调

```bash
colima start                                   # 开机后必做
colima ssh && cd /home/sunxiaoxuan.guest/intensure
bash start_all.sh                              # 一键起 4 组件（controller/api/mininet/assurance）
bash status.sh                                 # 5 段体检
bash stop_all.sh                               # 优雅停止（数据保留）
curl -s --noproxy '*' http://127.0.0.1:8080/metrics | head   # 看指标
```

---

## 4. 待办清单（2026-10-31）

### 4.1 🔴 演示前必做（阻塞级）

| 事项 | 归属 | 说明 |
|---|---|---|
| **跑通 1 次真实联调** | **外部阻塞** | 卡在保障模块的 cloudflared 隧道不通（HTTP 000）。路径/契约已对齐，只剩这一个变量 |
| 演示讲稿（15-20 分钟完整剧本） | 待做 | 含每步讲什么 / 看什么 / 解释什么 |
| Q&A 预案（15-20 个老师可能问的问题） | 待做 | `docs/speaker_notes.md` 有零散素材，未成体系 |

### 4.2 🟡 我们自己能做（不依赖外部）

| 事项 | 工作量 | 说明 |
|---|---|---|
| Dashboard 单文件 SPA 拆分 | 3-5 天 | 1600+ 行 HTML/JS/CSS 混在一起。**演示是加分项，拆错会伤演示**，建议演示后再做 |
| Docker 化 | 3 天 | `Dockerfile` + compose（intensure / postgres / dashboard 三服务） |
| 真实园区迁移路径文档 | 0.5 天 | 还没并入 `INTEGRATION_GUIDE.md` |
| Dashboard 加联调统计面板 | 1 天 | 上报成功率 / 响应时间 / ActualState 样例（数据源 `metrics.py` 已有） |
| collector 多协议适配器骨架 | 2-3 天 | SNMP / CLI / OpenFlow |
| OVS log → 根因归因 demo | 1 天 | `ovs_logger` 已能抓到「是谁下的 ofctl 命令」 |

### 4.3 🔵 已明确的边界（不做）

- **翻译模块 / 实现模块的接口** —— 不是我们的职责，也不跟那两位联调
- **`/api/shared/v1/*`** —— 公共模块的职责（架构图 §C 标注），我们不越界实现
- **冲突消解 / 自愈决策** —— v2.0 起明确交给其他模块

---

## 5. 联调对接清单（只和「意图保障模块」那位对）

对方要做的三件事，缺一不可：

1. **给可访问地址** —— cloudflared 域名或局域网 IP。
   拿到后：`export ASSURANCE_BASE=https://xxx.trycloudflare.com`
   （`ASSURANCE_BASE_FALLBACK` 存旧域名兜底，cloudflared 重启会换域名）

2. **按契约调我们的端点** —— 路径已在 v3.20.0 对齐：
   | 契约路径（对方调这个） | 旧路径（保留，现有链路在用） |
   |---|---|
   | `GET /api/intensure/v1/state/current` | `GET /api/state` |
   | `GET /api/intensure/v1/state/history` | `GET /api/history` |
   | `GET /api/intensure/v1/port-events` | `GET /api/port_stats`（部分语义） |
   | `GET /api/intensure/v1/link-events` | 无对应 |
   | `POST /api/intensure/v1/probe-results` | `POST /api/probe` |

   两套并存、指向同一份底层状态，**返回结构不同**（契约路径多一层转换：`switches` 是 list、带 `version` 乐观锁、带 `down_ports`/`down_links`）。

3. **共享库权限** —— `assurance_user` 密码 + 5 张 `assurance.*` 表的读写授权
   （我们已经替他建好了，历史核对文档在 `docs/partner_schema_*.md`）

### 5.1 ⚠️ 命名口径提醒（沟通时别用字母）

同一个仓库里，同一个人在不同文档里被叫成不同字母，联调时极易踩坑：

| 文档 | 口径 |
|---|---|
| `README.md`「与其他同学的接口」表 | 同学 A = 意图保障模块 |
| `REQUIREMENTS.md` §1（2026-09-03 专门修正过） | **同学 A = 状态采集（本系统自己）**，B/C 才是意图智能体 |
| `~/Desktop/网络智能体/采集模块_对接文档.md` | 目标读者 = 同学 B（翻译）、同学 C（保障） |
| `STATE_MODEL.md` 署名 | 作者：同学 A |

> **结论：联调沟通一律用模块名**（意图保障模块 / 意图翻译模块 / 意图实现模块），不要用 A/B/C 字母。
> 这个口径还没在仓库里统一，**待确认后修**（需要知道真实的对应关系）。

---

## 6. 我们的边界（避免越界，也避免被误问）

| 维度 | 我们做 | 我们不做 |
|---|---|---|
| HTTP | 只 POST（`/check`、`/verify`）+ 被动提供契约端点 | **不读 GET** 对方的状态——当前状态由保障模块的 `/record/{intent_id}` 管 |
| 数据库 | 写 `intensure.*`（5 张）+ `assurance.*`（5 张）+ `shared.*`（2 张）= 12 张 | 不写 `translation.*` / `implementation.*`；**联调/生产模式下不写 `assurance.diagnoses`**（`ASSURANCE_WRITE_DIAGNOSES=0`，诊断归保障模块，职责分离） |
| 决策 | 采集、探测、上报 | 一致性判定、根因诊断、自愈决策、冲突消解 |

> 老师可能问「你们为什么也建了 assurance 表？」——见 `docs/our_tables_and_methods.md` §4。

---

## 7. 已知问题 / 待清理（不影响当前交付）

| 项 | 严重度 | 说明 |
|---|---|---|
| `migrations/versions/0002_retention_function.py` 与生成器模板漂移 | 低 | 该文件被手工排版过，`gen_baseline.py` 重新生成会产生 diff（语义相同，只差换行）。CI 只校验 `0001_baseline.py` 所以没发现。**改 schema 时注意别把无关 diff 带进去** |
| CI 测试列表是显式枚举 | 中 | 新增测试文件必须手工加进 `ci.yml`，忘了就不会跑（已踩坑 2 次） |
| `dashboard.html` 单文件 1600+ 行 | 低 | 可维护性问题，见 §4.2 |
| `docs/architecture.png` 需手工重新生成 | 低 | 改 `docs/architecture.html` 后要重新截图 PNG |

---

## 8. 协作约定

### 8.1 分工

| 角色 | 负责 |
|---|---|
| **Mavis**（本会话） | 代码实现、测试与验证、版本管理、进度盘点 |
| **OpenClaw** | 文档同步（README / CHANGELOG / 契约文档 / 联调手册 / 架构图）、项目复盘、方案评审 |

> 交接方式：Mavis 给 OpenClaw 明确清单（改哪些文件、依据是什么、哪些数字以实测为准），OpenClaw 改完自行提交 docs commit。
> **注意**：OpenClaw 的回复偶尔为空，但**工作照做**——以 git log 为准，不要以回复内容为准。

### 8.2 版本管理

- 分支开发 → fast-forward 合入 master → 打 `vX.Y.Z` 注释标签 → 推 `origin`
- 一个功能一个 commit，保留可回溯粒度（当前每个 commit 都能单独 revert）
- 回滚点：每个标签的父提交就是上一版的回滚点，例如
  ```bash
  git reset --hard 06979d1     # 回到 v3.20.0 之前
  ```
- 提交前 pre-commit 会自动跑 `ruff --fix`；**E501 超长行会被挡住**（正常，直接改行）

### 8.3 环境陷阱清单

1. `colima` 不在默认 PATH —— 要 `export PATH="/usr/local/bin:$PATH"`
2. VM 里**没有 git** —— 版本管理一律在宿主机做
3. VM 的 `/etc/environment` 设了 `http_proxy` 但没设 `no_proxy` → 本地地址被劫持 →
   脚本里已加 `export no_proxy` + `curl --noproxy '*'`（v3.19.0 修的，别删）
4. macOS 自带 python 是 3.9，**项目要 3.12** —— 一律在 VM 里跑测试
5. `eventlet.monkey_patch()` 必须在 `import os_ken` 之前 → ruff 的 **E402 不能开**
6. `rsync --delete` 会删掉 alembic 旧 revision → multiple heads

---

## 9. 文档地图

| 文档 | 什么时候看 |
|---|---|
| `README.md` | 日常怎么跑、对外接口、与其他模块的关系 |
| `CHANGELOG.md` | 每个版本做了什么（v1.0 ~ v3.20，按版本号降序） |
| `REQUIREMENTS.md` | 职责划分、模块边界、测试清单 |
| `docs/cross-module-api-contract.md` | **跨模块契约**（§4.1/§5.1 端点定义、§5.6 契约 vs 实现映射、§5.7 版本协商） |
| `docs/architecture.html` / `.png` | 一张图看懂三个智能体怎么连、缺什么 |
| `docs/OPTIMIZATION.md` | 优化清单：什么已完成、什么待做 |
| `docs/our_tables_and_methods.md` | 我们写哪些表、读不读别人的表 |
| `docs/ALEMBIC_WORKFLOW.md` | 改 schema 的唯一正确流程 |
| `docs/demo_runbook.md` / `demo_scripts/演示手册.md` | 演示现场怎么操作 |
| `docs/speaker_notes.md` | 汇报问答素材（尚未成体系，见 §4.1） |
| `docs/DEVELOPMENT_LOG.md` | **本文**：进度、待办、交接 |
