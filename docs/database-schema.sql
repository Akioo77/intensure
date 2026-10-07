-- ============================================================================
-- 数据库多模块化架构 · 完整 DDL
-- ============================================================================
-- 文件：docs/database-schema.sql
-- 版本：v0.1 (2026-10-08)
-- 目标：意图驱动网络管理系统 4 智能体共享 PostgreSQL
--       - intensure（采集）/ translation（翻译）/ implementation（实现）
--         / assurance（保障）/ shared（公共）
--       - 重点：保障模块高频读路径优化
--
-- 使用：
--   1) sudo -u postgres psql -f docs/database-schema.sql
--   2) 见 docs/access-control-matrix.md 执行 GRANT 脚本
--
-- 设计原则：
--   - Schema-per-module（不是 database-per-module），后期可拆分
--   - id = BIGSERIAL（内部）+ uuid（外部），跨模块引用用 uuid
--   - 时间戳全用 TIMESTAMPTZ（aware UTC），避免时区坑
--   - 高频写入表按月分区（state_history/consistency_checks/healing_history/probe_results/audit_log）
--   - 关键路径用复合索引、单行表用单行 upsert
-- ============================================================================

-- 清理（仅首次创建时；正式环境不要 uncomment）
-- DROP SCHEMA IF EXISTS intensure CASCADE;
-- DROP SCHEMA IF EXISTS translation CASCADE;
-- DROP SCHEMA IF EXISTS implementation CASCADE;
-- DROP SCHEMA IF EXISTS assurance CASCADE;
-- DROP SCHEMA IF EXISTS assurance_mv CASCADE;
-- DROP SCHEMA IF EXISTS shared CASCADE;

-- ============================================================================
-- 0. 扩展 + Schema 命名空间
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- gen_random_uuid() 替代上者更现代

CREATE SCHEMA IF NOT EXISTS intensure       AUTHORIZATION postgres;
CREATE SCHEMA IF NOT EXISTS translation     AUTHORIZATION postgres;
CREATE SCHEMA IF NOT EXISTS implementation  AUTHORIZATION postgres;
CREATE SCHEMA IF NOT EXISTS assurance       AUTHORIZATION postgres;
CREATE SCHEMA IF NOT EXISTS assurance_mv    AUTHORIZATION postgres;   -- 保障物化视图
CREATE SCHEMA IF NOT EXISTS shared          AUTHORIZATION postgres;

COMMENT ON SCHEMA intensure      IS '采集层：网络状态 + 事件流 + 探测结果';
COMMENT ON SCHEMA translation    IS '翻译层：意图模板 + 解析结果';
COMMENT ON SCHEMA implementation IS '实现层：已部署策略（权威）+ 部署日志';
COMMENT ON SCHEMA assurance      IS '保障层：一致性 + 诊断 + 自愈';
COMMENT ON SCHEMA assurance_mv   IS '保障层物化视图：提升高频读路径性能';
COMMENT ON SCHEMA shared         IS '跨模块：审计/健康/schema 版本（append-only）';

-- ============================================================================
-- 1. SHARED schema（公共，append-only）
-- ============================================================================

CREATE TABLE shared.schema_version (
    version         TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by      TEXT NOT NULL
);

COMMENT ON TABLE shared.schema_version IS 'schema 版本追踪（migration tool 写入）';

CREATE TABLE shared.module_health (
    module_name     TEXT PRIMARY KEY,         -- 'intensure' / 'translation' / 'implementation' / 'assurance'
    last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'healthy',  -- healthy / degraded / down
    version         TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (status IN ('unknown', 'healthy', 'degraded', 'down'))
);

COMMENT ON TABLE shared.module_health IS '各模块心跳（自己 UPDATE 自己的行）';

