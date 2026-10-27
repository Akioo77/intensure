"""baseline — intensure 全量 schema（首个基线）

**生成方式**: 由 `db_maintenance/gen_baseline.py` 从 `docs/database-schema.sql`
自动生成，不是手抄 —— 保证基线与规范文件一致。

**分区说明**: 原 schema 文件把月分区写死成 2026_10/11/12，到 2027 年 1 月建新库
就没有当月分区，任何 INSERT 都会报 `no partition of relation ... found for row`。
本基线改为**按「当前月 + 0..2」动态创建**，新库任何时候建出来都覆盖当月。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-27
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


SCHEMA_SQL = r"""-- ============================================================================
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
-- (原硬编码月分区已由下方动态分区块取代)

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
-- (原硬编码月分区已由下方动态分区块取代)

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

-- (原硬编码月分区已由下方动态分区块取代)

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

-- (原硬编码月分区已由下方动态分区块取代)

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

-- (原硬编码月分区已由下方动态分区块取代)

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
    intent_id       TEXT NOT NULL UNIQUE,      -- 业务意图 ID（与 policies.intent_id 对齐）— 保证一个 intent 表达上定 1 行
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
-- (原硬编码月分区已由下方动态分区块取代)

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

COMMENT ON TABLE assurance.diagnoses IS
    '[我方·本地 mock 专用] 违例诊断记录。联调/生产模式我方不写此表'
    '(见 docs/partner_合并文档.md 第二节), 诊断数据以伙伴的 assurance.diagnosis_results 为准。'
    '开关: ASSURANCE_WRITE_DIAGNOSES=0';
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
    -- ---- 按伙伴反馈补齐（2026-10-26）----
    status              TEXT NOT NULL DEFAULT 'pending',
    priority            INTEGER NOT NULL DEFAULT 100,
    affected_devices    TEXT[] NOT NULL DEFAULT '{}'::text[],
    expires_at          TIMESTAMPTZ,
    CHECK (verdict IN ('APPROVED', 'BLOCKED', 'PENDING')),
    CONSTRAINT healing_intents_status_check
        CHECK (status IN ('pending', 'approved', 'rejected', 'executed', 'failed'))
);

COMMENT ON TABLE assurance.healing_intents IS '自愈意图（含 Guard 14 项校验结果）';
COMMENT ON COLUMN assurance.healing_intents.verdict IS 'APPROVED=可执行 / BLOCKED=被 Guard 拒绝 / PENDING=待审';

CREATE INDEX idx_healing_intents_intent_time ON assurance.healing_intents (intent_id, created_at DESC);
CREATE INDEX idx_healing_intents_verdict_time ON assurance.healing_intents (verdict, created_at DESC)
    WHERE verdict = 'APPROVED';
CREATE INDEX idx_healing_intents_diagnosis ON assurance.healing_intents (diagnosis_id);

-- 队列扫描：取待执行自愈意图
CREATE INDEX idx_healing_intents_queue
    ON assurance.healing_intents (status, priority, created_at)
    WHERE status IN ('pending', 'approved');
-- 过期清扫
CREATE INDEX idx_healing_intents_expires
    ON assurance.healing_intents (expires_at)
    WHERE expires_at IS NOT NULL;

COMMENT ON COLUMN assurance.healing_intents.status IS
    '信封生命周期: pending/approved/rejected/executed/failed（verdict 是 Guard 判定，两者不同）';
COMMENT ON COLUMN assurance.healing_intents.priority IS '自愈队列排序，数值越小越先执行';
COMMENT ON COLUMN assurance.healing_intents.affected_devices IS '受影响设备（Guard G3 范围安全检查用）';
COMMENT ON COLUMN assurance.healing_intents.expires_at IS '信封过期时间（通常是 created_at + ttl_seconds）';

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
    -- ---- 按伙伴反馈补齐（2026-10-26）----
    result_details  JSONB NOT NULL DEFAULT '{}'::jsonb,
    verified        BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (id, executed_at)
) PARTITION BY RANGE (executed_at);

-- ============================================================================
-- 动态月分区（替代硬编码月份）
--
-- 为什么不用硬编码: 原文件写死 2026_10/11/12，到 2027 年 1 月建新库就没有
-- 当月分区，任何 INSERT 都会报
--   ERROR: no partition of relation "..." found for row
-- 现在按「当前月 + 0..2」动态创建，新库任何时候建出来都覆盖当月。
-- 已存在的分区不会重复创建（IF NOT EXISTS）。
--
-- ⚠️ schema 和表名必须拆开用 %I.%I 拼:
--   format('%I', 'assurance.consistency_checks') 得到 "assurance.consistency_checks"
--   —— 这是**一个名字里带点的标识符**, PG 当成普通表名而不是 schema.table,
--   报 relation "assurance.consistency_checks" does not exist。
-- ============================================================================
DO $$
DECLARE
    t           TEXT;
    base_month  DATE := date_trunc('month', now())::date;
    i           INT;
    pname       TEXT;
    pschema     TEXT;
    ptable      TEXT;
    pstart      DATE;
    pend        DATE;
