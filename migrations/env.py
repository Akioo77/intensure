"""Alembic 环境 — intensure

**刻意不用 autogenerate**：本项目的 schema 是手写 SQL + psycopg2，
没有 SQLAlchemy 模型，autogenerate 没有「模型侧」可比对，只会产出噪音。

所以这里的分工是:
- `alembic upgrade head`  = 按版本号顺序执行 migrations/versions/*.py 里的增量 DDL
- 改动 schema 的流程 = 改 docs/database-schema.sql + 写一个新 revision
- `target_metadata = None` 明确告诉 alembic「别想 autogenerate」

DSN 优先级: 环境变量 ALEMBIC_DB_URL > alembic.ini 的 sqlalchemy.url
"""
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    try:
        from logging.config import fileConfig
        fileConfig(config.config_file_name)
    except Exception:
        # logging 配置有问题不该让迁移挂掉
        pass

# 本项目无 SQLAlchemy 模型 -> 不做 autogenerate
target_metadata = None


def get_url():
    """DSN 解析: 环境变量优先。

    驱动说明: SQLAlchemy 2.x 的 `postgresql://` 默认走 **psycopg(v3)**，
    而本项目装的是 **psycopg2(v2)**（requirements.txt 里 psycopg2-binary==2.9.13）。
    不显式指定驱动会报 `ModuleNotFoundError: No module named 'psycopg'`。
    所以这里把裸 `postgresql://` 统一改写成 `postgresql+psycopg2://`，
    复用已有驱动，不引入新依赖。
    """
    url = os.environ.get('ALEMBIC_DB_URL')
    if not url:
        url = config.get_main_option('sqlalchemy.url')
    if not url:
        raise RuntimeError(
            "没有配置数据库 DSN。请设置环境变量 ALEMBIC_DB_URL，例如:\n"
            "  export ALEMBIC_DB_URL='postgresql:///intensure_dev'"
        )
    if url.startswith('postgresql://') and '+' not in url.split('://', 1)[0]:
        url = 'postgresql+psycopg2://' + url.split('://', 1)[1]
    return url


def run_migrations_offline():
    """离线模式: 只把 SQL 打到 stdout，不连库。"""
    context.configure(
        url=get_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """在线模式: 连库执行。"""
    section = config.get_section(config.config_ini_section) or {}
    section['sqlalchemy.url'] = get_url()
    connectable = engine_from_config(
        section,
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 迁移里有 CREATE SCHEMA / ALTER ... OWNER，需要能建对象
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
