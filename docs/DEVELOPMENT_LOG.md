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
| 提交总数 | 55 个（含本文档 commit） | `git log --oneline \| wc -l` |
| 数据库 | **6 schema / 32 张逻辑表 / 21 个月分区** | `docs/database-schema.sql` 逐表比对，与现库完全一致 |
| 对外接口方向 | **只 POST 不读 GET** | 见 §6 边界 |

---

## 2. 开发进度时间线

> 只列「做成了什么」，每条都能在代码里验证到。

| 版本 | 日期 | 做了什么 |
|---|---|---|
| v1.0 ~ v2.0 | 08-04 ~ 09-03 | 初始开发 → 重构为「与上游契约对齐」。**这两版只在 `CHANGELOG.md` 里有，git 仓库是从 v3.0 开始的**，所以 git log 里查不到 |
| v3.0 | 09-03 | 范围收敛为「采集 + 上报」；dashboard 自愈前后对比面板 |
| v3.1 ~ v3.2 | 09-03/04 | 清理作者署名（改团队中性表述）；加 `.gitignore`、移出 `__pycache__` |
| v3.3 ~ v3.4 | 10-08 | 端口标签 `s1:eth2` + OVS 详细日志接入 + `safe_ping` 防死锁；联调脚本与文档（`demo_scripts` + 联调手册 + 项目汇报） |
| v3.5 | 10-08 | 数据库架构 Phase 0：6 schema + 分区 + GRANT + 保留策略 |
| v3.6 | 10-08 | `db_writer.py` 选择性写库（异常才写、变化才写、30s 节流） |
| v3.7 | 10-08 | `assurance_mvp.py`：一致性检查 + 根因诊断 + `intent_states` 状态轨迹 |
| v3.8 ~ v3.9 | 10-08 | 一键 E2E 演示脚本；心跳默认关（生产才开） |
| v3.10 ~ v3.10.1 | 10-09 | 补全 30 天工作记录、清理过期描述；修测试暴露的小问题 |
| P0-1 / P0-5 / P1-4 / P3-5 | 10-09 | **基础工程一批**：`requirements.txt` + `requirements-dev.txt`、**GitHub Actions CI workflow**、修 `our_tables_and_methods` 描述矛盾、移除 `assurance_bridge.py` 里硬编码的 cloudflared URL |
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
宿主机：macOS + Colima（/usr/local/bin/colima —— **实测在默认 PATH 里**，干净 login shell 也能找到；
      极少数工具环境里找不到时再 `export PATH="/usr/local/bin:$PATH"` 兜底）
VM：Ubuntu 24.04 / Python 3.12.3 / pytest 9.1.1 / ruff 0.6.9   ← 权威测试环境
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

### 3.4 演示时按什么顺序敲哪条命令

> **完整版在 `demo_scripts/README.md` §「演示流程全景」**（含状态机轨迹 + 每步产物）。
> 下表是速查版 —— 命令在 VM 里的 `/home/sunxiaoxuan.guest/intensure` 下执行。

| 顺序 | 命令 | 干什么 | 关键产物 |
|---|---|---|---|
| 0 | `bash start_all.sh` | 起 4 组件（controller / api / mininet / assurance）| — |
| 1 | `bash status.sh` | 5 段体检（进程 / tmux / 端口 / DB / 汇总）| — |
| 2 | `bash demo_scripts/00_pre_check.sh` | 联调预检 5 项（VM / 隧道 / intensure / assurance-agent / Mininet）| `.pre_check.json` |
| 3 | `bash demo_scripts/01_register_intent.sh` | 注册 + activate 演示意图 | `expected_state` |
| 4 | `bash demo_scripts/02_collect_state.sh` | 采集 ActualState | `.actual_state.json` |
| 5 | `bash demo_scripts/03_check_baseline.sh` | 基线检查：推给 assurance → `ACTIVE` | 健康基线 |
| 6 | `bash demo_scripts/04_inject_fault.sh` | 注入故障（`link_down` / `high_latency` / `packet_loss` / `flow_missing`）| `.fault_state.json` |
| 7 | `bash demo_scripts/05_check_violated.sh` | 推送故障状态 → 触发 `VIOLATED` | `violations[]` |
| 8 | `bash demo_scripts/06_diagnose.sh` | 根因诊断 | `root_causes[]` |
| 9 | `bash demo_scripts/07_heal.sh` | 自愈（生成 healing_intent + guard_checks 评估）| healing_intent |
| 10 | `bash demo_scripts/08_verify_recovered.sh` | 验证恢复 → `RECOVERED` | healing_verdict |
| 11 | `bash demo_scripts/99_reset.sh` | 演示后清理（清环境状态 + 删临时文件）| — |

**两个一键版**（省事，但正式演示别用 —— 看不出每步在干什么）：

| 命令 | 用途 | 前提 |
|---|---|---|
| `bash demo_scripts/demo_all.sh` | 全链路 9 步一键（`--auto` 不暂停 / `--skip=00` 跳预检 / `--fault=<类型>` 指定故障）| 需要对方服务可达 |
| `bash demo_scripts/demo_db_pipeline.sh` | 数据库闭环 8 步（启动 → 基线 → 注入 → 验证 → 恢复 → 验证 → 清理）| **不需要对方**，纯本地，最适合演示前自检 |

> ⚠️ `demo_all.sh` 依赖对方的 assurance-agent 服务；对方隧道不通（当前状态）时，
> **用 `demo_db_pipeline.sh` 也能完整演示我们的采集 → 判定 → 诊断 → 自愈能力**。

---

## 4. 待办清单（2026-10-31）

