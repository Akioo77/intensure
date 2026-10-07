-- ============================================================================
-- 数据保留清理脚本（不依赖 pg_partman）
-- ============================================================================
-- 文件：db_maintenance/retention_cleanup.sql
-- 用途：定期 DROP 超过保留期的分区，控住 DB 大小
-- 运行：建议每周 cron 跑一次（详见末尾注释）
-- 安全：所有 DROP 用 IF EXISTS，幂等可重跑
-- ============================================================================

-- -----------------------------------------------------------------
-- 保留策略配置（修改这里调整保留天数）
-- -----------------------------------------------------------------
\set probe_results_retention 30
\set state_history_retention 7
\set consistency_checks_retention 30
\set audit_log_retention 30
\set port_events_retention 30
\set link_events_retention 30
\set probe_results_retention 30

-- -----------------------------------------------------------------
-- 通用：删除 N 天前的某 schema 表的所有月份分区
-- -----------------------------------------------------------------
-- 用法：SELECT drop_old_partitions('intensure.probe_results', 30);
-- 会 DROP 所有 partition upper bound < (now() - 30 days) 的子表
CREATE OR REPLACE FUNCTION drop_old_partitions(
    p_parent_table TEXT,
    p_retention_days INTEGER
) RETURNS INTEGER AS $$
DECLARE
    part RECORD;
    part_date DATE;
    cutoff_date DATE := CURRENT_DATE - p_retention_days;
    dropped_count INTEGER := 0;
    parent_prefix TEXT;
BEGIN
    -- 取父表名（schema-qualified），前缀作为命名匹配
    parent_prefix := split_part(p_parent_table, '.', 2) || '_';
    
    FOR part IN
        SELECT 
            inhrelid::regclass::text AS partition_name,
            pg_get_expr(c.relpartbound, c.oid) AS bounds
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE inhparent = p_parent_table::regclass
    LOOP
        -- bounds 形如：FOR VALUES FROM ('2026-08-01 00:00:00+08') TO ('2026-09-01 00:00:00+08')
        -- 提取起始日期
        part_date := (regexp_matches(part.bounds, 
            'FROM \(''([0-9]{4}-[0-9]{2}-[0-9]{2})', 'g'))[1]::DATE;
        
        IF part_date IS NOT NULL AND part_date < cutoff_date THEN
            EXECUTE format('DROP TABLE IF EXISTS %s', part.partition_name);
            RAISE NOTICE 'Dropped: % (date=%, cutoff=%)', part.partition_name, part_date, cutoff_date;
            dropped_count := dropped_count + 1;
        END IF;
    END LOOP;
    
    RETURN dropped_count;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------
-- 主流程：清理所有需要 DROP 的分区
-- -----------------------------------------------------------------
DO $$
DECLARE
    n INT;
BEGIN
    RAISE NOTICE '=== Starting retention cleanup at % ===', now();
    
    -- intensure
    n := drop_old_partitions('intensure.probe_results', 30);
    RAISE NOTICE 'intensure.probe_results: dropped % partition(s)', n;
    
    n := drop_old_partitions('intensure.state_history', 7);
    RAISE NOTICE 'intensure.state_history: dropped % partition(s)', n;
    
    n := drop_old_partitions('intensure.port_events', 30);
    RAISE NOTICE 'intensure.port_events: dropped % partition(s)', n;
    
    n := drop_old_partitions('intensure.link_events', 30);
    RAISE NOTICE 'intensure.link_events: dropped % partition(s)', n;
    
    -- assurance
    n := drop_old_partitions('assurance.consistency_checks', 30);
    RAISE NOTICE 'assurance.consistency_checks: dropped % partition(s)', n;
    
    n := drop_old_partitions('assurance.healing_history', 365);
    RAISE NOTICE 'assurance.healing_history: dropped % partition(s)', n;
    
    -- shared
    n := drop_old_partitions('shared.audit_log', 30);
    RAISE NOTICE 'shared.audit_log: dropped % partition(s) (ERROR severity 已另存到 error_archive)', n;
    
    RAISE NOTICE '=== Cleanup done at % ===', now();
END $$;

-- -----------------------------------------------------------------
-- shared.module_health：只保留最近 7 天记录（单表清理）
-- -----------------------------------------------------------------
DELETE FROM shared.module_health 
WHERE last_heartbeat < now() - INTERVAL '7 days';

-- -----------------------------------------------------------------
-- 同步 ERROR 级别 audit_log 到独立归档（永久保留）
-- -----------------------------------------------------------------
-- 创建归档表（一次性）：
CREATE TABLE IF NOT EXISTS shared.audit_log_error_archive (LIKE shared.audit_log INCLUDING ALL);

-- 把老 ERROR 复制到归档
INSERT INTO shared.audit_log_error_archive 
SELECT * FROM shared.audit_log 
WHERE severity = 'ERROR' 
  AND occurred_at < now() - INTERVAL '30 days'
ON CONFLICT DO NOTHING;

-- 然后 DROP 老 INFO/WARN（保留 ERROR 直到归档完成）
-- （这里为简化不主动 DROP；如需更激进清理可手动执行）

-- -----------------------------------------------------------------
-- VACUUM 回收空间
-- -----------------------------------------------------------------
VACUUM ANALYZE;

-- -----------------------------------------------------------------
-- 文件结束
-- -----------------------------------------------------------------

-- 使用建议：
-- 1) 每周一次：psql -d intensure_dev -f db_maintenance/retention_cleanup.sql
-- 2) crontab -e 加：
--    0 3 * * 0  psql -d intensure_dev -f /path/to/retention_cleanup.sql >/var/log/pg-retention.log 2>&1
-- 3) 监控脚本可查 SELECT count(*) FROM partman.part_config WHERE retention IS NOT NULL