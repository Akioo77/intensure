# intensure 优化清单(2026-10-09 整理)

> **目的**:盘点 intensure 项目当前可改进的地方,按优先级排好,标注**做什么 + 怎么开发 + 工作量**。
> **状态**:v1.0(2026-10-28 更新)——**P0 与 P1 全部完成**；P2-1 压测、P2-4 重连、P2-5 归档、P3-1 alembic 也已完成。共 15 项已完成，剩余 5 项待办。
> **边界**:**只关心 intensure 这边**——翻译组 / 冲突消解组 / A 同学 assurance-agent 的内部设计不归我们管。
> **不抢活**:我们能催/建议,但不能越界做。

---

## 🎯 优先级总览(5 项待办 / 15 项已完成)

| 优先级 | 项数 | 类别 |
|------|------|------|
| 🔴 P0(基础缺失,必须补) | **0** | 全部完成 ✅ |
| 🟡 P1(质量/可靠性) | 0 | 全部完成 ✅ |
| 🟢 P2(性能/扩展) | 2 | dashboard / 故障类型 |
| 🔵 P3(完善) | 3 | Docker / observability |

---

## 🔴 P0 · 必须补(基础)


---


---


---


---


---

## 🟡 P1 · 质量 / 可靠性


---


---


---


---


---

## 🟢 P2 · 性能 / 扩展


---

### P2-2 · dashboard 单文件 SPA 难维护
**问题**:`dashboard.html` 1600+ 行 HTML/JS/CSS 混在一起,改一个颜色要 grep 整个文件。

**怎么开发**:
1. 拆成 `dashboard/index.html` + `dashboard.js` + `dashboard.css`(3 文件)
2. 引入 Vue 3 单文件组件(CDN 加载,不构建)
3. 或引入 HTMX(更轻量,服务器端渲染为主)
4. 加历史回放面板(基于 `/api/history` 已有的 200 条)

**工作量**:3-5 天
**价值**:🟡 中(不是当务之急)

---

### P2-3 · 复杂故障类型扩展
**问题**:现在 `fault_injector.py` 支持断链 / 时延 / 丢包 / 限速 / 任意 IP link。**没有**:burst 流量、packet reorder、VLAN mismatch、流表 race condition。

**怎么开发**:
1. `fault_injector.py` 加新类:
   - `BurstFault(tc qdisc ... tbf burst)` 突发流量
   - `ReorderFault(tc qdisc ... reordering)` 包乱序
   - `VlanMismatchFault(ovs-ofctl mod-vlan)` VLAN 不匹配
   - `FlowRaceFault(同时发两条 ovs-ofctl 改流表)`
2. 给 assurance-agent 准备测试场景(它诊断器需要这些样本)

**工作量**:2-3 天
**价值**:🟡 中(联调需要)

---


---


---

## 🔵 P3 · 完善


---

### P3-2 · Docker 化
**问题**:部署 = 手动 colima + 手动 SSH + 手动 sudo。新机器复现要 30+ 分钟。

**怎么开发**:
1. `Dockerfile`(基于 ubuntu:24.04,装 python3.12 + os-ken + Mininet)
2. `docker-compose.yml`:`intensure` + `postgres` + `dashboard` 三个服务
3. CI 验证:`docker build .` + `docker run smoke_test`

**工作量**:3 天
**价值**:🟡 中(部署效率)

---

### P3-3 · 跨模块 API 版本管理
**问题**:assurance-agent 接口没 version 字段(`/api/v1/assurance/...`),如果他升级到 v2,我们 intensure 这边**没人通知就坏了**。

**怎么开发**:
1. `assurance_bridge.py` 加 `API_VERSION = "0.4.0"` 常量
2. 启动时调 `/api/health` 拿他那边 version,断言一致
3. 不一致 → logger.error + 不上报(降级到本地缓存)

**工作量**:0.5 天
**价值**:🟡 中(防止被外部服务偷偷破坏)

---

### P3-4 · Metrics / Observability
**问题**:演示/生产没有任何 metrics——不知道每分钟多少 POST / 多少错误 / 平均延迟。