### 4.1 🔴 演示前必做（阻塞级）

| 事项 | 归属 | 说明 |
|---|---|---|
| **跑通 1 次真实联调** | **外部阻塞** | 卡在保障模块的 cloudflared 隧道不通（HTTP 000）。路径/契约已对齐，只剩这一个变量 |
| ~~演示讲稿~~（15-20 分钟完整剧本） | ✅ **已完成** | `docs/speaker_notes.md`（299 行）：开场 30 秒 + 11 个分段带时间标注的逐句话术 + 汇报完成 30 秒 + 应急方案，**合计约 29 分钟**，已超过 15-20 分钟的目标 |
| Q&A 预案（15-20 个老师可能问的问题） | 🟡 **部分完成** | 素材其实成型了：`docs/speaker_notes.md` 有 **Q1–Q8 完整答案**（不是占位），`demo_scripts/README.md` 另有 5 个问题。**去重后约 10-13 题**，离 15-20 还差 3-7 题，且没有按「老师真会问什么」排过序 |

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
| `docs/architecture.png` 需手工重新生成 | 低 | 改 `docs/architecture.html` 后要重新截图 PNG（headless Chrome，`--window-size=1460,1060`）|
| **`39 表 / 15 个月分区` 这个旧数全库还有 6 处没改** | 中 | 本文已订正为 **32 张逻辑表 / 21 个月分区**（7 张分区父表 × 当前月+0..2 动态分区），但下面 4 个文件还写着旧数：`README.md`（3 处，L15/L43/L491）、`REQUIREMENTS.md`（1 处，L57）、`docs/architecture.html`（2 处，L121/L204）。**改 `architecture.html` 后要连带重截 PNG**。下次同步文档时一并清掉 |
| `docs/speaker_notes.md` 里有 2 条问答内容已过期 | 低 | Q4 说「自愈的触发和执行留给 Phase 2」（我们已有 `assurance_mvp` 自愈）、Q6 说「稳定时 60 秒写一次心跳」（心跳 v3.9 起默认关）。**讲稿本身是现成的，但这两条要改** |

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

1. `colima` 在 `/usr/local/bin/colima`，**默认 PATH 里就有**（实测干净 login shell 也能找到）；
   只有个别工具环境找不到时才需要 `export PATH="/usr/local/bin:$PATH"`
2. VM 里**没有 git** —— 版本管理一律在宿主机做
3. VM 的 `/etc/environment` 设了 `http_proxy` 但没设 `no_proxy` → 本地地址被劫持 →
   脚本里已加 `export no_proxy` + `curl --noproxy '*'`（v3.19.0 修的，别删）
4. **macOS 上没有 `python` 这个命令**（只有 `python3`）；`/usr/bin/python3` 是系统自带的 **3.9.6**，
   但 PATH 里 `python3` 优先命中 `/usr/local/bin/python3` = **3.14.8**。
   VM 里的 **3.12.3** 是权威环境；**Mac 上也能跑** —— 用 `~/tools/testenv/bin/pytest`（3.14.8）实测 196 例全绿。
   但 `pip install --user` 会被 PEP 668 拒（externally-managed-environment），**别在 Mac 上临时装包**
5. `eventlet.monkey_patch()` 必须在 `import os_ken` 之前 → ruff 的 **E402 不能开**
6. `rsync --delete` 会删掉 alembic 旧 revision → multiple heads

### 8.4 后加的坑（v3.18 ~ v3.19，README 踩坑清单里还没有）

7. **`tc` 的复合参数必须拆成独立 argv** —— `f'rate {x}mbit'` 会被 tc 当成未知参数
   （`unknown parameter "rate 1.0mbit"`），要写成 `['rate', f'{x}mbit']`（v3.19.0）
8. **`tc qdisc del` 在「本来就没 qdisc」时报错是正常状态** —— 清理逻辑必须幂等，
   不能把 `Cannot delete qdisc with handle of zero` 当失败（v3.19.0）
9. **别把 dict 当 list 迭代** —— `_all_switch_ports()` 返回 `{sw: [ports]}`，
   直接迭代拿到的是交换机名而不是端口，全量清理会只清掉 1 个假接口（v3.19.0）
10. **别在实例方法里硬编码类级调用** —— `FaultApi._all_switch_ports()` 会绕过实例覆盖，
    单测没法 mock；改用 `self.` 调用（v3.19.0 单测抓出来的设计问题）
11. **VM 的 `http_proxy` 会劫持本地 curl** —— `/etc/environment` 设了 `http_proxy` 却没设 `no_proxy`，
    连 `127.0.0.1:8080` 都走代理 → 返回 502 → 健康检查误判「服务没起来」→ 启动脚本提前退出。
    脚本里已加 `export no_proxy` + `curl --noproxy '*'`，**别删**（v3.19.0）
12. **启动脚本的就绪探测别太早** —— `sleep 3` 后就 curl，Flask 还没 ready 会误判；
    看到「服务没起来」先去看 `/tmp/state_server.log`，别急着改代码（v3.19.0）

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
| `docs/speaker_notes.md` | **汇报讲稿 + Q&A 素材**（约 29 分钟逐句话术 + Q1–Q8 完整答案，见 §4.1）|
| `docs/demo_runbook.md` / `demo_scripts/演示手册.md` | 演示现场怎么操作（8 分钟流程 + 应急方案）|
| `demo_scripts/README.md` | 9 个联调脚本的流程全景 + 演讲稿速查 + 老师可能问的 5 个问题 |
| `docs/DEVELOPMENT_LOG.md` | **本文**：进度、待办、交接 |
