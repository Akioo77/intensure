#!/bin/bash
# run_retention_cleanup.sh —— 定期清理过期分区（**有破坏性**）
#
# 为什么和 alembic 分开:
#   分区清理会 DROP 数据, 属于运维动作, 不能塞进 migration ——
#   否则「alembic upgrade head」就等于删数据。
#   migration 只负责建 drop_old_partitions() 函数, 清理由这个脚本执行。
#
# ⚠️ 为什么用 stdin 而不是 psql -f:
#   项目目录是 0700, postgres 用户读不到里面的 .sql 文件。
#   psql -f 会报 "Permission denied"。
#   改成 < 重定向: 由当前用户(项目属主)打开文件喂给 psql 的 stdin,
#   postgres 根本不需要读路径 —— 和 alembic 里 _exec_raw 的思路一致。
#
# 用法:
#   bash db_maintenance/run_retention_cleanup.sh [库名]
#
# 建议 crontab（若不用 systemd timer）:
#   0 4 * * 0 bash /home/sunxiaoxuan.guest/intensure/db_maintenance/run_retention_cleanup.sh
set -euo pipefail

DB="${1:-intensure_dev}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SQL_FILE="$PROJECT_DIR/db_maintenance/retention_cleanup.sql"

if [ ! -r "$SQL_FILE" ]; then
    echo "读不到清理脚本: $SQL_FILE" >&2
    exit 1
fi

echo "=== retention cleanup @ $(date '+%F %T') (db=$DB) ==="
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$DB" < "$SQL_FILE"
echo "=== done ==="
