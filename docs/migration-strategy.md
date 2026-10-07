# 数据库迁移策略 v0.1

> **范围**：意图驱动网络管理系统 4 智能体的 PostgreSQL schema 演进
> **目标**：schema 变更可控、可回滚、跨模块协调
> **配套**：`docs/database-schema.sql`（v0.1 初始 DDL）

---

## 0. 工具选型

按模块实现语言选迁移工具：

| 模块 | 实现语言（推断） | 迁移工具 |
|---|---|---|
| intensure | Python 3.12 | **Alembic** |
| translation | Python（待定）| **Alembic** |
| assurance | Python 3.12（FastAPI）| **Alembic** |
| implementation | Python 或 Java（待定）| **Alembic** 或 **Flyway** |

> Phase 1 全 Python，**只用 Alembic**；Phase 2+ 若实现模块切 Java/Spring Boot，加 Flyway。

---

## 1. Alembic 目录布局（每个模块各自一套）

```
module-repo/
├── alembic/
│   ├── env.py                  # 配置：连接串 + target_schema
│   ├── script.py.mako           # 模板
│   └── versions/
│       ├── v0_1_initial.py     # 初始 migration
│       ├── v0_2_add_index.py   # 后续
│       └── ...
├── alembic.ini                 # Alembic 配置
└── db/
    └── schema/
        └── *.sql               # 共享 DDL 引用（从 docs/database-schema.sql 复制）
```

**关键**：`env.py` 显式设置 `target_schema = ['intensure', 'shared', 'public']`（按模块），**不会跨 schema 误改**。

---

## 2. 版本号规则（语义化 + 时间戳前缀）

```python
revision_id = "v0.1_initial_20261008"
```

格式：`v{major}.{minor}_{description}_{YYYYMMDD}`

- **major**：破坏性（表结构变更，跨模块契约变化）
- **minor**：向后兼容（增字段、增索引、新表）
- **description**：简短英文描述
- **YYYYMMDD**：创建日期（多个模块对同一关键变更同步）

**示例**：
- `v0.1_initial_20261008`：初始 schema
- `v0.2_add_diagnoses_evidence_20261101`：诊断表加 evidence 列
- `v1.0_policies_intent_id_split_20261215`：policies 表拆分（破坏性 → major）

---

## 3. 初始化流程（Phase 1 一次性）

### 3.1 创建数据库（在 intensure VM 上）

```bash
# VM 内
sudo apt update && sudo apt install -y postgresql-16 postgresql-client-16
sudo systemctl enable --now postgresql
sudo -u postgres psql -c "SHOW server_version;"  # 确认 16.x
```

### 3.2 创建 intensure 数据库（dev）

```bash
sudo -u postgres createuser -s intensure_admin  # DDL 用
sudo -u postgres createdb -O intensure_admin intensure_dev
sudo -u postgres psql -d intensure_dev -f ~/workspace/projects/intensure/docs/database-schema.sql
```

### 3.3 创建 4 模块用户 + 权限

```bash
sudo -u postgres psql -d intensure_dev -f ~/workspace/projects/intensure/docs/access-control-matrix.sql
# 创建 access-control-matrix.sql（把 .md 中的 SQL 部分抽出成 .sql 文件）
```

### 3.4 每个模块初始化 Alembic

以 intensure 为例：

```bash
cd ~/workspace/projects/intensure
pip install alembic psycopg2-binary sqlalchemy
alembic init alembic
# 配置 alembic.ini：sqlalchemy.url = postgresql://intensure_user:intensure_dev_pwd@localhost/intensure_dev
# 修改 alembic/env.py：version_table_schema='intensure', include_schemas=True
alembic revision --autogenerate -m "v0.1_initial_20261008"
# 检查生成的 migration：只包含 intensure.* 的表
alembic upgrade head
```

**Phase 1 关键约定**：所有 4 个模块的 migration 都跑在同一个 DB（`intensure_dev`），通过 schema 隔离。这样：
- migration 脚本能引用其他 schema 的表（避免硬编码 schema 名）

---

## 4. 日常 migration 流程（开发期）

### 4.1 改表结构

1. 修改 SQLAlchemy model（`intensure/db/models.py`）
2. `alembic revision --autogenerate -m "v0.2_add_xxx_20261101"`
3. **人工 review** 生成的 `.py` 文件（autogenerate 不完美！）
4. 本地 `alembic upgrade head` 测试
5. `alembic downgrade -1` 验证可回滚
6. 提交到 git

### 4.2 多人协作

- Migration 文件用**时间戳**排序（避免冲突）
- Alembic 自动按字母序 apply，文件名用 `v0_2_1_xxx_20261101_xxxx.py` 确保顺序
- 任何 schema 变更**先 PR、再 merge、最后 apply**（禁止直接改生产 DB）

---

## 5. 跨模块 migration 协调

**场景**：保障模块新增 `assurance.healing_intents.execution_strategy` 列，需要实现模块也新增对应字段。

**协议**：
1. **owner 先提交**：assurance 模块先 PR migration（owner：保障模块）
2. **依赖方跟进**：implementation 模块在同 PR 里跟随（owner：实现模块）
3. **CI 强制顺序**：CI 检查 `git log` 中两个 migration 的 commit 顺序
4. **同步升级**：4 个模块部署时按时间窗口批量升级（避免部分升级导致 schema 不一致）

