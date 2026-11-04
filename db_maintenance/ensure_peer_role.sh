#!/bin/bash
# ensure_peer_role.sh — 确保「与 OS 用户同名的 PG 角色」存在（alembic 迁移的前置条件）
#
# 背景：alembic 用 `postgresql:///intensure_dev` 连接，走 unix socket + peer 认证，
#       peer 认证的 PG 角色名 = OS 用户名。换了机器 / 换了账号后，这个角色不会
#       跟着出现 → alembic 直接报 `FATAL: role "xxx" does not exist`。
#
# 用法:
#   bash db_maintenance/ensure_peer_role.sh          # 建当前 OS 用户同名的角色
#   bash db_maintenance/ensure_peer_role.sh akioo    # 建指定名字的角色
#
# 幂等：角色已存在就直接退出，不会改权限。

set -euo pipefail

TARGET_USER="${1:-$(id -un)}"

# 检查 postgres 服务在跑
if ! pg_isready -q 2>/dev/null; then
    echo "❌ PostgreSQL 没在跑 —— 先起服务（VM 内: sudo systemctl start postgresql）"
    exit 1
fi

# 角色已存在？
EXISTS=$(sudo -u postgres psql -tAc \
    "SELECT 1 FROM pg_roles WHERE rolname = '${TARGET_USER}'" 2>/dev/null || echo "")

if [ "$EXISTS" = "1" ]; then
    echo "✅ PG 角色 '${TARGET_USER}' 已存在 —— 无需操作"
    exit 0
fi

echo "➕ 创建 PG 角色 '${TARGET_USER}'（LOGIN SUPERUSER CREATEDB，与 OS 用户同名走 peer 认证）"
sudo -u postgres psql -v ON_ERROR_STOP=1 -c \
    "CREATE ROLE \"${TARGET_USER}\" LOGIN SUPERUSER CREATEDB;"

echo "✅ 完成。现在这条命令应该能跑通："
echo "   export ALEMBIC_DB_URL='postgresql:///intensure_dev'"
echo "   python3 -m alembic current"
