-- ----------------------------------------------------------------------------
-- 文档元数据（用 Markdown frontmatter 风格标注）
-- 文件：docs/cross-module-api-contract.md
-- 版本：v0.1 (2026-10-08)
-- 目标：4 智能体之间的 API 契约（**重点：保障模块 3 条核心 API**）
-- 配套：完整 OpenAPI 3 YAML 规范见 docs/api/openapi.yaml（Phase 1 生成）
-- ----------------------------------------------------------------------------

# 跨模块 API 契约 v0.1

## 0. 核心原则（重述你的原话）

> **翻译模块可以查询"有效规则"，但不能因为生成了消解方案就把已部署规则删除。**

API 设计铁律：
1. **跨模块读**：通过 API 读（不直连对方 DB 表；即使 DB 有 SELECT 权限，也走 API）
2. **跨模块写**：必须调用对方模块的"command API"，**实际写入由被调用方负责**
3. **DELETE 是危险动词**：跨模块 DELETE 几乎都被禁；用 `POST /{resource}/{id}/retire` 这种语义化操作替代

## 1. 路径命名规范

```
/api/{module}/v{version}/{resource}
```

- `module`：`intensure` / `translation` / `implementation` / `assurance` / `shared`
- `version`：语义化版本（v1、v2），不强制日期
- `resource`：复数名词（policies / intents / states / checks）
- 状态转移操作：`POST /{resource}/{id}/{action}`（retire / activate / verify）

## 2. 通用约定

| 项 | 约定 |
|---|---|
| HTTP 方法 | GET（读）/ POST（命令/写）/ PATCH（局部更新）/ DELETE（**仅本模块自用**） |
| 请求/响应 | JSON |
| 错误响应 | 标准 HTTP code + 业务 code：`{"error": "VALIDATION_FAILED", "detail": "..."}` |
| 鉴权 | Phase 1 用 DB 用户密码（每个模块一个）；Phase 2 加 `mTLS` 或 `JWT` |
| ID | UUID v7（外部引用）；BIGSERIAL 仅做内部排序 |
| 时间 | ISO 8601 + `Z`（UTC）；不接受时区缩写 |
| 分页 | `?limit=50&offset=0`（Phase 1 简单版）；cursor 分页（Phase 2+） |
| 版本兼容 | 新增字段 = 兼容；删除字段 = major version 升级 |

## 3. 错误码（业务）

| code | HTTP | 说明 |
|---|---|---|
| `VALIDATION_FAILED` | 400 | 参数校验失败 |
| `UNAUTHORIZED` | 401 | 鉴权失败 |
| `FORBIDDEN` | 403 | 权限拒绝（如翻译尝试 DELETE 别人的表） |
| `NOT_FOUND` | 404 | 资源不存在 |
| `CONFLICT` | 409 | 状态冲突（如重复注册） |
| `DEPENDENCY_NOT_READY` | 424 | 上游依赖未就绪（如保障需 intensure 当前状态，但采集还没跑） |
| `RATE_LIMITED` | 429 | 频率超限 |
| `INTERNAL_ERROR` | 500 | 服务器内部错误 |

---

## 4. ⭐ 保障模块 3 条核心 API（重点）

### 4.1 `GET /api/intensure/v1/state/current`

**目的**：保障每次一致性检查都读这个（替代旧 `POST /check` 的实际状态来源）

**调用方**：保障（每 5 秒一次）

**鉴权**：assurance_user（DB 层）+ API 层签名（Phase 2+）

**请求**：
```http
GET /api/intensure/v1/state/current HTTP/1.1
Host: intensure:8080
Authorization: Bearer <token>
```

**响应**（200 OK）：
```json
{
  "captured_at": "2026-10-08T04:20:55.123Z",
  "version": 149046,
  "switches": [
    {
      "dpid": "0000000000000001",
      "ports": [
        {"port_no": 1, "name": "s1-eth1", "state": 4, "speed_mbps": 10000},
        {"port_no": 2, "name": "s1-eth2", "state": 4, "speed_mbps": 10000},
        {"port_no": 3, "name": "s1-eth3", "state": 1, "speed_mbps": 0}
      ]
    },
    {
      "dpid": "0000000000000002",
      "ports": [
        {"port_no": 1, "name": "s2-eth1", "state": 4, "speed_mbps": 10000},
        {"port_no": 2, "name": "s2-eth2", "state": 4, "speed_mbps": 10000},
        {"port_no": 3, "name": "s2-eth3", "state": 1, "speed_mbps": 0}
      ]
    }
  ],
  "links": [],
  "flows": {"0000000000000001": [...], "...": "..."},
  "down_ports": ["s1:eth3", "s2:eth3"],
  "down_links": ["s1:eth3-s2:eth3"]
}
```