**怎么开发**:
1. 加 `prometheus_client`:`Counter('intensure_post_total')` / `Histogram('intensure_post_latency')`
2. 加 `/metrics` 端点
3. (可选)Grafana dashboard

**工作量**:2 天
**价值**:🟡 中(生产期重要)

---


---

## ✅ 已完成(从这份清单里移出来的项)

### ✅ P0-1 · `requirements.txt` 缺失

**完成情况**: requirements.txt (864B) + requirements-dev.txt (259B) 已建, README 补环境搭建段

**提交**: `3097b08`

**问题**:Python 依赖没记录在文件里(只有 VM 里隐式装过)。新人 clone 后没法复现环境,演示现场"我这边能跑你那边跑不起来"。

**价值**:🟢 高(解决"环境不一致"这个演示杀手)

---

### ✅ P0-5 · 没有 CI(GitHub Actions)

**完成情况**: .github/workflows/ci.yml 已建: py3.12 + 装依赖 + pytest + 语法检查

**提交**: `7870b26`

**问题**:今天我做完"最后测试一遍才 push"是**人工**做的。**没 push 前的自动化检查**——下一个开发者改了代码可能直接 push,语法错误 / 单元测试失败都不知道。

**价值**:🟢 高(以后所有改动自动验证)

---

### ✅ P1-4 · README "读 1 张别人表"描述错误

**完成情况**: README/STATE_MODEL 已改为「intensure 这边只写不读」, 并列出不读的表

**提交**: `501d0e0`

**问题**:README.md 当前状态表写"读 1 张别人的表 `implementation.policies`",但**实际我们只写不读**(2026-09-19 验证),这是**虚报职责**。

**价值**:🟡 中(诚实,避免被老师/搭档追问)

---

### ✅ P3-5 · A 同学 URL 硬编码

**完成情况**: assurance_bridge.py 硬编码已移除 → 改读 ASSURANCE_BASE 环境变量, 未配置则 no-op

**提交**: `a28bfe8`

**问题**:`assurance_bridge.py` 第 28 行 `DEFAULT_BASE = "https://westminster-accounts-permitted-pursuant.trycloudflare.com"`,A 同学重启 cloudflared 域名就变,我们 intensure 这边就挂。

**价值**:🟢 高(防止演示时挂)

---

### ✅ P0-2 · `reporter.py` 老 bug(频率 + DNS)

**完成情况**: `reporter.py` 已加 `DEFAULT_USER_AGENT = IntensureReporter/3.10` + `ProxyHandler({})` 强制直连；`live_env.py` 只建一个 `AssuranceReporter` 实例复用；connect 失败记 `error` 字段并 WARN 输出，不静默吞。⚠️ 清单第 5 步「验证 reporter.check() 6/6 = 200 ACTIVE」**仍待办** —— 需要 A 同学 cloudflared 隧道（当前 HTTP 000）。

**提交**: `ca5abc9（2026-10-09 已修，本次核实记账）`



---
### ✅ P0-3 · 没有 graceful shutdown(信号处理)

**完成情况**: 新增 `graceful.py`：`install_graceful_shutdown()` 统一注册 SIGTERM/SIGINT，收尾逻辑全包 try/except，打印 `GRACEFUL_SHUTDOWN` 标记，重复信号幂等强退。接入 3 个长驻进程：`controller_app.py`（落盘 + 关 DB）、`live_env.py`（停 Mininet）、`assurance_mvp.py`（关 DB）。✅ 实测：`kill -TERM` → 日志见 GRACEFUL_SHUTDOWN → 进程干净退出 → DB payload 完整、232 行 state_history 无空 payload、孤儿行 0。

**提交**: `待提交（2026-10-25）`



---
### ✅ P0-4 · 没有连接池(每次 `_get_conn` 都新建连接)

**完成情况**: `db_writer._get_conn()` 已是懒加载 + 自动重连：复用 `self._conn`，每次先 `SELECT 1` 探活，异常则重连；带 `self._conn_lock` 线程锁。**不是**清单描述的「每次新建连接」。

