# 下一步开发方案（采集模块视角）

> 文档版本：v1.0 (2026-09-03)
> 适用范围：替换旧的 `PHASE3_PLAN.md`
> 目标：把"采集模块"做成可联调的工作版本，与同学 A 的保障模块、同学 C 的翻译模块对接

---

## 🎯 设计原则

1. **对外只输出一份 ActualState** —— 不做任何业务判断
2. **内部保持采集模块的独立性** —— 自己的拓扑、链路、流表、自检、可视化
3. **不重复造轮子** —— 冲突 / 自愈 / 诊断由其他同学做
4. **加分项保留但不影响主流程** —— Dashboard、LLDP、异常自检

---

## 📂 文件分类（动 / 不动 / 归档）

### 🔴 必须新做（其他同学依赖我们）

| 文件 | 工作量 | 关键内容 |
|---|---|---|
| `actual_state_builder.py` | 0.5 天 | 内部 `state_model` 数据 → 联调 §4.2 的 ActualState JSON；重点是 `down_links` / `down_ports` 用 `"swN:pN"` 字符串格式；带字段过滤（无 SLA 字段传 null） |
| `reporter.py` | 0.5 天 | POST `/check` + `/verify` 客户端；配置保障模块 base URL；HTTP 错误处理（409 状态机错 / 404 未注册）；指数退避重试 |
| `integration_demo.py` | 1 天 | 端到端：手工注册测试意图 → 激活 → 周期探测 + 上报 → 注入故障 → 复测；输出**首次探测样例**（§3.3 联调前提） |

### 🟡 必须改造（保留但调整）

| 文件 | 改动 |
|---|---|
| `live_env.py` | 改造成"周期探测 → 调 reporter 上报"模式；接受 `intent_id` 参数（从上游透传） |
| `demo_full.py` | 改造成"端到端 + 上报"版本；保留故障注入 + 验证逻辑，新增 POST 步骤 |
| `controller_app.py` | 简化：不再写 `/tmp/network_state.json`（保障模块管状态）；只输出事件流供 ActualStateBuilder 消费 |
| `state_collector.py` | 改造成手工 CLI 工具（不接 REST API，自营 REST 已取消） |

### 🟢 保留（采集模块独立价值）

| 文件 / 功能 | 保留理由 |
|---|---|
| `topo_simple.py` / `topo_multi.py` / `topo_mesh.py` | 拓扑是采集模块核心 |
| `controller_app.py` (LLDP 部分) | 链路事件采集 = ActualState 的 `down_links` 数据源 |
| `fault_injector.py` | 联调必备：可控故障才有可复现上报 |
| `dashboard.html` (采集视角) | 加分项，不替代保障模块 |
| `simple_switch_13.py` | 参考实现 |

### 🟢 保留（轻量自检，与保障模块不冲突）

| 文件 | 用途 |
|---|---|
| `anomaly_detector.py` | **重定位**为"采集自检"：检测自身探测异常（如流表老化、采集延迟、采样缺失），与保障模块的"业务级异常"分属不同抽象层 |

### 🟢 保留（采集侧"基础"工具，与上报解耦）

| 文件 | 用途 |
|---|---|
| `state_model.py` | 内部数据抽象，供 ActualStateBuilder 读取 |

### ⚫ 归档到 `legacy/`（功能重叠 + 历史材料）

| 文件 | 重叠对象 |
|---|---|
| `intent_registry.py` | 翻译模块的 `child_intents` |
| `conflict_detector.py` | 翻译模块 v0.1 schema `conflicts[]` |
| `conflict_resolver.py` | 翻译模块 `conflicts[].suggestions[]` |
| `healing_engine.py` | 保障模块 `/verify` 自愈决策 |
| `healing_intent_generator.py` | 保障模块自愈策略 |
| `state_history.py` | 保障模块历史管理 |
| `state_server.py` | 自营 REST（被 reporter 替代） |
| `test_conflict_*.py` / `test_healing_*.py` | 对应归档 |
| `PHASE3_PLAN.md` | 旧路线图，迁移到 `legacy/PHASE3_PLAN_v1.md` |
| `dashboard_v6_v7_v8_v9.png` | 旧截图 |

