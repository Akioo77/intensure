# Phase 3 路线图 - 意图驱动网络管理系统

> 文档版本：v1.0 (2026-08-08)
> 截止日期：~2026-08-18（还有 10 天）
> 当前已完成：Phase 2（4 大任务 + 5 个追加）
> 目标：呼应系统 2 大目标（精准度提升 + 冲突消解）

---

## 🎯 设计原则

**直接呼应系统 2 大目标**：
- 目标 1（意图翻译与配置生成的精准度与可靠性提升）
- 目标 2（意图冲突检测与自动消解）

**优先级排序**：
- **Tier 1**（必做）：直接命中目标的 3 个功能
- **Tier 2**（建议做）：加分项 3 个
- **Tier 3**（汇报）：PPT + 项目报告 + Demo 视频

---

## 🔥 Tier 1（必做）：直击 2 大目标

### Feature 1：意图冲突检测（1.5 天）

> **直接对应目标 2**

**功能描述**：检测新提交的意图与当前激活意图之间的冲突。

**冲突类型**：

| 类型 | 示例 |
|---|---|
| **正向 vs 负向** | intent-A 要求"A-B 互通"，intent-B 要求"A-B 隔离" |
| **资源竞争** | 两个 QoS 意图对同一链路带宽要求之和超过物理上限 |
| **时延矛盾** | intent-A 要求时延 ≤ 10ms，intent-B 要求时延 ≥ 100ms（同链路） |
| **可达性矛盾** | intent-A 要求 A 隔离，intent-B 要求 C 经过 A 中转 |

**接口设计**：

```
GET  /api/intents               # 列出当前所有激活意图
POST /api/intents/check         # 提交新意图，返回冲突分析
GET  /api/conflicts             # 当前活跃冲突列表
POST /api/conflicts/resolve     # 提交消解决策
```

**实现步骤**：
1. `intent_registry.py` - 意图注册表（增删改查 + 冲突索引）
2. `conflict_detector.py` - 冲突检测引擎（4 种类型分别算法）
3. 扩展 `state_server.py` 加 4 个 REST 端点
4. dashboard 新增"意图冲突"面板（实时显示冲突）

**验证标准**：
- 10 个测试意图中召回率 ≥ 90%
- 检测延迟 < 1s
- 误报率 < 10%

---

### Feature 2：自愈完整回路（1.5 天）

> **直接对应目标 1**

**功能描述**：补齐从"检测 violation"到"恢复一致"的完整回路。

**当前状态**（Phase 2 已完成）：
- ✅ 检测到 violation（state_model.validate）
- ✅ 生成偏差报告（state_history + consistency_check）

**Phase 3 新增**：
- ❌ → ✅ **自动生成"自愈意图"**（基于偏差类型模板化生成）
- ❌ → ✅ **推送自愈意图给上游**（POST /api/healing/intent）
- ❌ → ✅ **记录自愈完整生命周期**（检测 → 生成 → 下发 → 验证恢复）
- ❌ → ✅ **自愈失败升级**（避免无限循环）

**自愈意图模板**：

| 偏差类型 | 自愈动作 | 生成的自愈意图 |
|---|---|---|
| 链路 DOWN | 重下发链路配置 | `{"action": "reroute", "path": "...", "deadline_ms": 5000}` |
| 策略漂移 | 重下发 ACL | `{"action": "reapply_acl", "policy_id": "...", "targets": [...]}` |
| QoS 不达标 | 重下发 QoS | `{"action": "adjust_qos", "link": "...", "target_delay_ms": 50}` |
| 连通性丧失 | 重探测 + 重下发 | `{"action": "reprobe_then_redo", "path": "..."}` |

**接口设计**：

```
GET  /api/healing/status        # 当前自愈状态（无/检测中/执行中/完成/失败）
GET  /api/healing/history       # 自愈历史（含完整生命周期）
POST /api/healing/intent        # 接收上游的自愈指令
POST /api/healing/trigger       # 手动触发自愈（测试用）
```

**实现步骤**：
1. `healing_engine.py` - 自愈引擎（状态机：idle → detecting → generating → pushing → verifying → done/failed）
2. `healing_intent_generator.py` - 基于偏差类型生成自愈意图（模板化）
3. 扩展 REST API（5 个端点）
4. dashboard 新增"自愈工作流"面板（实时状态 + 历史时间线）

**验证标准**：
- 检测 → 生成 → 推送：总延迟 < 5s
- 自愈成功率 ≥ 80%（剩余升级人工）
- 自愈前后状态对比可视化清晰

---

### Feature 3：冲突自动消解建议（1 天）

> **直接对应目标 2**

**功能描述**：检测到冲突后，自动生成消解建议，反馈给意图翻译智能体。

**消解策略**：

| 策略 | 适用场景 | 示例 |
|---|---|---|
| **优先级覆盖** | 高优先级意图可覆盖低优先级 | "intent-A 优先级=高，覆盖 intent-B 的冲突部分" |
| **时序覆盖** | 最新意图覆盖最旧（可配置） | "intent-A 后于 intent-B，按最新生效" |
| **协商修订** | 双方都可让步 | "建议 intent-A 改为 A-C 而非 A-B，intent-B 保留" |
| **拒绝接受** | 严重冲突不可消解 | "新意图与安全策略冲突，建议拒绝" |