**响应字段说明**：
- `version`：乐观锁版本号（保障模块记录"上次读到的 version"，下次对比判断采集是否在更新）
- `down_ports` / `down_links`：由 intensure 直接计算（保障模块**直接用**，不再自己解析）
- 字段命名遵循 s1:eth3 格式（**避免** dpid/port_no 数字 → 09-19 联调发现的可读性问题）

**错误响应**：
- `424 DEPENDENCY_NOT_READY`：采集还没初始化（首次部署时）
- `500 INTERNAL_ERROR`：intensure 内部异常

---

### 4.2 `GET /api/implementation/v1/policies/active`

**目的**：保障读取"当前应生效的所有策略"（期望状态来源）

**调用方**：保障（每 5 秒一次，配合上面那条）

**请求**：
```http
GET /api/implementation/v1/policies/active HTTP/1.1
Host: implementation:8081
```

**Query 参数**：
- `intent_type`（可选）：过滤特定类型（如 `?intent_type=ACCESS_CONTROL`）
- `include_superseded`（可选，默认 false）：是否包含被替代的策略

**响应**（200 OK）：
```json
{
  "policies": [
    {
      "intent_id": "live-AC-003",
      "intent_type": "ACCESS_CONTROL",
      "policy_spec": {
        "source": {"cidr": "192.168.10.0/24"},
        "destination": {"cidr": "10.0.50.10/32"},
        "protocol": "tcp",
        "destination_port": 443,
        "action": "ALLOW"
      },
      "effective_from": "2026-09-19T14:05:34Z",
      "effective_to": null,
      "deployed_by": "deploy-live-AC-003"
    }
  ],
  "total": 1
}
```

**保障模块拿到的 `policy_spec`** + intensure 的 `down_ports` → 跑一致性检查

---

### 4.3 `POST /api/intensure/v1/port-events`（保障侧只读，这条是反向——采集内部用，但保障也用来订阅事件流）

> 实际上这条是 intensure 内部 record，但在 Phase 1 简化时，可以由 integration 提供事件流给保障。

**改用事件流方式**（更现代）：
- **保障模块订阅** `intensure.events`（Server-Sent Events / WebSocket）
- intensure 推送 `port_link_status`、`link_up`、`link_down` 等事件
- 保障实时更新 `intent_states`，无需轮询

**Phase 1 简化版**（先用轮询）：
- 保障每 5s 调 `GET /state/current`，对比 `version` 字段判断"是否有更新"
- 有更新 → 重跑一致性检查

**Phase 2 升级**：SSE/WebSocket 事件流

---

## 5. 完整跨模块 API 矩阵

### 5.1 Intensure 模块（采集层）

| 方法 | 路径 | 用途 | 调用方 |
|---|---|---|---|
| GET | `/api/intensure/v1/state/current` | 当前状态快照 | 保障 |
| GET | `/api/intensure/v1/state/history?since=...&until=...&limit=N` | 历史状态（分页） | 保障、运营 |
| GET | `/api/intensure/v1/port-events?dpid=...&since=...` | 端口事件流 | 保障 |
| GET | `/api/intensure/v1/link-events?since=...` | 链路事件流 | 保障 |
| POST | `/api/intensure/v1/probe-results` | live_env 提交探测结果 | 内置 |

### 5.2 Translation 模块（翻译层）

| 方法 | 路径 | 用途 | 调用方 |
|---|---|---|---|
| GET | `/api/translation/v1/templates` | 意图模板列表 | 保障、实现 |
| GET | `/api/translation/v1/parsed?status=ready&since=...` | 待部署意图（实现读） | 实现 |
| POST | `/api/translation/v1/parse` | NL → JSON 解析入口 | 用户/前端 |
| POST | `/api/translation/v1/parsed/{id}/mark-deployed` | 实现部署完标记 | 实现 |

### 5.3 Implementation 模块（实现层）⭐ 含"防御纵深"示例

| 方法 | 路径 | 用途 | 调用方 |
|---|---|---|---|
| GET | `/api/implementation/v1/policies/active` | 当前有效策略 | 翻译、保障 |
| GET | `/api/implementation/v1/policies/{id}` | 单个策略详情 | 翻译、保障 |
| POST | `/api/implementation/v1/policies/{id}/retire` | **退役策略**（语义化操作） | 翻译、保障 |
| POST | `/api/implementation/v1/deployments` | 触发部署（传入 parsed_intent_id） | 翻译 |
| POST | `/api/implementation/v1/deployments/{id}/rollback` | 回滚部署 | 保障（自愈后验证）、用户 |
| POST | `/api/implementation/v1/resolutions` | **翻译提交消解方案**（不是 DELETE 表！）| 翻译 |