---

## 🔌 接口契约（**严格遵守**）

### 4.1 ActualState 字段（与联调说明 §4.2 一致）

```json
{
  "intent_id": "REQ-001-CI-001",             // 必填
  "checked_at": "2026-09-03T10:05:00+08:00", // 必填，ISO 8601
  "reachable": true,                         // 必填
  "expected_flow_present": true,             // 必填
  "source_host": "10.0.10.5",                // 选填
  "destination_host": "10.0.60.10",          // 选填
  "latency_ms": 12.3,                        // 选填（无 SLA 传 null）
  "packet_loss": 0.0,                        // 选填（无 SLA 传 null）
  "down_links": ["sw1:p1-sw2:p2"],           // 选填，格式 "设备:端口-设备:端口"
  "down_ports": ["sw1:p1"],                  // 选填，格式 "设备:端口"
  "conflicting_flows": [{"switch": "sw4", "cookie": "0x123"}]
}
```

### 4.2 上报端点（采集 → 保障）

| 端点 | 触发时机 | 请求体 | 响应 |
|---|---|---|---|
| `POST /api/v1/assurance/check` | 每次探测后 | `{intent_id, actual: ActualState}` | `{status: ACTIVE \| VIOLATED, violations[]}` |
| `POST /api/v1/assurance/verify` | 修复后复测 | `{intent_id, actual_after_heal: ActualState}` | `{status: RECOVERED \| FAILED}` |

### 4.3 HTTP 错误码（要主动处理）

| 错误 | 含义 | 行动 |
|---|---|---|
| 409 | 状态机前置条件不满足（如未 activate） | 轮询 `/record/{intent_id}` 看当前状态 |
| 404 | 意图未注册 | 报错退出 / 重试注册 |
| 5xx | 保障模块异常 | 指数退避重试 3 次后告警 |

### 4.4 配置项（reporter 启动时读取）

```yaml
# config.yaml 示例
assurance:
  base_url: "http://192.168.1.100:8000/api/v1/assurance"
  timeout_s: 5
  retry: 3
  retry_backoff: "exponential"

collection:
  intent_id: null             # 由上游透传或 CLI 参数注入
  interval_s: 15              # 探测周期
  source_host: "10.0.10.5"    # 选填
  destination_host: "10.0.60.10"

topology:
  dpid_to_switch:             # 内部 dpid → 联调约定 "swN" 映射
    "0000000000000001": "sw1"
    "0000000000000002": "sw2"
```

---

## 🪜 实施步骤（推荐顺序）

### 阶段 1：契约对齐（0.5 天）
- [ ] 写 `actual_state_builder.py`
- [ ] 写单元测试：内部 state → ActualState 字段映射表（5 个测试用例）
- [ ] 跑一次 demo，输出**真实探测样例**给同学 A 确认（§3.3）

### 阶段 2：上报客户端（0.5 天）
- [ ] 写 `reporter.py`：POST `/check` + `/verify`
- [ ] 单元测试：mock 保障模块响应（200/409/404/500）
- [ ] 集成测试：起本地 mock 服务（FastAPI + 一条假意图）验证 reporter 流程

### 阶段 3：端到端联调脚本（1 天）
- [ ] 写 `integration_demo.py`：
  - 手工注册一条测试意图（用 §5 的 curl 示例）
  - 激活
  - 启动周期探测 + 上报循环
  - 注入故障 → 等探测失败 → 上报
  - 修复 → 复测上报
- [ ] 跑通后**第一次正式联调**（同学 A 配合）

### 阶段 4：联调修正（0.5-1 天）
- [ ] 根据同学 A 反馈调整 `actual_state_builder.py`
- [ ] 处理边界场景（端口 down 但链路未完全 down 等）
- [ ] 补 ActualState 异常态样例

### 阶段 5：Dashboard 调整（0.5 天）
- [ ] dashboard.html 增加"采集统计"面板（上报次数 / 错误次数 / 最近响应时间）
- [ ] dashboard.html 增加"探测样例"页（生成并展示给同学 A 对齐用）