**提交**: `（早已实现，本次核实记账）`



---

### ✅ P1-1 · 测试覆盖率极低(8/9 关键模块 0 测试)

**完成情况**: 新增 6 个测试文件、131 个用例（host）/ 142 个（VM，含真实库 + Flask）：
  - `test_db_writer.py`（35）—— 12 个 write_* 的成功/失败路径、连接懒加载与探活重连、
    state_history 30s 节流与变化触发、should_write_probe 四种 anomaly_type、
    should_write_consistency 五种判定；含真实库集成（upsert 幂等 + 节流落库）
  - `test_assurance_mvp.py`（24）—— 3 个 extract 边界 + 状态机 7 种转换 + 4 种诊断规则
  - `test_state_server.py`（11）—— 11 个 REST 端点状态码/JSON 格式/参数过滤
  - `test_state_history.py`（10）—— 环形缓冲上限、get 语义、dump 原子写 + load 往返
  - `test_ovs_logger.py`（15）—— 日志行解析、UTC 时间戳（不偏 8 小时）、6 种过滤
  - `test_state_model.py`（26）—— 5 个验证器 + 主入口 ok/issue_count/by_level 汇总
同时修了 CI：原先只跑 2 个测试文件，新测试不会被执行；现改为跑 9 个。

**提交**: `待提交（2026-10-26）`

**怎么开发**(优先级排序):
1. **`test_db_writer.py`**:为 12 个 write_* 方法做 mock + 真实 DB 双测试(每个方法:成功 + 失败 + idempotent)
2. **`test_assurance_mvp.py`**:8 状态机转换 + 14 guard_checks 边界
3. **`test_state_server.py`**:12 个 REST 端点状态码 + JSON 格式
4. **`test_state_history.py`**:环形缓冲 + 原子持久化
5. **`test_ovs_logger.py`**:日志解析(已有 OVSLogParser 类的话)
6. `controller_app.py` 不容易单测(依赖 os-ken 事件循环),但可以用 mock EventOFP* 测试事件钩子逻辑

---

### ✅ P1-3 · 代码风格(lint/format)

**完成情况**: 新增 `pyproject.toml`（ruff 配置：line-length=100 / target py312 / 排除 legacy/）与 `.pre-commit-config.yaml`（ruff --fix）。
关键豁免：**E402** 必须豁免 —— `eventlet.monkey_patch()` 必须在 os_ken import 之前调用，否则控制器直接挂；这不是风格问题。
一次性 `ruff check --fix` 修了 52 处（未用 import / f-string 无占位符 / 文件尾换行等），另手工拆 24 处 E501 长行（只改排版不改语义，SQL 用换行等价替换）。
CI 加了 `ruff check .` 步骤防退化。`ruff check` 现已 **All checks passed**。

**提交**: `待提交（2026-10-26）`



---

### ✅ P1-5 · Mininet + tmux 启动繁琐

**完成情况**: 新增 3 个脚本，均已在 VM 实测通过：
- `start_all.sh` —— 一键起 4 个组件（tmux 窗口 controller/api/mininet/assurance，--with-live 可加 live_env）：控制器 6653 + REST 8080 + Mininet 4 主机 + 保障模块。起完自动校验端口与 /api/health。
- `stop_all.sh` —— 一键优雅停止：先发 SIGTERM（走 graceful.py 落盘/关DB/停拓扑），等 8 秒仍未退则强杀；同时清 tmux 与 Mininet，最后复查残留。
- `status.sh` —— 5 段体检：进程 / tmux 窗口 / 端口+健康 / 13 张表行数 / 汇总。live_env 为可选项不计入失败。
实测：start_all 起 4 组件全绿；status 全绿 exit 0；stop_all 三进程全部优雅退出、tmux 与 Mininet 清理干净、DB 数据保留。

**提交**: `待提交（2026-10-26）`



---

### ✅ P2-1 · 100 意图场景压测

