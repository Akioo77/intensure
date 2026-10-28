r"""retention 分区清理**函数**（只建函数，不执行清理）

把 `db_maintenance/retention_cleanup.sql` 的**函数部分**纳入版本管理。

⚠️ 刻意**不包含**脚本末尾的主流程（那段会真的 DROP 分区）——
   迁移必须无副作用：把破坏性清理放进 `alembic upgrade head` 意味着
   「升个版本就删数据」。清理仍然是运维动作，由 run_retention_cleanup.sh 定期跑。

⚠️ psql 元命令（\set ...）必须剔除 —— 走 DBAPI 会语法错并回滚整个事务。

Revision ID: 0002_retention_function
Revises: 0001_baseline
Create Date: 2026-10-27
"""
from alembic import op

revision = "0002_retention_function"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


RETENTION_FUNC_SQL = r"""-- ============================================================================
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
    part_end DATE;
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
        part_end  := (regexp_matches(part.bounds, 'TO \(''([0-9]{4}-[0-9]{2}-[0-9]{2})', 'g'))[1]::DATE;

        -- ⚠️ 必须用「结束日」判断，不能用「起始日」。
        -- 用起始日会把**当前月**的分区删掉 —— 例如 state_history 保留 7 天时，
        -- cutoff=今天-7，而当月分区起始日（如 2026-10-01）< cutoff，
        -- 结果当前月分区被删，之后任何 INSERT 都报
        --   ERROR: no partition of relation "state_history" found for row
        -- 2026-10-27 实测发现并修复。
        -- 正确语义：只有当分区**整个区间**都早于 cutoff 才删。
        IF part_end IS NOT NULL AND part_end <= cutoff_date THEN
            EXECUTE format('DROP TABLE IF EXISTS %s', part.partition_name);
            RAISE NOTICE 'Dropped: % (range % to %, cutoff=%)', part.partition_name, part_date, part_end, cutoff_date;
            dropped_count := dropped_count + 1;
        END IF;
    END LOOP;

    RETURN dropped_count;
END;
$$ LANGUAGE plpgsql;"""


def _exec_raw(sql):
    bind = op.get_bind()
    raw = bind.connection
    with raw.cursor() as cur:
        cur.execute(sql)
    raw.commit()


def upgrade():
    """只建函数，不删任何分区。"""
    _exec_raw(RETENTION_FUNC_SQL)


def downgrade():
    op.get_bind().exec_driver_sql(
        "DROP FUNCTION IF EXISTS drop_old_partitions(TEXT, INTEGER)")