### 阶段 6：文档定稿（0.5 天）
- [ ] 更新 README.md（取消自营 REST 章节，加入上报章节）
- [ ] 写联调日志（含首次跑通的截图、调试记录）

---

## ⏱️ 时间表（建议）

| 阶段 | 工作量 | 累计 |
|---|---|---|
| 阶段 1 | 0.5 天 | 0.5 天 |
| 阶段 2 | 0.5 天 | 1 天 |
| 阶段 3 | 1 天 | 2 天 |
| 阶段 4 | 0.5-1 天 | 3 天 |
| 阶段 5 | 0.5 天 | 3.5 天 |
| 阶段 6 | 0.5 天 | 4 天 |

---

## ⚠️ 风险点与缓解

### 风险 1：ActualState 字段格式与同学 A 期望不一致
- **概率**：中（首次联调常见）
- **缓解**：阶段 1 就跑 demo 拿真实样例给同学 A 确认；不等到阶段 3 才暴露

### 风险 2：Windows 防火墙挡住 8000 端口
- **概率**：高（默认入站阻止）
- **缓解**：联调前确认同学 A 防火墙规则；用 Swagger UI（`/docs`）先验证可达

### 风险 3：同学 A 的保障模块在跑别的服务（端口冲突）
- **概率**：低
- **缓解**：让同学 A 提供部署文档 + 启动日志

### 风险 4：跨平台 JSON 编码（GBK vs UTF-8）
- **概率**：中
- **缓解**：reporter 强制 `Content-Type: application/json; charset=utf-8`，构造 payload 时 `ensure_ascii=False`

### 风险 5：clock skew（时间戳对不上）
- **概率**：低（ISO 8601 是字符串）
- **缓解**：使用本地时间 + 时区；不传 unix timestamp

### 风险 6：reporter 阻塞 live_env 探测循环
- **概率**：低（HTTP 请求异步即可）
- **缓解**：用 `requests` + `concurrent.futures.ThreadPoolExecutor` 异步上报

---

## 🎯 验收标准

### 必达（硬指标）

- [ ] `actual_state_builder.py` 输出 100% 通过 §4.2 字段一致性测试
- [ ] `reporter.py` 在 mock 保障模块下，200/409/404/500 全处理正确
- [ ] `integration_demo.py` 跑通完整 5 步：注册 → 激活 → 探测 → 故障 → 复测
- [ ] **真实联调跑通一次**：与同学 A 的 Windows:8000 跑通至少 1 个完整流程

### 加分（软指标）

- [ ] Dashboard 展示上报统计（成功率 / 响应时间）
- [ ] 真实样例生成器（输出给同学 A 对齐用）
- [ ] 单元测试覆盖率 ≥ 70%

---

## ❓ 待主人确认

1. **保障模块 IP / Swagger 地址**：同学 A 给了之后能立刻开干
2. **是否需要 mock 保障模块**：在同学 A 机器不可达时，先用本地 mock 跑通 reporter
3. **联调前的探测样例**：要不要先内部跑一遍 demo，把样例发群里给同学 A 看？

---

## 📚 相关文档

- `REQUIREMENTS.md` —— 本模块职责 + 联调契约背景
- `~/Desktop/网络智能体/采集模块联调说明.md` —— **真实契约（硬约束）**
- `~/Desktop/网络智能体/意图翻译智能体_案例1-6_接口确认稿_v0.1.pdf` —— 翻译模块 schema
- `legacy/PHASE3_PLAN_v1.md` —— 旧路线图（参考）
- `legacy/dashboard_v6-v9.png` —— 旧 Dashboard 截图（参考）

---

📁 文档位置：
- 本地：`~/workspace/projects/intensure/INTEGRATION_PLAN.md`
- VM：`/home/sunxiaoxuan.guest/intensure/INTEGRATION_PLAN.md`（待同步）

**下一步**：等你给保障模块 IP + Swagger 地址后开干 💎