-- 审计日志：append-only，分区；severity 决定保留时长
CREATE TABLE shared.audit_log (
    id              BIGSERIAL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    module_name     TEXT NOT NULL,
    actor           TEXT NOT NULL,            -- user / system / cross-module-call
    action          TEXT NOT NULL,            -- 'policy.create' / 'state.write' ...
    target          TEXT,                     -- 资源 ID / 表名
    severity        TEXT NOT NULL DEFAULT 'INFO',  -- 'INFO' / 'WARN' / 'ERROR'（ERROR 永久保留）
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id  UUID,                     -- 关联同一次请求的多次日志
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

COMMENT ON TABLE shared.audit_log IS '跨模块审计（append-only，无 UPDATE/DELETE 权限）';

-- 初始审计分区（覆盖近 3 个月）
CREATE TABLE shared.audit_log_2026_10 PARTITION OF shared.audit_log
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE shared.audit_log_2026_11 PARTITION OF shared.audit_log
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE shared.audit_log_2026_12 PARTITION OF shared.audit_log
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

CREATE INDEX idx_audit_log_module_time ON shared.audit_log (module_name, occurred_at DESC);
CREATE INDEX idx_audit_log_correlation ON shared.audit_log (correlation_id) WHERE correlation_id IS NOT NULL;

-- ============================================================================
-- 2. INTENSURE schema（采集层，"只写不读"传感器）
-- ============================================================================

-- 当前状态（单行 upsert；保障模块每 5s 读这个）
-- 设计为"永远是 1 行"，key 用固定 'current'；避免 count(*) + 多行筛选
CREATE TABLE intensure.network_state (
    state_key       TEXT PRIMARY KEY DEFAULT 'current' CHECK (state_key = 'current'),
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL,           -- switches/hosts/links/flows 全量
    version         BIGINT NOT NULL,          -- 乐观锁，每次采集 +1
    updated_by      TEXT NOT NULL DEFAULT 'intensure'
);

COMMENT ON TABLE intensure.network_state IS '当前网络状态单行快照（保障模块每 5s 读）';
COMMENT ON COLUMN intensure.network_state.version IS '乐观锁版本号（采集每次 +1）';

-- 历史快照（分区）
CREATE TABLE intensure.state_history (
    id              BIGSERIAL,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload         JSONB NOT NULL,
    summary         JSONB NOT NULL DEFAULT '{}',  -- {switches:2, hosts:4, down_links:1}
    capture_source  TEXT NOT NULL DEFAULT 'osken-controller',
    capture_reason  TEXT NOT NULL DEFAULT 'scheduled',  -- 'scheduled' / 'on-change'
    PRIMARY KEY (id, snapshot_at)
) PARTITION BY RANGE (snapshot_at);

COMMENT ON TABLE intensure.state_history IS '历史状态快照（环形缓冲替代品）';

-- 初始分区
CREATE TABLE intensure.state_history_2026_10 PARTITION OF intensure.state_history
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE intensure.state_history_2026_11 PARTITION OF intensure.state_history
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE intensure.state_history_2026_12 PARTITION OF intensure.state_history
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- 端口事件
CREATE TABLE intensure.port_events (
    id              BIGSERIAL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    dpid            TEXT NOT NULL,            -- '0000000000000001'
    port_no         INTEGER NOT NULL,
    port_name       TEXT,                     -- 's1-eth2'
    event_type      TEXT NOT NULL,            -- 'link_up' / 'link_down' / 'add' / 'delete'
    speed_mbps      INTEGER,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE intensure.port_events_2026_10 PARTITION OF intensure.port_events
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE intensure.port_events_2026_11 PARTITION OF intensure.port_events
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE intensure.port_events_2026_12 PARTITION OF intensure.port_events
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

CREATE INDEX idx_port_events_dpid_time ON intensure.port_events (dpid, port_no, occurred_at DESC);

-- 链路事件
CREATE TABLE intensure.link_events (
    id              BIGSERIAL,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    src_dpid        TEXT NOT NULL,
    src_port        INTEGER NOT NULL,
    dst_dpid        TEXT NOT NULL,
    dst_port        INTEGER NOT NULL,
    event_type      TEXT NOT NULL,            -- 'link_up' / 'link_down'
    latency_us      INTEGER,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE intensure.link_events_2026_10 PARTITION OF intensure.link_events
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE intensure.link_events_2026_11 PARTITION OF intensure.link_events
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE intensure.link_events_2026_12 PARTITION OF intensure.link_events
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

CREATE INDEX idx_link_events_src ON intensure.link_events (src_dpid, src_port, occurred_at DESC);
CREATE INDEX idx_link_events_dst ON intensure.link_events (dst_dpid, dst_port, occurred_at DESC);

-- 流表快照
CREATE TABLE intensure.flows_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    dpid            TEXT NOT NULL,
    flow_count      INTEGER NOT NULL,
    flows           JSONB NOT NULL,           -- OVS-ofctl dump 原始 JSON
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_flows_snapshot_dpid_time ON intensure.flows_snapshot (dpid, captured_at DESC);

-- 探测结果（live_env 写入）
CREATE TABLE intensure.probe_results (
    id              BIGSERIAL,
    probed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    src_host        TEXT NOT NULL,
    dst_host        TEXT NOT NULL,
    reachable       BOOLEAN NOT NULL,
    latency_ms      NUMERIC(8,3),
    packet_loss     NUMERIC(5,4) NOT NULL DEFAULT 0,
    probe_method    TEXT NOT NULL DEFAULT 'ping',   -- 'ping' / 'tcp' / 'udp'
    is_anomaly      BOOLEAN NOT NULL DEFAULT FALSE, -- 选择性写库标记（异常=true）
    anomaly_type    TEXT,                            -- 'reachability_change'/'high_loss'/'high_latency'
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, probed_at)
) PARTITION BY RANGE (probed_at);

CREATE TABLE intensure.probe_results_2026_10 PARTITION OF intensure.probe_results
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE intensure.probe_results_2026_11 PARTITION OF intensure.probe_results
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE intensure.probe_results_2026_12 PARTITION OF intensure.probe_results
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

CREATE INDEX idx_probe_results_src_time ON intensure.probe_results (src_host, probed_at DESC);
CREATE INDEX idx_probe_results_dst_time ON intensure.probe_results (dst_host, probed_at DESC);

-- ============================================================================
-- 3. TRANSLATION schema（翻译层）
-- ============================================================================

-- 意图模板
CREATE TABLE translation.intent_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_key    TEXT UNIQUE NOT NULL,      -- 'access_control_v1'
    name            TEXT NOT NULL,
    intent_type     TEXT NOT NULL,             -- 'ACCESS_CONTROL' / 'QOS' ...
    schema_json     JSONB NOT NULL,            -- 意图字段定义（slot 定义）
    examples        JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deprecated_at   TIMESTAMPTZ
);

CREATE INDEX idx_intent_templates_type ON translation.intent_templates (intent_type) WHERE deprecated_at IS NULL;

-- 解析结果（NL → JSON）
CREATE TABLE translation.parsed_intents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key     TEXT UNIQUE NOT NULL,  -- 客户端提供，防重复
    raw_nl              TEXT NOT NULL,         -- 原始自然语言
    parsed_json         JSONB NOT NULL,        -- 解析结果
    template_id         UUID REFERENCES translation.intent_templates(id),
    confidence          NUMERIC(4,3) NOT NULL DEFAULT 1.000,
    status              TEXT NOT NULL DEFAULT 'parsed',  -- parsed / ready / deployed / failed / superseded
    failure_reason      TEXT,
    parsed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at            TIMESTAMPTZ,
    CHECK (status IN ('parsed', 'ready', 'deployed', 'failed', 'superseded'))
);

