# Alembic 使用指南（schema 迁移）

> 引入时间：2026-10-27（P3-1）
> 目的：把「改 schema」从**手工 DDL + 手工同步两个文件**，变成**一条命令 + 一个版本号**

---

## 一、为什么引入

在这之前，改一次 schema 要做 3 件事，漏一件就会出事：

```
1. 手写 ALTER TABLE 在库里执行        ← 忘了 = 库和文件不一致
2. 手工同步 docs/database-schema.sql  ← 忘了 = 新建的库少字段
3. 手工更新 docs/retention-policy.md  ← 忘了 = 容量估算又错
```

2026-10-26/27 两天内就因此出过 3 次事故：

| 日期 | 事故 |
|---|---|
| 10-26 | `shared.audit_log_error_archive` 在现库有、规范文件没有 → 「新建库=现有库」**是假的** |
| 10-27 | `retention_cleanup.sql` 的 RAISE 占位符/参数不匹配 → **整个脚本执行不了** |
| 10-27 | 分区删除判定用「起始日」→ 保留 7 天时会**删掉当月分区**，之后所有 INSERT 报 `no partition found` |

现在这些都有对应的**可执行检查**（见第五节）。

---

## 二、日常用法

所有命令都在 **VM 内**执行（库在 Colima VM 里）：

```bash
colima ssh
cd /home/sunxiaoxuan.guest/intensure

export ALEMBIC_DB_URL='postgresql:///intensure_dev'

alembic current          # 现在是哪个版本
alembic history          # 版本历史
alembic heads            # 最新版本是哪个
alembic upgrade head     # 升到最新（建库/升库都靠它）
alembic downgrade -1     # 回退一版
```

> DSN 优先读环境变量 `ALEMBIC_DB_URL`，其次读 `alembic.ini`。
> 密码不写进版本库。

### 建一个全新的库

```bash
sudo -u postgres psql -c 'CREATE DATABASE mydb;'
export ALEMBIC_DB_URL='postgresql:///mydb'
alembic upgrade head     # 从零建出完整 schema
```

**不需要**再单独跑 `docs/database-schema.sql` 或 `db_maintenance/retention_cleanup.sql` —— alembic 已经把它们都纳进来了。

---

## 三、改 schema 的标准流程

```bash
# 1. 改规范文件（唯一的事实来源）
vim docs/database-schema.sql

# 2. 重新生成基线（如果改的是基线覆盖的部分）
python3 db_maintenance/gen_baseline.py

# 3. 写一个新的 revision（增量）
alembic revision -m "add xxx column to yyy"
#    然后在生成的 migrations/versions/xxxx_*.py 里填 upgrade()/downgrade()

# 4. 应用
alembic upgrade head

# 5. 验证（关键！别跳过）
bash demo_scripts/verify_fresh_db.sh
```

### 为什么不用 autogenerate

本项目**没有 SQLAlchemy 模型** —— schema 是手写 SQL + psycopg2 直接操作。
alembic 的 `autogenerate` 是「比对模型和库」，没有模型就没有比对基准，
产出的只会是噪音。所以这里只用 alembic 的**版本管理 + 有序执行**能力。

新 revision 里执行 SQL 用这个helper（**不要**用 `op.execute(sa.text(...))`）：

```python
def _exec_raw(sql):
    bind = op.get_bind()
    raw = bind.connection
    with raw.cursor() as cur:
        cur.execute(sql)
    raw.commit()
```

原因见 `migrations/versions/0001_baseline.py` 里的注释 —— 简单说：
`sa.text()` 会把 `::jsonb` 里的冒号当绑定参数；`exec_driver_sql` 传空 dict
会让 psycopg2 在多语句 SQL 上报错。

---

## 四、目录结构