**接口设计**：

```
POST /api/conflicts/resolve     # 提交消解决策（手动）
POST /api/conflicts/suggest     # 请求自动消解建议（自动）
```

**实现步骤**：
1. `conflict_resolver.py` - 消解策略引擎（4 种策略 + 选择算法）
2. 集成到 `conflict_detector.py`（检测 → 建议一体化）
3. dashboard 冲突面板新增"建议"列（点击可应用）

**验证标准**：
- 10 个测试冲突中能给出合理建议 ≥ 8 个
- 建议合理性可由"用户接受率"衡量（dashboard 反馈按钮）

---

## ⭐ Tier 2（建议做）：锦上添花

### Feature 4：多意图并发监控（0.5 天）

> 扩展现有 3 意图示例到 10+ 意图并发校验。

**实现**：
- `intents_full.json` - 10+ 意图样本（含冲突场景）
- dashboard 多意图视图（表格 + 一致性矩阵）
- 批量校验 API：`POST /api/intents/check_all`

---

### Feature 5：异常检测算法（1 天）

> 从历史数据检测异常模式（不仅检查一致性，还预测问题）。

**检测项**：
- 时延突增（> 历史均值 2σ）
- 丢包累积（> 阈值持续 5s）
- 流量异常（与基线偏差 > 30%）
- 链路频繁震荡（同一链路 1 分钟内 down/up > 3 次）

**实现**：
- `anomaly_detector.py` - 基于历史窗口的滑动检测
- REST 端点：`GET /api/anomalies`
- dashboard 新增"异常"面板（实时滚动）

---

### Feature 6：状态采集延迟基准测试（0.5 天）

> 验证系统目标 3：状态采集延迟 ≤ 3s（从网络事件到 dashboard 显示）。

**实现**：
- `perf_benchmark.py` - 自动测量事件 → 状态更新 → REST 返回 → 浏览器渲染全链路延迟
- 多场景测试：1/2/5/10 交换机拓扑
- 输出报告：`perf_report.md` + 图表

---

## 📊 Tier 3（汇报）：PPT + 报告 + Demo 视频

### Feature 7：项目 PPT（1 天）

> 10-15 页，覆盖系统全貌 + 我的核心贡献。

**结构**：
1. 标题 + 团队介绍
2. 系统总目标
3. 3 智能体架构图
4. **我的贡献（同学 A）：状态采集 + 一致性保障** ⭐ 重点
5. 关键技术：LLDP / 故障注入 / 自愈回路
6. Demo 截图（dashboard 4 大场景）
7. 测试覆盖与性能
8. 总结与展望

**工具**：用 `apple-design-skill` 做 Apple 风 PPT（响应主人偏好）

---

### Feature 8：项目报告 Markdown（0.5 天）

> 完整文档，配套 PPT。

**结构**：
- 摘要 / 背景 / 系统设计 / 实现 / 测试 / 总结

---

### Feature 9：Demo 视频录制（0.5 天）

> 5 分钟，覆盖演示流程（REQUIREMENTS §5.6）

**工具**：`screencapture` + QuickTime + 剪辑

---

## 📅 时间表（10 天倒计时）

| 日期 | 任务 | 累计进度 |
|---|---|---|
| **8/9 (周日)** | Feature 1 冲突检测（1.5 天） | 30% |
| **8/10 (周一)** | Feature 2 自愈回路 | 60% |
| **8/11 (周二)** | Feature 3 冲突消解 + Feature 4 多意图 | 80% |
| **8/12 (周三)** | Feature 5 异常检测 + Feature 6 性能基准 | 95% |
| **8/13 (周四)** | Feature 7 PPT | 105% |
| **8/14 (周五)** | Feature 8 报告 + Feature 9 Demo 视频 | 115% |
| **8/15-8/17** | 缓冲 + 与同学 B/C 联调 | - |
| **8/18** | **截止** | - |

---

## 🎯 Tier 选择策略

**至少完成 Tier 1（3 个功能）+ Tier 3 的 PPT** —— 这是"必拿分"。

**时间充裕时**：加 Tier 2 的功能（演示更丰满）。

**风险点**：
- 与同学 B/C 联调：自愈回路需要上游接收，**必须尽早与他们约定 API**
- 演示时长：5 分钟内要展示所有功能，需要 demo 脚本提前演练

---

## 🤝 立即可做（无需等别人）

- ✅ Feature 1（冲突检测）：纯后端 + dashboard，独立完成
- ✅ Feature 2（自愈回路）：可以先做"自愈意图生成 + REST 端点"，联调延后
- ✅ Feature 3（消解建议）：纯后端，独立完成
- ✅ Feature 5（异常检测）：纯后端，独立完成
- ✅ Feature 6（性能基准）：独立完成
- ✅ Feature 7-9（汇报材料）：独立完成

---

**下一步**：建议今晚先启动 Feature 1（冲突检测），定个 API 接口就开干。需要与同学 B/C 联调的部分延后到中后期。

📁 文档位置：
- 本地：`~/workspace/projects/intensure/PHASE3_PLAN.md`
- VM：`/home/sunxiaoxuan.guest/intensure/PHASE3_PLAN.md`