COMMENT ON TABLE translation.parsed_intents IS 'NL 解析结果（实现模块读取 status=ready 的部署）';

CREATE INDEX idx_parsed_intents_status_time ON translation.parsed_intents (status, parsed_at) WHERE status = 'ready';
CREATE INDEX idx_parsed_intents_template ON translation.parsed_intents (template_id);

-- 翻译审计
CREATE TABLE translation.translation_audit (
    id              BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    parsed_intent_id UUID REFERENCES translation.parsed_intents(id),
    raw_nl          TEXT NOT NULL,
    matched_rules   JSONB NOT NULL DEFAULT '[]'::jsonb,
    chosen_template TEXT,
    confidence      NUMERIC(4,3),
    duration_ms     INTEGER,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_translation_audit_time ON translation.translation_audit (occurred_at DESC);

-- ============================================================================
-- 4. IMPLEMENTATION schema（实现层，**权威表**）
-- ============================================================================

-- 已部署策略（核心权威）
CREATE TABLE implementation.policies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id           TEXT NOT NULL,         -- 业务意图 ID（如 'live-AC-003'）
    parsed_intent_id    UUID REFERENCES translation.parsed_intents(id),
    intent_type         TEXT NOT NULL,         -- 'ACCESS_CONTROL' / 'QOS' ...
    policy_spec         JSONB NOT NULL,        -- 策略规格
    status              TEXT NOT NULL DEFAULT 'active',  -- active / retired / failed / superseded
    effective_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
    effective_to        TIMESTAMPTZ,           -- NULL = 当前生效
    deployed_by         TEXT NOT NULL,         -- 哪个 deployment 部署的
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (status IN ('active', 'retired', 'failed', 'superseded'))
);

