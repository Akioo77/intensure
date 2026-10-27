#!/bin/bash
# run_retention_cleanup.sh —— 定期清理过期分区（**有破坏性**）
#
# 为什么和 alembic 分开:
#   分区清理会 DROP 数据, 属于运维动作, 不能塞进 migration ——
#   否则「alembic upgrade head」就等于删数据。
#   migration 只负责建 drop_old_partitions() 函数, 清理由这个脚本执行。
#
# 用法:
#   bash db_maintenance/run_retention_cleanup.sh [库名]
#
# 建议 crontab（每周一次）:
#   0 4 * * 0 bash /home/sunxiaoxuan.guest/intensure/db_maintenance/run_retention_cleanup.sh
set -euo pipefail

DB="${1:-intensure_dev}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== retention cleanup @ $(date '+%F %T') (db=$DB) ==="
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "$DB" -f "$PROJECT_DIR/db_maintenance/retention_cleanup.sql"
echo "=== done ==="
