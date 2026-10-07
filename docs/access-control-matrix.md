-- ============================================================================
-- 数据库访问控制矩阵 · GRANT 脚本
-- ============================================================================
-- 文件：docs/access-control-matrix.md（含 SQL 脚本）
-- 版本：v0.1 (2026-10-08)
-- 目标：实现架构文档 §4 的访问控制矩阵
--       "防御纵深"：每个模块用独立 DB 用户，只授权本模块表
--
-- 使用：
--   1) 先跑 docs/database-schema.sql 建表
--   2) 以 postgres 身份跑本文件的 SQL（创建用户 + GRANT）
--   3) 跑末尾的越权测试，验证权限隔离
--
-- 设计原则：
--   - intensure_user: 只读 intensure.*, RW shared.module_health (own row)
--   - translation_user: RW translation.*, R implementation.policies, R assurance.*, RW shared (部分)
--   - implementation_user: RW implementation.*, R translation.parsed_intents, R assurance.*, RW shared
--   - assurance_user: RW assurance.*, R intensure.*, R implementation.policies, RW shared
--   - 关键：translation_user 对 implementation.policies 只能 SELECT（防误删）
-- ============================================================================

-- ============================================================================
-- 1. 创建用户（密码仅开发用，生产环境用 secret store）
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'intensure_user') THEN
        CREATE ROLE intensure_user LOGIN PASSWORD 'intensure_dev_pwd';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'translation_user') THEN
        CREATE ROLE translation_user LOGIN PASSWORD 'translation_dev_pwd';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'implementation_user') THEN
        CREATE ROLE implementation_user LOGIN PASSWORD 'implementation_dev_pwd';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'assurance_user') THEN
        CREATE ROLE assurance_user LOGIN PASSWORD 'assurance_dev_pwd';
    END IF;
END
$$;

-- ============================================================================
-- 2. 默认权限（连接、临时表）
-- ============================================================================

GRANT CONNECT ON DATABASE postgres TO intensure_user, translation_user, implementation_user, assurance_user;
-- 生产环境应该是独立 database，不是 postgres

GRANT USAGE ON SCHEMA shared, translation, implementation, assurance_mv TO intensure_user;
GRANT USAGE ON SCHEMA shared, intensure, implementation, assurance_mv TO translation_user;
GRANT USAGE ON SCHEMA shared, translation, intensure, assurance, assurance_mv TO implementation_user;
GRANT USAGE ON SCHEMA shared, intensure, implementation, translation, assurance_mv TO assurance_user;

-- ============================================================================
-- 3. SHARED schema 权限（所有模块可读 schema_version/audit_log；
--    module_health 自己 UPDATE 自己的行；audit_log 可 INSERT 不能改）
-- ============================================================================

GRANT SELECT ON shared.schema_version TO intensure_user, translation_user, implementation_user, assurance_user;

GRANT SELECT ON shared.audit_log TO intensure_user, translation_user, implementation_user, assurance_user;
GRANT INSERT ON shared.audit_log TO intensure_user, translation_user, implementation_user, assurance_user;
-- 显式拒绝 UPDATE/DELETE（即使 default 不允许，写出来更明确）
REVOKE UPDATE, DELETE ON shared.audit_log FROM intensure_user, translation_user, implementation_user, assurance_user;

-- module_health: 各模块只能 UPDATE 自己的行（用 RLS row-level security）
GRANT SELECT, INSERT, UPDATE ON shared.module_health TO intensure_user, translation_user, implementation_user, assurance_user;

ALTER TABLE shared.module_health ENABLE ROW LEVEL SECURITY;

-- RLS policy: 只能 UPDATE 自己的行
CREATE POLICY module_health_intensure ON shared.module_health
    FOR ALL TO intensure_user USING (module_name = 'intensure') WITH CHECK (module_name = 'intensure');
CREATE POLICY module_health_translation ON shared.module_health
    FOR ALL TO translation_user USING (module_name = 'translation') WITH CHECK (module_name = 'translation');
CREATE POLICY module_health_implementation ON shared.module_health
    FOR ALL TO implementation_user USING (module_name = 'implementation') WITH CHECK (module_name = 'implementation');
CREATE POLICY module_health_assurance ON shared.module_health
    FOR ALL TO assurance_user USING (module_name = 'assurance') WITH CHECK (module_name = 'assurance');

-- ============================================================================
-- 4. INTENSURE schema 权限（采集层完全自治）
-- ============================================================================

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA intensure TO intensure_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA intensure TO intensure_user;

-- 保障模块读 intensure
GRANT USAGE ON SCHEMA intensure TO assurance_user;
GRANT SELECT ON ALL TABLES IN SCHEMA intensure TO assurance_user;

-- ============================================================================
-- 5. TRANSLATION schema 权限
-- ============================================================================

-- 翻译自己：RW
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA translation TO translation_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA translation TO translation_user;

-- 实现模块读 parsed_intents（待部署）
GRANT USAGE ON SCHEMA translation TO implementation_user;
GRANT SELECT ON translation.intent_templates, translation.parsed_intents TO implementation_user;
-- 实现模块不需要 translation_audit

-- 保障模块读意图模板
GRANT USAGE ON SCHEMA translation TO assurance_user;
GRANT SELECT ON translation.intent_templates TO assurance_user;