COMMENT ON TABLE implementation.policies IS '已部署策略（**唯一权威**——线上是什么）';
COMMENT ON COLUMN implementation.policies.effective_to IS 'NULL = 当前生效；retired 时填时间';

-- 关键索引：保障模块高频查
CREATE INDEX idx_policies_active ON implementation.policies (intent_type, status, effective_from DESC)
    WHERE status = 'active';
CREATE INDEX idx_policies_intent_id ON implementation.policies (intent_id);
CREATE INDEX idx_policies_effective ON implementation.policies (effective_from DESC, effective_to);

-- 策略版本（用于回滚/审计；不需要每次读，但故障排查重要）
CREATE TABLE implementation.policy_versions (
    id              BIGSERIAL PRIMARY KEY,
    policy_id       UUID NOT NULL REFERENCES implementation.policies(id),
    version         INTEGER NOT NULL,
    policy_spec     JSONB NOT NULL,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by      TEXT NOT NULL,
    change_reason   TEXT,
    UNIQUE (policy_id, version)
);

CREATE INDEX idx_policy_versions_policy ON implementation.policy_versions (policy_id, version DESC);

-- 部署记录
CREATE TABLE implementation.deployments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deployment_key      TEXT UNIQUE NOT NULL,
    parsed_intent_id    UUID REFERENCES translation.parsed_intents(id),
    target_device       TEXT NOT NULL,
    deployment_method   TEXT NOT NULL,         -- 'cli' / 'netconf' / 'restconf'
    deployment_status   TEXT NOT NULL DEFAULT 'pending',  -- pending/in_progress/success/failed/rolled_back
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    duration_ms         INTEGER,
    error_message       TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (deployment_status IN ('pending', 'in_progress', 'success', 'failed', 'rolled_back'))
);

CREATE INDEX idx_deployments_status_time ON implementation.deployments (deployment_status, started_at DESC);
CREATE INDEX idx_deployments_parsed ON implementation.deployments (parsed_intent_id);

-- 部署日志（CLI/NETCONF 原始输出）
CREATE TABLE implementation.deployment_logs (
    id              BIGSERIAL PRIMARY KEY,
    deployment_id   UUID NOT NULL REFERENCES implementation.deployments(id) ON DELETE CASCADE,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    log_level       TEXT NOT NULL DEFAULT 'info',  -- debug/info/warn/error
    message         TEXT NOT NULL,
    raw_output      TEXT
);

CREATE INDEX idx_deployment_logs_deploy ON implementation.deployment_logs (deployment_id, logged_at);