BEGIN
    FOREACH t IN ARRAY ARRAY[
'assurance.consistency_checks',
        'assurance.healing_history',
        'intensure.link_events',
        'intensure.port_events',
        'intensure.probe_results',
        'intensure.state_history',
        'shared.audit_log'
    ] LOOP
        FOR i IN 0..2 LOOP
            pstart := (base_month + (i || ' month')::interval)::date;
            pend   := (pstart + '1 month'::interval)::date;
            pschema := split_part(t, '.', 1);
            ptable  := split_part(t, '.', 2);
            pname   := ptable || '_' || to_char(pstart, 'YYYY_MM');
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I.%I PARTITION OF %I.%I '
                'FOR VALUES FROM (%L) TO (%L)',
                pschema, pname, pschema, ptable, pstart, pend);
        END LOOP;
    END LOOP;
END $$;
CREATE INDEX idx_healing_history_intent_time ON assurance.healing_history (intent_id, executed_at DESC);

COMMENT ON COLUMN assurance.healing_history.result_details IS '执行结果明细（伙伴需要）';
COMMENT ON COLUMN assurance.healing_history.verified IS '恢复是否已独立校验通过（伙伴需要）';

-- ============================================================================
-- 5.1 伙伴新增的 9 张保障表（2026-10-26 由伙伴 assurance_schema.sql 落地）
--
-- 说明:
--   - 这 9 张表与伙伴的 SQL 脚本逐字对应, 已实测落地
--   - 伙伴脚本里的 healing_intents / healing_history 因结构冲突未采用,
--     仍用本文件第 5 节的版本（我方结构 + 已加的 4 个字段）
--   - 权限: 见 access-control-grants.sql（对 9 张新表单独授权）
-- ============================================================================

CREATE TABLE assurance._metadata (
    key character varying(128) NOT NULL,
    value jsonb NOT NULL
);
COMMENT ON TABLE assurance._metadata IS '全局配置元数据，存储 healing_queue 等 JSON blob';
CREATE TABLE assurance.assurance_records (
    intent_id character varying(64) NOT NULL,
    data jsonb NOT NULL,
    status character varying(32) NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL
);
COMMENT ON TABLE assurance.assurance_records IS '保障记录主表，存储完整的 AssuranceRecord';
COMMENT ON COLUMN assurance.assurance_records.data IS '完整的 AssuranceRecord JSON，包含 intent/diagnosis/healing 等所有信息';
CREATE TABLE assurance.audit_logs (
    log_id integer NOT NULL,
    intent_id character varying(64),
    event_type character varying(64) NOT NULL,
    event_data jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);
COMMENT ON TABLE assurance.audit_logs IS '审计日志，记录所有状态变更和关键操作';
CREATE TABLE assurance.case_records (
    case_id character varying(64) NOT NULL,
    fault_id character varying(64) NOT NULL,
    title character varying(256) NOT NULL,
    description text,
    violation_type character varying(64),
    actual_state jsonb,
    expected_root_cause text,
    expected_confidence double precision,
    embedding_text text,
    embedding jsonb,
    embedding_model character varying(64) DEFAULT 'bge-m3'::character varying,
    embedding_updated_at timestamp with time zone,
    retrieval_count integer DEFAULT 0,
    last_retrieved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    is_active boolean DEFAULT true
);
COMMENT ON TABLE assurance.case_records IS '案例库，用于 RAG 检索和 LLM 消歧';
COMMENT ON COLUMN assurance.case_records.embedding IS 'bge-m3 embedding（1024 维），需要 pgvector 扩展';
COMMENT ON COLUMN assurance.case_records.retrieval_count IS '检索次数，用于统计热度';
CREATE TABLE assurance.device_inventory (
    device_id character varying(64) NOT NULL,
    device_name character varying(128) NOT NULL,
    device_type character varying(32) NOT NULL,
    management_ip inet,
    management_port integer,
    location text,
    status character varying(32) NOT NULL,
    last_seen_at timestamp with time zone,
    ports jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    metadata jsonb
);
COMMENT ON TABLE assurance.device_inventory IS '网络设备基础信息，所有模块共享';
CREATE TABLE assurance.diagnosis_results (
    diagnosis_id character varying(64) NOT NULL,
    intent_id character varying(64) NOT NULL,
    record_id character varying(64) NOT NULL,
    violation_event_id character varying(64),
    root_cause text NOT NULL,
    root_cause_confidence double precision NOT NULL,
    affected_devices jsonb,
    affected_links jsonb,
    evidence jsonb,
    cause_discriminator text,
    llm_enabled boolean DEFAULT false,
    llm_reasoning jsonb,
    llm_model character varying(64),
    llm_latency_ms integer,
    retrieval_method character varying(64),
    retrieved_cases jsonb,
    retrieval_latency_ms integer,
    diagnosed_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    diagnosis_duration_ms integer
);
COMMENT ON TABLE assurance.diagnosis_results IS
    '[伙伴负责] 完整版诊断结果(含 CoT 推理步骤/LLM 审计信息)。'
    '我方只读此表, 只写 assurance.consistency_checks。两表用 intent_id 关联。';