```
alembic.ini                          # 配置（DSN 从环境变量取）
migrations/
  env.py                             # 自定义 env（不用 autogenerate）
  script.py.mako                     # 新 revision 的模板
  versions/
    0001_baseline.py                 # 全量 schema 基线（**自动生成，别手改**）
    0002_retention_function.py       # 分区清理函数（只建函数，不删数据）
db_maintenance/
  gen_baseline.py                    # 从 schema 文件生成基线的生成器
  retention_cleanup.sql              # 分区清理脚本（psql 专用，有破坏性）
  run_retention_cleanup.sh           # 上面那个的 cron 包装
docs/database-schema.sql             # 规范文件（事实来源）
```

### 三个文件的关系

| 文件 | 角色 | 谁改 |
|---|---|---|
| `docs/database-schema.sql` | **事实来源** | 人改 |
| `migrations/versions/0001_baseline.py` | 基线快照 | **生成器产出，别手改** |
| `migrations/versions/0002_*.py` | 增量迁移 | 人写 |

**关键**：`0001_baseline.py` 是从 `docs/database-schema.sql` **自动生成**的。
如果改了规范文件但没重新生成，两者就漂了 —— `verify_fresh_db.sh` 第②步会直接 diff 出来报错。

### 为什么 0002 只建函数不执行清理

`retention_cleanup.sql` 的主流程会 **DROP 分区**（删数据）。
迁移必须无副作用 —— 把破坏性清理放进 `alembic upgrade head` 意味着「升个版本就删数据」。

所以拆成：
- **函数定义** → `0002_retention_function.py`（迁移，无副作用）
- **主流程** → `db_maintenance/run_retention_cleanup.sh`（运维脚本，cron 定期跑）

---

## 五、验证（每次改 schema 后必跑）

```bash
bash demo_scripts/verify_fresh_db.sh
```

它做 7 件事：

1. 建临时库
2. **检查基线与规范文件是否一致**（防手工同步漂移）
3. `alembic upgrade head` 从零建全库
4. 逐 schema 对比表数（新建库 vs 现库）
5. 校验每个分区表都有「覆盖 now()」的分区
6. 7 张分区表写入路由冒烟（插一行再删掉）
7. 清理临时库

全部通过 → 输出 `✅ 全部通过 —— 新建库 = 现有库`。

---

## 六、CI 里有什么

`.github/workflows/ci.yml` 加了一步：

```yaml
- name: 检查基线与 schema 规范文件一致
  run: |
    python3 db_maintenance/gen_baseline.py
    git diff --exit-code -- migrations/versions/0001_baseline.py
```

**这步不需要数据库** —— 纯文件比对。改了 `docs/database-schema.sql` 却忘了
重新生成基线，CI 会直接失败。

---

## 七、坑（都是这次踩过的）

| 坑 | 症状 | 记住 |
|---|---|---|
| `format('%I', 'a.b')` | `relation "a.b" does not exist` | `%I` 会把带点的整体加引号 → 必须拆成 `%I.%I` |
| `op.execute(sa.text(sql))` | 冒号被当绑定参数 | 用 `_exec_raw()` |
| `exec_driver_sql` 传空 dict | `immutabledict is not a sequence` | 多语句 SQL 用裸 DBAPI 连接 |
| `\set` 是 psql 元命令 | 走 DBAPI 语法错 | psql 脚本不能直接进 migration |
| alembic 会 import `versions/` 下所有 .py | 生成器被 import 时炸 | 生成器放 `db_maintenance/`，别放 versions/ |
| 分区写死月份 | 2027 年建库没当月分区 | 用「当前月 + 0..2」动态创建 |
| rsync 不删文件 | 旧 revision 残留 → multiple heads | 同步 `versions/` 要加 `--delete` |
| Python `%` 格式化模板 | `not enough arguments for format string` | 模板里有字面量 `%I` 时用 `str.replace()` 占位符 |

---

## 八、配套的角色

alembic 需要能建 schema 的角色。VM 里用的是与 OS 用户同名的 PG 角色（peer 认证）：

```sql
-- 已建（2026-10-27）
CREATE ROLE "sunxiaoxuan" LOGIN SUPERUSER CREATEDB;
```

连接串 `postgresql:///intensure_dev` 走 unix socket + peer 认证，无需密码。