-- 回滚记录
CREATE TABLE implementation.rollback_history (
    id              BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    policy_id       UUID NOT NULL REFERENCES implementation.policies(id),
    rollback_reason TEXT NOT NULL,
    triggered_by    TEXT NOT NULL,             -- 'user' / 'assurance-alarm' / 'auto-timeout'
    success         BOOLEAN NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_rollback_history_policy ON implementation.rollback_history (policy_id, occurred_at DESC);

-- ============================================================================
-- 5. ASSURANCE schema（保障层，**优先优化**）
-- ============================================================================

-- 意图状态机轨迹（保障模块每 5s 更新一次）
CREATE TABLE assurance.intent_states (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id       TEXT NOT NULL,             -- 业务意图 ID（与 policies.intent_id 对齐）
    current_status  TEXT NOT NULL,             -- REGISTERED/ACTIVE/VIOLATED/DIAGNOSING/HEALING/RECOVERED/FAILED
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_status_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (current_status IN ('REGISTERED', 'ACTIVE', 'VIOLATED', 'DIAGNOSING',
                              'HEALING', 'VERIFYING', 'RECOVERED', 'FAILED', 'RETIRED'))
);

COMMENT ON TABLE assurance.intent_states IS '意图状态机当前快照';

CREATE INDEX idx_intent_states_intent ON assurance.intent_states (intent_id, last_status_at DESC);

-- 一致性检查（保障模块高频写；**选择性**：状态变化时 + 每 60s 心跳）
CREATE TABLE assurance.consistency_checks (
    id              BIGSERIAL,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    intent_id       TEXT NOT NULL,
    check_type      TEXT NOT NULL DEFAULT 'reachability',  -- reachability/policy/flow
    expected_state  JSONB NOT NULL,            -- {flow_present:true, src:..., dst:..., latency_max_ms:50}
    actual_state    JSONB NOT NULL,            -- {reachable:true, latency_ms:12, ...}
    result          TEXT NOT NULL,             -- 'pass' / 'violated' / 'error'
    violation_types TEXT[],                    -- ['REACHABILITY_FAILURE', 'POLICY_DRIFT', ...]
    evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- down_ports/down_links/conflicting_flows
    is_heartbeat    BOOLEAN NOT NULL DEFAULT FALSE, -- 选择性写库标记（心跳=true）
    duration_ms     INTEGER,
    PRIMARY KEY (id, checked_at)
) PARTITION BY RANGE (checked_at);

COMMENT ON TABLE assurance.consistency_checks IS '一致性检查结果（高频写、保障核心表）';

-- 初始分区
CREATE TABLE assurance.consistency_checks_2026_10 PARTITION OF assurance.consistency_checks
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE assurance.consistency_checks_2026_11 PARTITION OF assurance.consistency_checks
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE assurance.consistency_checks_2026_12 PARTITION OF assurance.consistency_checks
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

-- **关键复合索引**（保障查"最近 N 次"）
CREATE INDEX idx_consistency_checks_intent_time ON assurance.consistency_checks (intent_id, checked_at DESC);
CREATE INDEX idx_consistency_checks_result_time ON assurance.consistency_checks (result, checked_at DESC)
    WHERE result = 'violated';
-- 部分索引：违规的（合规的查得少）
CREATE INDEX idx_consistency_checks_violation_types ON assurance.consistency_checks USING GIN (violation_types);

-- 根因诊断（含证据 + 置信度 + 影响设备）
CREATE TABLE assurance.diagnoses (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    diagnosed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    intent_id           TEXT NOT NULL,
    consistency_check_id BIGINT,                -- 可关联 consistency_checks（跨分区，存 id 不存 FK）
    checked_at_ref      TIMESTAMPTZ,            -- 一致性检查的时间（用于跨分区关联）
    root_causes         JSONB NOT NULL,         -- [{type, confidence, candidate_reasons: [...]}]
    confidence          NUMERIC(4,3) NOT NULL,
    affected_devices    TEXT[] NOT NULL DEFAULT '{}',  -- ['s1', 's2']
    evidence          JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {down_ports, down_links, conflicting_flows}
    diagnosis_status    TEXT NOT NULL DEFAULT 'completed',  -- pending/completed/failed
    error_message       TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (diagnosis_status IN ('pending', 'completed', 'failed')),
    CHECK (confidence >= 0 AND confidence <= 1)
);

COMMENT ON TABLE assurance.diagnoses IS '根因诊断（**关键：含 evidence/confidence/affected_devices**）';
COMMENT ON COLUMN assurance.diagnoses.evidence IS '09-19 联调发现：空 evidence → Guard BLOCKED；真实 evidence → APPROVED';

CREATE INDEX idx_diagnoses_intent_time ON assurance.diagnoses (intent_id, diagnosed_at DESC);
CREATE INDEX idx_diagnoses_devices ON assurance.diagnoses USING GIN (affected_devices);
CREATE INDEX idx_diagnoses_confidence ON assurance.diagnoses (confidence) WHERE confidence < 0.5;  -- 低置信度诊断

-- 自愈意图（Guard 校验后）
CREATE TABLE assurance.healing_intents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    intent_id           TEXT NOT NULL,
    diagnosis_id        UUID REFERENCES assurance.diagnoses(id),
    parent_intent_id    TEXT,                   -- 关联原始意图
    strategy            TEXT NOT NULL,          -- 'reroute_around_failure' / 'reset_port' ...
    primitives          JSONB NOT NULL,         -- 自愈原子操作
    risk                TEXT NOT NULL DEFAULT 'medium',  -- low/medium/high/critical
    guard_checks        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {G1:pass, G2:pass, G3:fail_reason, ...}
    verdict             TEXT NOT NULL,          -- 'APPROVED' / 'BLOCKED' / 'PENDING'
    blocked_by          TEXT[],                 -- ['G3:scope_devices_empty', ...]
    ttl_seconds         INTEGER NOT NULL DEFAULT 120,
    executed_at         TIMESTAMPTZ,
    execution_result    TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (verdict IN ('APPROVED', 'BLOCKED', 'PENDING'))
);