COMMENT ON COLUMN assurance.diagnosis_results.llm_reasoning IS 'LLM 思维链推理步骤，用于审计追溯';
CREATE TABLE assurance.fault_patterns (
    fault_id character varying(64) NOT NULL,
    version character varying(32) NOT NULL,
    cause_type character varying(64) NOT NULL,
    title character varying(256) NOT NULL,
    category character varying(64),
    description text,
    typical_symptoms jsonb,
    confirmation_signals jsonb,
    exclusion_signals jsonb,
    required_evidence jsonb,
    cause_discriminators jsonb,
    repair_strategies jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    is_active boolean DEFAULT true
);
COMMENT ON TABLE assurance.fault_patterns IS '故障模式知识库，用于规则匹配诊断';
CREATE TABLE assurance.intent_registry (
    intent_id character varying(64) NOT NULL,
    intent_type character varying(32) NOT NULL,
    source_module character varying(32) NOT NULL,
    child_intent_id character varying(64),
    lifecycle_status character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    deprecated_at timestamp with time zone,
    owner_module character varying(32),
    description text,
    tags jsonb,
    metadata jsonb
);
COMMENT ON TABLE assurance.intent_registry IS '全局意图索引，所有模块共享';
COMMENT ON COLUMN assurance.intent_registry.tags IS '标签数组，便于查询过滤';
CREATE TABLE assurance.violation_events (
    event_id character varying(64) NOT NULL,
    intent_id character varying(64) NOT NULL,
    record_id character varying(64) NOT NULL,
    violation_type character varying(64) NOT NULL,
    violation_severity character varying(32) NOT NULL,
    violation_details jsonb,
    detected_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    resolved_at timestamp with time zone,
    is_resolved boolean DEFAULT false,
    resolution_method character varying(64),
    diagnosis_id character varying(64),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);
COMMENT ON TABLE assurance.violation_events IS '违例事件记录，每次检测发现违例时写入';
-- audit_logs.log_id 的自增序列。
-- 伙伴的原始脚本里这列是 serial（PG 会自动建序列），但规范文件是手写 CREATE TABLE，
-- 序列不会自动出现 —— 漏建会导致全新库执行到此报错。
-- 2026-10-27 实测发现：文件套到新库时报 relation "assurance.audit_logs_log_id_seq" does not exist
CREATE SEQUENCE assurance.audit_logs_log_id_seq;
ALTER SEQUENCE assurance.audit_logs_log_id_seq OWNED BY assurance.audit_logs.log_id;
ALTER TABLE ONLY assurance.audit_logs ALTER COLUMN log_id SET DEFAULT nextval('assurance.audit_logs_log_id_seq'::regclass);
ALTER TABLE ONLY assurance._metadata
    ADD CONSTRAINT _metadata_pkey PRIMARY KEY (key);
ALTER TABLE ONLY assurance.assurance_records
    ADD CONSTRAINT assurance_records_pkey PRIMARY KEY (intent_id);
ALTER TABLE ONLY assurance.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (log_id);
ALTER TABLE ONLY assurance.case_records
    ADD CONSTRAINT case_records_pkey PRIMARY KEY (case_id);
ALTER TABLE ONLY assurance.device_inventory
    ADD CONSTRAINT device_inventory_pkey PRIMARY KEY (device_id);
ALTER TABLE ONLY assurance.diagnosis_results
    ADD CONSTRAINT diagnosis_results_pkey PRIMARY KEY (diagnosis_id);
ALTER TABLE ONLY assurance.fault_patterns
    ADD CONSTRAINT fault_patterns_pkey PRIMARY KEY (fault_id);