**短场景**：考虑给所有模块用**同一个 git 仓库**（monorepo），避免跨仓库协调。Phase 1 评估是否合并。

---

## 6. 回滚策略

### 6.1 单个 migration 回滚

```bash
# 回滚一个版本
alembic downgrade -1

# 回滚到指定版本
alembic downgrade v0.2_xxx_20261020

# 全部回滚到初始
alembic downgrade base
```

### 6.2 整个 schema 回滚（灾难恢复）

```bash
sudo -u postgres dropdb intensure_dev
sudo -u postgres createdb -O intensure_admin intensure_dev
sudo -u postgres psql -d intensure_dev -f ~/workspace/projects/intensure/docs/database-schema.sql
# 重跑 access-control-matrix.sql
```

### 6.3 生产环境回滚（Phase 2+）

- 每次升级前 `pg_dump --schema-only > backup-YYYYMMDD.sql`
- 回滚 = `psql -d new < backup-YYYYMMDD.sql`
- **永远不要** `DROP SCHEMA` 然后重建（丢失数据）

---

## 7. Migration 脚本编写规范

### 7.1 必须包含 upgrade 和 downgrade

```python
def upgrade():
    op.add_column('policies',
        sa.Column('execution_strategy', sa.Text(), nullable=True))

def downgrade():
    op.drop_column('policies', 'execution_strategy')
```

### 7.2 破坏性变更要拆步

不要一个 migration 里 DROP COLUMN + ADD COLUMN（数据丢）；分两步：
1. migration A：ADD COLUMN（nullable）
2. 应用代码填默认值
3. migration B：ALTER COLUMN SET NOT NULL

### 7.3 数据迁移独立 migration

```python
def upgrade():
    # 1. 加列
    op.add_column('policies', sa.Column('priority', sa.Integer(), nullable=True))
    # 2. 填数据（用 Python 逻辑，不是 SQL）
    connection = op.get_bind()
    connection.execute("UPDATE policies SET priority = 100 WHERE priority IS NULL")
    # 3. 改 NOT NULL
    op.alter_column('policies', 'priority', nullable=False)
```

---

## 8. 测试策略

### 8.1 本地自动测

```bash
# 创建临时 DB
sudo -u postgres createdb intensure_test

# 跑所有 migration
DATABASE_URL=postgresql://intensure_user@localhost/intensure_test alembic upgrade head

# 跑测试
pytest tests/

# 删 DB
sudo -u postgres dropdb intensure_test
```

### 8.2 CI 强制项（Phase 2）

- [ ] 所有 migration 都测试过 `upgrade` + `downgrade`
- [ ] 没有跨 schema 外键（避免拆分时崩）
- [ ] 没有 `op.drop_table` 没确认（PR review）

---

## 9. 部署期 migration（Phase 2 简化）

Phase 1 简化版：手动跑

```bash
# 每个模块启动时检查
alembic current  # 当前版本
alembic upgrade head  # 跟最新
```

Phase 2 自动化：
- CI/CD pipeline 步骤：`alembic upgrade head`
- 4 个模块升级有顺序：intensure → implementation → translation → assurance（依赖链）

---

## 10. 拆分期的 migration（Phase 3）

当拆成 4 个独立 DB 时：

| 旧（统一 DB） | 新（拆分 DB） | 迁移动作 |
|---|---|---|
| `intensure.*` | DB1.intensure.* | 导出/导入 |
| `translation.*` | DB2.translation.* | 导出/导入 |
| `implementation.*` | DB3.implementation.* | 导出/导入 |
| `assurance.*` | DB4.assurance.* | 导出/导入 |
| `shared.audit_log` | DB4.assurance.shared.* 或独立的 shared DB | 决定 |
| `shared.module_health` | 同上 | 决定 |

**关键**：拆分前**避免跨 schema 外键**（Phase 0 设计就保证了）。只需：
1. 每个模块的 Alembic 改 connection URL
2. 调整 default_privileges（每个 DB 独立）
3. 数据导出/导入（`pg_dump --schema=intensure`）

**业务代码不动**（这是 Phase 0 设计的胜利）。

---

## 11. 与"初始 DDL 文件"的关系

- `docs/database-schema.sql`：手写初始 DDL（一次性，**golden source**）
- `alembic/versions/v0_1_initial_*.py`：Alembic 初始 migration（**由手工编写**，与 DDL 等价）
- **不要**用 autogenerate 生成 v0.1（避免 Alembic 推断错误）

**流程**：
1. 设计 DDL → 写到 `docs/database-schema.sql`
2. 评审通过 → 写对应的 Alembic migration（手动）
3. 评审 Alembic migration → 跑 `alembic upgrade head`
4. 提交 git

---

## 12. 当前状态（Phase 1）

- [ ] PostgreSQL 16 装到 Colima VM
- [ ] 初始 DDL 跑通
- [ ] 4 用户 + 权限就位
- [ ] Alembic 初始化（intensure 先做）
- [ ] 越权测试通过（见 access-control-matrix.md §11）

---

## 13. 反模式（不要做）

- ❌ **autogenerate v0.1**（自己写）
- ❌ **跨 schema 外键**（拆分会崩）
- ❌ **生产环境改表不写 migration**（必须走 alembic）
- ❌ **DROP SCHEMA CASCADE 重建**（数据丢失）
- ❌ **不同模块 migration 不协调**（schema 不一致）

---

_文档创建：2026-10-08 · v0.1 · Phase 0 交付_