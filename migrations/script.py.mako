"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}




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
    """应用变更。用 _exec_raw(...) 执行原始 SQL。"""
    ${upgrades if upgrades else "pass"}


def downgrade():
    """回退变更。"""
    ${downgrades if downgrades else "pass"}