-- ============================================================================
-- 6. IMPLEMENTATION schema 权限（**关键**：policies 表的 SELECT-only 隔离）
-- ============================================================================

-- 实现自己：RW
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA implementation TO implementation_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA implementation TO implementation_user;

-- 翻译读 policies（"有效规则"）
GRANT USAGE ON SCHEMA implementation TO translation_user;
GRANT SELECT ON implementation.policies TO translation_user;     -- 只读！不能写
GRANT SELECT ON implementation.deployments TO translation_user;   -- 只读部署历史

-- 翻译无 UPDATE/DELETE 权限（防御纵深）：
-- 不显式写 REVOKE，因为根本没 GRANT，default 就是没权限

-- 保障读 policies（期望状态）
GRANT USAGE ON SCHEMA implementation TO assurance_user;
GRANT SELECT ON implementation.policies, implementation.deployments TO assurance_user;

-- ============================================================================
-- 7. ASSURANCE schema 权限（保障模块自治）
-- ============================================================================

-- 保障自己：RW
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA assurance TO assurance_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA assurance TO assurance_user;

-- 翻译读 assurance 的"哪些意图历史上常违规"
GRANT USAGE ON SCHEMA assurance TO translation_user;
GRANT SELECT ON assurance.intent_states, assurance.diagnoses TO translation_user;

-- 实现读自愈历史
GRANT USAGE ON SCHEMA assurance TO implementation_user;
GRANT SELECT ON assurance.healing_intents, assurance.healing_history TO implementation_user;

-- ============================================================================
-- 8. ASSURANCE_MV schema 权限（物化视图，所有模块可读）
-- ============================================================================

GRANT SELECT ON ALL TABLES IN SCHEMA assurance_mv TO intensure_user, translation_user, implementation_user, assurance_user;

-- 保障模块可刷新物化视图
GRANT INSERT, UPDATE, DELETE ON assurance_mv.intent_status TO assurance_user;
-- 但物化视图不能直接 DELETE/UPDATE——只能 REFRESH MATERIALIZED VIEW
-- REFRESH 由 postgres 调度或 cron 跑（不归属任何模块）

-- ============================================================================
-- 9. 默认权限（未来新表）
-- ============================================================================

-- 未来在 intensure schema 创建的表，intensure_user 自动有 RW 权限
ALTER DEFAULT PRIVILEGES IN SCHEMA intensure
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO intensure_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA intensure
    GRANT USAGE, SELECT ON SEQUENCES TO intensure_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA translation
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO translation_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA translation
    GRANT USAGE, SELECT ON SEQUENCES TO translation_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA implementation
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO implementation_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA implementation
    GRANT USAGE, SELECT ON SEQUENCES TO implementation_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA assurance
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO assurance_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA assurance
    GRANT USAGE, SELECT ON SEQUENCES TO assurance_user;

-- ============================================================================
-- 10. 连接串模板（每个模块 .env / config）
-- ============================================================================

-- intensure 模块（Python/Flask）
-- DATABASE_URL=postgresql://intensure_user:intensure_dev_pwd@localhost:5432/postgres?options=-csearch_path=intensure,shared,public

-- 翻译模块
-- DATABASE_URL=postgresql://translation_user:translation_dev_pwd@localhost:5432/postgres?options=-csearch_path=translation,shared,public

-- 实现模块
-- DATABASE_URL=postgresql://implementation_user:implementation_dev_pwd@localhost:5432/postgres?options=-csearch_path=implementation,shared,public

-- 保障模块
-- DATABASE_URL=postgresql://assurance_user:assurance_dev_pwd@localhost:5432/postgres?options=-csearch_path=assurance,intensure,implementation,shared,public

-- ============================================================================
-- 11. 越权测试（**Phase 1 验收**必跑）
-- ============================================================================

-- 切换到 translation_user，尝试 DELETE implementation.policies → 期望失败
-- SET ROLE translation_user;
-- DELETE FROM implementation.policies WHERE intent_id = 'test';
-- ERROR: permission denied for table policies

-- 切换到 assurance_user，尝试 UPDATE translation.parsed_intents → 期望失败
-- SET ROLE assurance_user;
-- UPDATE translation.parsed_intents SET status = 'deployed' WHERE id = '...';
-- ERROR: permission denied for table parsed_intents

-- 切换到 intensure_user，尝试 INSERT assurance.consistency_checks → 期望失败
-- SET ROLE intensure_user;
-- INSERT INTO assurance.consistency_checks (intent_id, check_type, expected_state, actual_state, result)
-- VALUES ('test', 'reachability', '{}', '{}', 'pass');
-- ERROR: permission denied for table consistency_checks

-- 切换到 translation_user，尝试 INSERT shared.audit_log → 期望成功
-- SET ROLE translation_user;
-- INSERT INTO shared.audit_log (module_name, actor, action) VALUES ('translation', 'system', 'test');
-- INSERT 0 1

-- 切换到 translation_user，尝试 UPDATE shared.audit_log → 期望失败
-- SET ROLE translation_user;
-- UPDATE shared.audit_log SET action = 'tampered' WHERE id = 1;
-- ERROR: permission denied for table audit_log

-- 重置
-- RESET ROLE;

-- ============================================================================
-- 文件结束
-- ============================================================================