COMMENT ON TABLE assurance.healing_intents IS '自愈意图（含 Guard 14 项校验结果）';
COMMENT ON COLUMN assurance.healing_intents.verdict IS 'APPROVED=可执行 / BLOCKED=被 Guard 拒绝 / PENDING=待审';

CREATE INDEX idx_healing_intents_intent_time ON assurance.healing_intents (intent_id, created_at DESC);
CREATE INDEX idx_healing_intents_verdict_time ON assurance.healing_intents (verdict, created_at DESC)
    WHERE verdict = 'APPROVED';
CREATE INDEX idx_healing_intents_diagnosis ON assurance.healing_intents (diagnosis_id);

-- 自愈执行历史（审计）
CREATE TABLE assurance.healing_history (
    id              BIGSERIAL,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    healing_intent_id UUID NOT NULL REFERENCES assurance.healing_intents(id),
    intent_id       TEXT NOT NULL,
    success         BOOLEAN NOT NULL,
    duration_ms     INTEGER,
    error_message   TEXT,
    recovery_check_id BIGINT,                   -- 关联到 consistency_checks
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, executed_at)
) PARTITION BY RANGE (executed_at);

CREATE TABLE assurance.healing_history_2026_10 PARTITION OF assurance.healing_history
    FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE assurance.healing_history_2026_11 PARTITION OF assurance.healing_history
    FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE assurance.healing_history_2026_12 PARTITION OF assurance.healing_history
    FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

CREATE INDEX idx_healing_history_intent_time ON assurance.healing_history (intent_id, executed_at DESC);

-- ============================================================================
-- 6. ASSURANCE_MV schema（保障层物化视图，提升读路径性能）
-- ============================================================================

-- 当前"意图状态全景"（保障仪表盘/一致性检查快速查询）
-- 替代每次 JOIN intensure.network_state + implementation.policies
CREATE MATERIALIZED VIEW assurance_mv.intent_status AS
SELECT
    p.intent_id,
    p.intent_type,
    p.policy_spec,
    p.status              AS policy_status,
    p.effective_from,
    p.effective_to,
    s.captured_at         AS state_captured_at,
    s.payload             AS state_payload,
    s.version             AS state_version,
    st.current_status     AS intent_state,
    st.last_checked_at
FROM implementation.policies p
LEFT JOIN intensure.network_state s ON s.state_key = 'current'
LEFT JOIN assurance.intent_states st ON st.intent_id = p.intent_id
WHERE p.status = 'active';

COMMENT ON MATERIALIZED VIEW assurance_mv.intent_status IS '意图状态全景（保障模块查询首选）';

CREATE UNIQUE INDEX idx_intent_status_pk ON assurance_mv.intent_status (intent_id);
CREATE INDEX idx_intent_status_type ON assurance_mv.intent_status (intent_type);

-- ============================================================================
-- 7. 初始数据 + 权限占位（完整 GRANT 见 access-control-matrix.md）
-- ============================================================================

-- 插入初始 schema 版本
INSERT INTO shared.schema_version (version, description, applied_by)
VALUES ('v0.1', '初始 5 schema + 21 张表 + 1 物化视图', '庄英琪')
ON CONFLICT (version) DO NOTHING;

-- 初始化 4 模块心跳
INSERT INTO shared.module_health (module_name, status) VALUES
    ('intensure', 'unknown'),
    ('translation', 'unknown'),
    ('implementation', 'unknown'),
    ('assurance', 'unknown')
ON CONFLICT (module_name) DO NOTHING;

-- ============================================================================
-- 8. 验证（运行后可以看到表清单）
-- ============================================================================

-- SELECT table_schema, table_name, table_type
-- FROM information_schema.tables
-- WHERE table_schema IN ('intensure', 'translation', 'implementation', 'assurance', 'assurance_mv', 'shared')
-- ORDER BY table_schema, table_name;

-- ============================================================================
-- 文件结束
-- ============================================================================