**完成情况**: 新增 `demo_scripts/load_test.py`，跑了两组对比压测（100 意图）：
- **最坏上界**（每 intent 每周期都写）10 分钟 → 13,431 次写，22.3 写/秒，**0 错误**
- **真实模式**（走 `should_write_consistency` 变更检测 + 60s 心跳，与线上一致）3 分钟 → 1,047 次写，5.8 写/秒，**0 错误**

**实测结论**：算力完全不是瓶颈（CPU 平均 2.4%、峰值 28.3%；RSS 恒定 30.6 MB；
PG 锁等待 **0**；连接数全程 1；5s 周期实际只用 p50 395ms = 预算 8%）。**瓶颈是磁盘行数增长。**

**实测单行字节**：`consistency_checks` 279 B、`diagnoses` 426 B、`state_history` 2,201 B。
→ 最坏模式 482 MB/天 → 176 GB/年；真实模式 ~46 MB/天 → **~17 GB/年**。

**顺带修正了 `docs/retention-policy.md` 的两处偏差**（原估 13 GB 偏低）：
1. 混合策略行数原估 17,000/天，实测 **144,000/天**（60s 心跳 × 100 意图是地板值，省不掉，原估低估 8.5 倍）
2. 「够用 1.2 年+」修正为「够用 1 年」
另新增发现：故障期 `is_diagnosing=True` 强制每次都写，**写放大翻倍**（实测 213 写/分 vs 稳态 100 写/分）—— 容量规划要按故障态算。

报告：`docs/load_test_results.md`（含原始输出 `docs/loadtest_raw/`）

**提交**: `待提交（2026-10-26）`



---

### ✅ P3-1 · alembic 数据库迁移工具

**完成情况**: 引入 alembic，把「改 schema」从「手工 DDL + 手工同步两个文件」变成「一条命令 + 一个版本号」。
- `alembic.ini` + `migrations/env.py`（自定义，**不用 autogenerate** —— 本项目无 SQLAlchemy 模型，
  autogenerate 没有比对基准只会产噪音）+ `migrations/script.py.mako`
- `0001_baseline.py`：**由 `db_maintenance/gen_baseline.py` 从 `docs/database-schema.sql` 自动生成**
  （不手抄，保证基线 == 规范文件）
- `0002_retention_function.py`：只建 `drop_old_partitions()` 函数。**刻意不包含**清理主流程 ——
  迁移必须无副作用，把删分区的动作放进 upgrade 等于「升个版本就删数据」。
  清理留在 `db_maintenance/run_retention_cleanup.sh`（cron 定期跑）
- **顺手修掉分区定时炸弹**：schema 文件把月分区写死成 2026_10/11/12，到 2027 年 1 月建新库
  就没有当月分区，任何 INSERT 报 `no partition of relation ... found for row`。
  基线改为按「当前月 + 0..2」**动态创建**
- `docs/ALEMBIC_WORKFLOW.md`：用法 + 流程 + **8 个踩过的坑**
- `demo_scripts/verify_fresh_db.sh` 改走 alembic 路径，并新增「**基线 vs 规范文件漂移检查**」
- CI 新增一步「检查基线与 schema 规范文件一致」（纯文件比对，**不需要数据库**）

**验收**：`alembic upgrade head` 从零建库 → 5 个 schema 表数与现库逐一相等、
`drop_old_partitions()` 函数已建、7/7 分区表有当月分区、7/7 写入路由正常；
`downgrade base` 干净清库（0 残留）再 `upgrade head` 正常。

**提交**: `待提交（2026-10-27）`



---

### ✅ P2-4 · DB 自动重连

**完成情况**: `db_writer._get_conn` 改为**带指数退避的自动重连**：
- 最多 3 次尝试，退避 0.5 / 1 / 2 秒；日志打印 `reconnecting in Xs`（原来直接抛一串连接错误）
- `psycopg2.connect` 加 `connect_timeout=5` —— libpq 默认无超时，PG 真挂了会长时间挂起
- **区分「连接死了」和「查询本身出错」**：前者重连，后者直接冒泡不重连（免得掩盖语法错/权限不足的真因）
- 退避等待在锁外，不阻塞其他线程
- 新增统计：`reconnects`（成功建连次数）/ `connect_retries`（失败重试次数）

