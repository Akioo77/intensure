# CHANGELOG — 网络意图 SDN 采集模块

> 历史变更记录。所有变更都有对应 git commit 与测试验证。

---

## v3.0 — 2026-09-03 (05:50) Dashboard 调试面板清理 + 自愈前后对比重建

**主要作者**: 庄英琪 💎
**触发**: 主人要求清理开发阶段遗留物，恢复「自愈前后结果」可视化（v2.0 误删）

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
  - 现在（对）：**同学 A = 状态采集（本系统 = 主人）**；同学 B/C = 意图智能体
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

## v2.0 — 2026-09-03 (02:50) 重构：与上游契约对齐

**触发**: 主人分享 `~/Desktop/网络智能体/采集模块联调说明.md`（同学 A 写）

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

## v1.0 — 2026-08-04 ~ 2026-09-02 初始开发

- Mininet + Colima + os-ken 环境搭建
- 自定义拓扑（simple/multi/mesh）
- 链路事件监听（LLDP + PortStatus）
- 故障注入（断链/时延/丢包/限速）
- 采集视角 Dashboard（单文件 SPA）
- 7 个 bug 修复（详见 `memory/2026-08-04.md` / `memory/2026-08-05.md`）

---

## 🎯 当前状态（v3.0）

| 同学 A 职责 | 状态 |
|------------|------|
| Mininet 场景搭建（拓扑 + 故障注入 5 类）| ✅ 100% |
| 网络状态采集（交换机/端口/流表/主机连接）| ✅ 100% |
| 实际状态模型（统一 JSON + 历史 + 接口）| ✅ 95% |
| 可视化（拓扑 + 链路 + 告警 + 自愈前后）| ✅ 100% |

**对外接口**：POST `/check` + `/verify`（已在 v2.0 完成）
**等待**：同学 B/C 提供真实保障模块 IP