#!/usr/bin/env python3
"""从 docs/database-schema.sql 生成 alembic 基线 revision。

要点:
- 基线必须等于**已验证的现库结构**, 所以从 schema 文件生成而不是手抄
- 硬编码的月份分区改成**动态生成**(当前月 + 0..2), 否则 2027 年建新库就没有当月分区
- 用 exec_driver_sql 绕过 SQLAlchemy 对 `::jsonb` 的冒号解析

⚠️ 模板里**不要用 Python 的 % 格式化**:
   SQL 里有大量字面量 `%I` / `%L` / `%s`, 用 `%` 格式化必须处处转义成 `%%`,
   漏一个就报 "not enough arguments for format string"。
   这里统一用 `__PLACEHOLDER__` + str.replace(), 字面量原样保留。

⚠️ 本文件**不能**放在 migrations/versions/ 下: alembic 会 import 那里所有 .py,
   模块级读文件会在 import 时炸掉。

用法: python3 db_maintenance/gen_baseline.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

Q = r"'"


RETENTION_TEMPLATE = '''r"""retention 分区清理**函数**（只建函数，不执行清理）

把 `db_maintenance/retention_cleanup.sql` 的**函数部分**纳入版本管理。

⚠️ 刻意**不包含**脚本末尾的主流程（那段会真的 DROP 分区）——
   迁移必须无副作用：把破坏性清理放进 `alembic upgrade head` 意味着
   「升个版本就删数据」。清理仍然是运维动作，由 run_retention_cleanup.sh 定期跑。

⚠️ psql 元命令（\\set ...）必须剔除 —— 走 DBAPI 会语法错并回滚整个事务。

Revision ID: 0002_retention_function
Revises: 0001_baseline
Create Date: 2026-10-27
"""
from alembic import op

revision = "0002_retention_function"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


RETENTION_FUNC_SQL = r"""__SQL__"""


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
'''


def main():
    src = io.open(os.path.join(ROOT, 'docs/database-schema.sql'), encoding='utf-8').read()

    # ---------- 1. 处理硬编码分区（单趟，避免偏移量错位）----------
    PART_RE = re.compile(
        r'CREATE TABLE ([\w.]+_\d{4}_\d{2}) PARTITION OF ([\w.]+)\s*\n'
        r'\s*FOR VALUES FROM \(' + Q + r'([\d-]+)' + Q
        + r'\) TO \(' + Q + r'([\d-]+)' + Q + r'\);\n',
        re.M)

    matches = list(PART_RE.finditer(src))
    if not matches:
        print('  x 找不到硬编码分区')
        return 1

    # 分组: 相邻(仅隔空行)的分区语句属于同一张父表
    groups = []
    cur = [matches[0]]
    for prev, m in zip(matches, matches[1:]):
        if src[prev.end():m.start()].strip() == '':
            cur.append(m)
        else:
            groups.append(cur)
            cur = [m]
    groups.append(cur)

    parents = sorted({g[0].group(2) for g in groups})
    print('  硬编码分区: %d 个, 分 %d 组, 父表 %d 张'
          % (len(matches), len(groups), len(parents)))
    for p in parents:
        print('    -', p)

    # ---------- 2. 动态分区块（占位符替换，不用 % 格式化）----------
    DYN = """
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
__PARENTS__
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
"""

    arr = ',\n        '.join("'%s'" % p for p in parents)
    dyn = DYN.replace('__PARENTS__', arr)

    # ---------- 3. 替换最后一组硬编码分区 ----------
    # 动态块一次创建 7 张父表的分区, 必须放在**全部**父表建好之后 ——
    # 最后一组分区紧跟最后一张父表(assurance.healing_history), 正是这个位置。
    out = []
    prev_end = 0
    for gi, g in enumerate(groups):
        gm = g[0]
        out.append(src[prev_end:gm.start()])
        if gi == len(groups) - 1:
            out.append(dyn.strip())
        else:
            out.append('-- (原硬编码月分区已由下方动态分区块取代)\n')
        prev_end = g[-1].end()
    out.append(src[prev_end:])
    src2 = ''.join(out)
    print('  动态分区块已替换最后一组; 行数 %d -> %d'
          % (src.count('\n'), src2.count('\n')))

    # ---------- 4. 生成 revision 文件 ----------
    if '"""' in src2:
        print('  x SQL 里含三引号, 需改用其他分隔符')
        return 1

    REVISION_TEMPLATE = '''"""baseline — intensure 全量 schema（首个基线）

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


SCHEMA_SQL = r"""__SCHEMA__"""


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
'''

    revision = REVISION_TEMPLATE.replace('__SCHEMA__', src2)

    outdir = os.path.join(ROOT, 'migrations', 'versions')
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, '0001_baseline.py')
    io.open(out, 'w', encoding='utf-8').write(revision)
    print('  v 基线已生成: migrations/versions/0001_baseline.py (%d 字节)' % len(revision))

    gen_retention_revision()
    return 0


def gen_retention_revision():
    """生成 0002_retention_function.py（只含函数，不含破坏性主流程）。"""
    src = io.open(os.path.join(ROOT, 'db_maintenance/retention_cleanup.sql'),
                  encoding='utf-8').read()

    i = src.find('LANGUAGE plpgsql;')
    if i == -1:
        print('  x 找不到 retention 函数结束标记')
        return
    func_part = src[:i + len('LANGUAGE plpgsql;')]

    # 去掉 psql 元命令(\set ...) —— 走 DBAPI 会语法错
    func_part = '\n'.join(l for l in func_part.split('\n')
                           if not l.strip().startswith('\\'))

    # 主流程不能带进来(它会 DROP 分区)
    if 'Starting retention cleanup' in func_part:
        print('  x 主流程被带进来了')
        return
    if 'drop_old_partitions' not in func_part:
        print('  x 函数体丢了')
        return

    out = os.path.join(ROOT, 'migrations', 'versions', '0002_retention_function.py')
    io.open(out, 'w', encoding='utf-8').write(
        RETENTION_TEMPLATE.replace('__SQL__', func_part))
    print('  v 0002_retention_function.py 已生成 (%d 字节)' % len(func_part))


if __name__ == '__main__':
    sys.exit(main())