> ⭐ **关键示例**：
> 翻译模块想"删除已部署规则" → 调用 `POST /policies/{id}/retire`（body 写理由）
> → 实现模块内部校验（是否冲突 / 影响线上）→ **由实现模块自己**UPDATE 表的 `status='retired', effective_to=now()`
> → 翻译模块从来不直接 DELETE `implementation.policies`

### 5.4 Assurance 模块（保障层）

| 方法 | 路径 | 用途 | 调用方 |
|---|---|---|---|
| POST | `/api/assurance/v1/intents/register` | 注册意图 | 用户/翻译（注册后才需保障） |
| POST | `/api/assurance/v1/intents/{intent_id}/activate` | 激活意图 | 实现（部署成功后） |
| GET | `/api/assurance/v1/intents/{intent_id}/status` | 当前状态 | 用户/所有模块 |
| GET | `/api/assurance/v1/checks?intent_id=...&since=...&limit=50` | 检查历史 | 用户/调试 |
| GET | `/api/assurance/v1/diagnoses?intent_id=...&since=...` | 诊断历史 | 用户/翻译 |
| POST | `/api/assurance/v1/diagnoses` | 写入诊断（保障自己调） | 内置 |
| POST | `/api/assurance/v1/healings` | 写入自愈意图 | 内置 |
| POST | `/api/assurance/v1/healings/{id}/execute` | 触发自愈执行 | 运营（手动） |

### 5.5 Shared 模块（公共）

| 方法 | 路径 | 用途 | 调用方 |
|---|---|---|---|
| POST | `/api/shared/v1/audit` | 写审计（append-only） | 所有模块 |
| GET | `/api/shared/v1/health/modules` | 查看所有模块心跳 | 用户/监控 |
| POST | `/api/shared/v1/health/heartbeat` | 提交心跳 | 所有模块（自调用） |
| GET | `/api/shared/v1/schema/version` | 当前 schema 版本 | 部署工具 |

---

## 6. 鉴权（Phase 1 → Phase 2）

### Phase 1（开发期）
- 每个模块用 DB 用户密码（见 `access-control-matrix.md`）
- HTTP API：暂时信任内网（无 token）
- **前提**：所有模块跑在内网/VPN 内

### Phase 2（生产化）
- DB 用户密码 → secret store（HashiCorp Vault / Kubernetes Secret）
- HTTP API：`mTLS`（模块间证书）+ `JWT`（用户调用）
- **前提**：已拆分到独立 DB/独立部署

---

## 7. 版本管理

- 路径带 `v{N}`：大版本升级（破坏性变更）
- v1 内部小改：向后兼容（增字段、可选参数）
- 模块的 API 版本独立：intensure v1 改了不影响 implementation v2
- OpenAPI 3 spec 锁版本：`docs/api/openapi-v1.yaml`（Phase 1 生成）

---

## 8. 你提的核心约束 → API 契约层落地

| 你的约束 | API 体现 | DB 体现 |
|---|---|---|
| 翻译可查有效规则 | `GET /implementation/v1/policies/active` | `translation_user` 有 SELECT |
| 翻译不能因消解方案就删已部署规则 | 只能 `POST /implementation/v1/policies/{id}/retire`（不是 DELETE） | `translation_user` 对 policies 无 UPDATE/DELETE 权限 |
| 翻译不能改他人表 | 翻译无 intensure/assurance 写权限 | `translation_user` 仅对 translation.* 有 RW |

**两层防护**：API 层（路径/动词）+ DB 层（角色）—— 即使 API 漏了，DB 也兜得住。

---

## 9. Phase 1 实现的 3+1 优先级

1. **必须**：保障的 `GET /state/current` 和 `GET /policies/active`（MVP 闭环的输入端）
2. **必须**：保障的 `POST /diagnoses` 和 `POST /healings`（MVP 闭环的输出端）
3. **必须**：intensure 的 `POST /probe-results`（live_env 写入）
4. **建议**：shared 的 `POST /audit` 和 `POST /health/heartbeat`（观测性）

其他 API Phase 2+ 补。

---

## 10. 待办

- [ ] Phase 1：生成 OpenAPI 3 YAML spec（`docs/api/openapi.yaml`）
- [ ] Phase 1：用 `connexion`（Python Flask）或类似工具从 spec 生成 stub
- [ ] Phase 2：mTLS 证书管理流程

---

_文档创建：2026-10-08 · v0.1 · Phase 0 交付_