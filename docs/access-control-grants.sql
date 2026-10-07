-- ============================================================================
-- 数据库访问控制 GRANT 脚本（从 access-control-matrix.md 提取）
-- ============================================================================
-- 文件：docs/access-control-grants.sql
-- 版本：v0.1 (2026-10-08)
-- 使用：sudo -u postgres psql -d intensure_dev -f access-control-grants.sql
-- ============================================================================

-- 1. 创建用户
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

-- 2. CONNECT
GRANT CONNECT ON DATABASE intensure_dev TO intensure_user, translation_user, implementation_user, assurance_user;

-- 3. SCHEMA USAGE
GRANT USAGE ON SCHEMA intensure, shared, translation, implementation, assurance_mv TO intensure_user;
GRANT USAGE ON SCHEMA shared, translation, intensure, implementation, assurance_mv TO translation_user;
GRANT USAGE ON SCHEMA shared, translation, intensure, implementation, assurance, assurance_mv TO implementation_user;
GRANT USAGE ON SCHEMA shared, translation, implementation, intensure, assurance, assurance_mv TO assurance_user;

-- 4. SHARED
GRANT SELECT ON shared.schema_version TO intensure_user, translation_user, implementation_user, assurance_user;

GRANT SELECT ON shared.audit_log TO intensure_user, translation_user, implementation_user, assurance_user;
GRANT INSERT ON shared.audit_log TO intensure_user, translation_user, implementation_user, assurance_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA shared TO intensure_user, translation_user, implementation_user, assurance_user;
REVOKE UPDATE, DELETE ON shared.audit_log FROM intensure_user, translation_user, implementation_user, assurance_user;

GRANT SELECT, INSERT, UPDATE ON shared.module_health TO intensure_user, translation_user, implementation_user, assurance_user;

ALTER TABLE shared.module_health ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS module_health_intensure ON shared.module_health;
DROP POLICY IF EXISTS module_health_translation ON shared.module_health;
DROP POLICY IF EXISTS module_health_implementation ON shared.module_health;
DROP POLICY IF EXISTS module_health_assurance ON shared.module_health;

CREATE POLICY module_health_intensure ON shared.module_health
    FOR ALL TO intensure_user USING (module_name = 'intensure') WITH CHECK (module_name = 'intensure');
CREATE POLICY module_health_translation ON shared.module_health
    FOR ALL TO translation_user USING (module_name = 'translation') WITH CHECK (module_name = 'translation');
CREATE POLICY module_health_implementation ON shared.module_health
    FOR ALL TO implementation_user USING (module_name = 'implementation') WITH CHECK (module_name = 'implementation');
CREATE POLICY module_health_assurance ON shared.module_health
    FOR ALL TO assurance_user USING (module_name = 'assurance') WITH CHECK (module_name = 'assurance');

-- 5. INTENSURE
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA intensure TO intensure_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA intensure TO intensure_user;
GRANT USAGE ON SCHEMA intensure TO assurance_user;
GRANT SELECT ON ALL TABLES IN SCHEMA intensure TO assurance_user;

-- 6. TRANSLATION
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA translation TO translation_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA translation TO translation_user;
GRANT USAGE ON SCHEMA translation TO implementation_user;
GRANT SELECT ON translation.intent_templates, translation.parsed_intents TO implementation_user;
GRANT USAGE ON SCHEMA translation TO assurance_user;
GRANT SELECT ON translation.intent_templates TO assurance_user;

-- 7. IMPLEMENTATION（关键：翻译对 policies 仅 SELECT）
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA implementation TO implementation_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA implementation TO implementation_user;
GRANT USAGE ON SCHEMA implementation TO translation_user;
GRANT SELECT ON implementation.policies TO translation_user;
GRANT SELECT ON implementation.deployments TO translation_user;
GRANT USAGE ON SCHEMA implementation TO assurance_user;
GRANT SELECT ON implementation.policies, implementation.deployments TO assurance_user;

-- 8. ASSURANCE
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA assurance TO assurance_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA assurance TO assurance_user;
GRANT USAGE ON SCHEMA assurance TO translation_user;
GRANT SELECT ON assurance.intent_states, assurance.diagnoses TO translation_user;
GRANT USAGE ON SCHEMA assurance TO implementation_user;
GRANT SELECT ON assurance.healing_intents, assurance.healing_history TO implementation_user;

-- 9. ASSURANCE_MV
GRANT SELECT ON ALL TABLES IN SCHEMA assurance_mv TO intensure_user, translation_user, implementation_user, assurance_user;

-- 10. DEFAULT PRIVILEGES
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

-- 文件结束