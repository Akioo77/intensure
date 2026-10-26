# intensure 优化清单(2026-10-09 整理)

> **目的**:盘点 intensure 项目当前可改进的地方,按优先级排好,标注**做什么 + 怎么开发 + 工作量**。
> **状态**:v0.5(2026-10-26 更新)——**P0 全部完成**，P1-1 测试覆盖、P1-3 代码风格也已完成（9 个关键模块有测试、131 用例；ruff check 全绿且 CI 已接入拦截），共 9 项已完成，剩余 11 项待办。
> **边界**:**只关心 intensure 这边**——翻译组 / 冲突消解组 / A 同学 assurance-agent 的内部设计不归我们管。
> **不抢活**:我们能催/建议,但不能越界做。

---

## 🎯 优先级总览(11 项待办 / 9 项已完成)

| 优先级 | 项数 | 类别 |
|------|------|------|
| 🔴 P0(基础缺失,必须补) | **0** | 全部完成 ✅ |
| 🟡 P1(质量/可靠性) | 2 | 日志统一 |
| 🟢 P2(性能/扩展) | 5 | 压测 / dashboard / 故障类型 |
| 🔵 P3(完善) | 4 | 迁移 / Docker / observability |

---

## 🔴 P0 · 必须补(基础)


---


---


---


---


---

## 🟡 P1 · 质量 / 可靠性


---

### P1-2 · 日志一致性(1 个 logging vs 23 个 print)
**问题**:代码库里只有 1 个文件用 `logging` 模块,23 个用 `print()`——演示时输出噪声巨大,没法按级别过滤。

**怎么开发**:
1. 全模块统一用 `logging.getLogger(__name__)`
2. `run_all.py` / `live_env.py` 启动时配置 root logger(`level=INFO`)
3. 把 `print(f'CYCLE_2550 ...')` 改成 `logger.info('CYCLE %d ...', 2550)`
4. 加 file handler(`/var/log/intensure/intensure.log`)
5. 删除 demo_scripts 里的 `tee /tmp/live.log` 那套(改 logger 接管)

**工作量**:2-3 天
**价值**:🟡 中(日常不影响,排障时救命)

---


---


---

### P1-5 · Mininet + tmux 启动繁琐
**问题**:`run_all.py` 起 controller / Flask,Mininet 用 `sudo mn --topo single,4` 起,live_env.py 再起流量——演示时**3 个进程都要手动起**,容易卡住或忘记。

**怎么开发**:
1. 加 `start_all.sh`:一键 tmux session 起 3 个窗口
2. 加 `stop_all.sh`:一键 graceful kill
3. 加 `status.sh`:看每个进程是否活着 + DB 行数
4. 这些都基于现有 `demo_db_pipeline.sh` 的经验

**工作量**:1 天
**价值**:🟡 中(演示现场救命)

---

## 🟢 P2 · 性能 / 扩展

### P2-1 · 100 意图场景压测
**问题**:`docs/database-architecture.md` 算过 "100 意图 1 年 ≈ 13 GB",**但没真跑过**。controller 5s upsert + 30s 节流 + 异常才写,在 100 意图并发下能不能扛住?

**怎么开发**:
1. 写一个 `load_test.py`:模拟 100 个 intent_id,每个 5s 触发一次 controller write
2. 跑 1 小时,看 CPU / 内存 / DB 连接数 / storage 增长
3. 用 `pg_stat_activity` 看是否有锁等待
4. 出报告:`load_test_results.md` —— 性能基线

**工作量**:2-3 天
**价值**:🟢 高(后续扩展的基础数据)

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

### P2-4 · DB 自动重连
**问题**:如果 PostgreSQL 短暂挂了(`systemctl restart postgresql`),db_writer 会**永久失败**直到重启 intensure。

**怎么开发**:
1. `db_writer.py` 的 `_get_conn` 加 `OperationalError` 捕获 + 自动重连(最多 3 次,指数退避)
2. 连接池化(P0-4 一起做更好)
3. 验证:`sudo systemctl restart postgresql` 时 controller 日志看到"reconnecting"而不是"failed permanently"

**工作量**:0.5 天(配合 P0-4 做)
**价值**:🟡 中(生产环境重要)

---

### P2-5 · 历史数据压缩 / 归档
**问题**:`state_history` 30s 节流,1 天约 2880 行。1 年 100 意图 = 105M 行。文档说"30/7 天保留",但**实际没看到 cleanup job 跑**。

**怎么开发**:
1. 验证 `db_maintenance/retention_cleanup.sql` 是否被定期调用(cron / systemd timer)
2. 没的话加 systemd timer 每天跑 `partition_drop`
3. 月分区表 → 自动 drop 老月份
4. 验证:90 天后老数据自动消失

**工作量**:1 天
**价值**:🟡 中(长期跑)

---

## 🔵 P3 · 完善

### P3-1 · alembic 数据库迁移工具
**问题**:现在是手工跑 SQL(`docs/database-schema.sql`)。改表结构要手工对比 + 手工 ALTER。

**怎么开发**:
1. `alembic init migrations`
2. `alembic revision --autogenerate -m "add audit_log correlation_id index"`
3. CI 验证 migration 能跑(`alembic upgrade head` + `alembic downgrade base`)
4. 配合 P3-2(deployment)一起做

**工作量**:2 天
**价值**:🟢 高(长期工程价值)

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

---

## 📋 工作量汇总

| 类别 | 项数 | 估计总工作量 |
|------|------|-------------|
| 🔴 P0 | 0 | 0 天 ✅ |
| 🟡 P1 | 2 | 2-3 天 |
| 🟢 P2 | 5 | 9-12 天 |
| 🔵 P3 | 4 | 8-9 天 |
| **合计** | **11** | **~17-21 工作日** |

---

## 🎯 建议节奏

1. **本周(10-09 ~ 10-15)**:P0-1(requirements.txt) + P0-5(CI) + P0-2(reporter bug) —— 这些**门槛低价值高**
2. **下周(10-16 ~ 10-22)**:P0-3(graceful shutdown) + P0-4(连接池) + P1-1(db_writer 测试)
3. **10-23 ~ 10-30**:P1-2(日志) + P1-3(lint) + P2-1(压测)
4. **10-31 ~ 项目结束**:P2 / P3 视项目进度选

---

_写于 2026-10-09 v0.1(主人要求"记下来该怎么开发")_
_下次更新:每完成一项,把该项从原优先级移到底部"已完成"段_