ALTER TABLE ONLY assurance.intent_registry
    ADD CONSTRAINT intent_registry_pkey PRIMARY KEY (intent_id);
ALTER TABLE ONLY assurance.violation_events
    ADD CONSTRAINT violation_events_pkey PRIMARY KEY (event_id);
CREATE INDEX idx_al_intent ON assurance.audit_logs USING btree (intent_id);
CREATE INDEX idx_al_time ON assurance.audit_logs USING btree (created_at);
CREATE INDEX idx_al_type ON assurance.audit_logs USING btree (event_type);
CREATE INDEX idx_ar_status ON assurance.assurance_records USING btree (status);
CREATE INDEX idx_ar_updated ON assurance.assurance_records USING btree (updated_at);
CREATE INDEX idx_cr_active ON assurance.case_records USING btree (is_active);
CREATE INDEX idx_cr_fault ON assurance.case_records USING btree (fault_id);
CREATE INDEX idx_di_status ON assurance.device_inventory USING btree (status);
CREATE INDEX idx_di_type ON assurance.device_inventory USING btree (device_type);
CREATE INDEX idx_dr_cause ON assurance.diagnosis_results USING btree (root_cause);
CREATE INDEX idx_dr_diagnosed ON assurance.diagnosis_results USING btree (diagnosed_at);
CREATE INDEX idx_dr_intent ON assurance.diagnosis_results USING btree (intent_id);
CREATE INDEX idx_fp_active ON assurance.fault_patterns USING btree (is_active);
CREATE INDEX idx_fp_cause ON assurance.fault_patterns USING btree (cause_type);
CREATE INDEX idx_ir_created ON assurance.intent_registry USING btree (created_at);
CREATE INDEX idx_ir_status ON assurance.intent_registry USING btree (lifecycle_status);
CREATE INDEX idx_ir_type ON assurance.intent_registry USING btree (intent_type);
CREATE INDEX idx_ve_detected ON assurance.violation_events USING btree (detected_at);
CREATE INDEX idx_ve_intent ON assurance.violation_events USING btree (intent_id);
CREATE INDEX idx_ve_type ON assurance.violation_events USING btree (violation_type);

-- 错误级别审计归档（severity='ERROR' 的另存一份，便于排障；见 retention-policy.md）
CREATE TABLE shared.audit_log_error_archive (
    id             BIGINT DEFAULT nextval('shared.audit_log_id_seq'::regclass) NOT NULL,
    occurred_at    TIMESTAMPTZ DEFAULT now() NOT NULL,
    module_name    TEXT NOT NULL,
    actor          TEXT NOT NULL,
    action         TEXT NOT NULL,
    target         TEXT,
    severity       TEXT DEFAULT 'INFO'::text NOT NULL,
    payload        JSONB DEFAULT '{}'::jsonb NOT NULL,
    correlation_id UUID
);
ALTER TABLE ONLY shared.audit_log_error_archive
    ADD CONSTRAINT audit_log_error_archive_pkey PRIMARY KEY (id, occurred_at);
CREATE INDEX audit_log_error_archive_correlation_id_idx
    ON shared.audit_log_error_archive USING btree (correlation_id)
    WHERE (correlation_id IS NOT NULL);
CREATE INDEX audit_log_error_archive_module_name_occurred_at_idx
    ON shared.audit_log_error_archive USING btree (module_name, occurred_at DESC);

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
-- ============================================================================"""


def _exec_raw(sql):
    """执行原始 SQL（绕开 SQLAlchemy 的文本解析）。

    为什么不用 op.get_bind().exec_driver_sql(sql):
      - 它会给 driver 传一个空 dict 参数, psycopg2 遇到**多语句** SQL 会报
        TypeError: sqlalchemy.util._immutabledict_cy.immutabledict is not a sequence
      - op.execute(sa.text(sql)) 更糟: text() 会把 `::jsonb` 里的冒号当绑定参数
    直接拿 DBAPI 连接最稳, 也最接近 psql -f 的行为。
    """
    bind = op.get_bind()
    raw = bind.connection
    with raw.cursor() as cur:
        cur.execute(sql)
    raw.commit()


def upgrade():
    _exec_raw(SCHEMA_SQL)


def downgrade():
    _exec_raw("""
        DROP SCHEMA IF EXISTS assurance_mv    CASCADE;
        DROP SCHEMA IF EXISTS assurance      CASCADE;
        DROP SCHEMA IF EXISTS implementation CASCADE;
        DROP SCHEMA IF EXISTS translation    CASCADE;
        DROP SCHEMA IF EXISTS intensure      CASCADE;
        DROP SCHEMA IF EXISTS shared         CASCADE;
    """)