**实测**：`systemctl restart postgresql` 后写入**自动恢复**（0.91s），
stats 显示 reconnects=3 / errors=0；另加 3 个回归测试锁住行为。

**提交**: `待提交（2026-10-28）`



---
### ✅ P2-5 · 历史数据压缩 / 归档

**完成情况**: **先确认了问题真的存在**：VM 里既没有 crontab 也没有 systemd timer —— 
文档写的「30/7 天保留」**从来没被自动执行过**。

- 新增 `db_maintenance/retention-cleanup.service` + `.timer`（进版本库，可复现），
  每天 04:07 跑，`Persistent=true`（关机/休眠错过了开机补跑，笔记本场景重要）+ 随机延迟 5 分钟
- 跑 `db_maintenance/run_retention_cleanup.sh`（systemd 包装脚本）

**踩到并修掉的坑**：服务用 `User=postgres` + `psql -f` 会报 `Permission denied` —— 
项目目录是 0700，postgres 读不到 .sql 文件。
改成 `User=sunxiaoxuan` + **stdin 管道**（`<` 重定向）：由项目属主打开文件喂给 psql 的 stdin，
postgres 不需要读路径 —— 和 alembic 里 `_exec_raw` 同一思路。

**实测**：造了 2026_08 的过期分区 → 触发 service → **两个分区都被删掉**，只剩当月及以后。

**提交**: `待提交（2026-10-28）`



---

### ✅ P1-2 · 日志一致性(1 个 logging vs 23 个 print)

**完成情况**: 统一用 `logging`，7 个模块的 **75 处 print** 全部改造。
- 新增 `logging_setup.py`：统一配置（级别 / 格式 / 文件 handler / 幂等）
- 改造模块：`controller_app` / `live_env` / `assurance_mvp` / `db_writer` / 
  `run_all` / `reporter` / `state_server` / `fault_injector`
- **输出走 stderr** —— demo 脚本全是 `cmd 2>&1 | tee xxx.log`，合并流所以照样拿得到，
  不会因为改造日志而破坏演示脚本
- 级别自动判定：❌/ERROR → error，⚠️/WARN → warning，`if DEBUG:` 块内 → debug，其余 info
- **文件 handler**：设 `INTENSURE_LOG_FILE` 即落盘（`start_all.sh` 已设成 
  `/var/log/intensure/intensure.log`）；不设则不写文件，测试/CI 不污染 /var/log

**CLI 工具故意不改造**（`demo.py` / `demo_full.py` / `verify_live.py` / `seed_policies.py`）——
它们的 stdout 就是对外接口，换成 logging 反而破坏使用方式。

**验收**：ruff 全绿、135 单测通过、**完整 demo 8/8 全绿 EXIT=0**、日志文件实测落盘正常。

**提交**: `待提交（2026-10-28）`



---

---

## 📋 工作量汇总

| 类别 | 项数 | 估计总工作量 |
|------|------|-------------|
| 🔴 P0 | 0 | 0 天 ✅ |
| 🟡 P1 | 0 | 0 天 ✅ |
| 🟢 P2 | 2 | 5-6 天 |
| 🔵 P3 | 3 | 6-7 天 |
| **合计** | **5** | **~7-9 工作日** |

---

## 🎯 建议节奏

1. **本周(10-09 ~ 10-15)**:P0-1(requirements.txt) + P0-5(CI) + P0-2(reporter bug) —— 这些**门槛低价值高**
2. **下周(10-16 ~ 10-22)**:P0-3(graceful shutdown) + P0-4(连接池) + P1-1(db_writer 测试)
3. **10-23 ~ 10-30**:P1-2(日志) + P1-3(lint) + P2-1(压测)
4. **10-31 ~ 项目结束**:P2 / P3 视项目进度选

---

_写于 2026-10-09 v0.1(主人要求"记下来该怎么开发")_
_下次更新:每完成一项,把该项从原优先级移到底部"已完成"段_
