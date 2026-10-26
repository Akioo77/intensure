#!/bin/bash
# stop_all.sh - 一键停止 intensure 全部组件（优雅退出优先）
#
# 用法:
#   bash stop_all.sh            # 先 SIGTERM（走 graceful.py 优雅收尾），超时再强杀
#   bash stop_all.sh --force    # 直接 SIGKILL
#
# 优雅退出的意义: controller_app / live_env / assurance_mvp 都装了
# graceful.py 的 SIGTERM handler —— 收到信号会先落盘 state、关 DB 连接、
# 停 Mininet 拓扑，再退出。强杀会留下写一半的 network_state。

set -uo pipefail

PROJECT_DIR="/home/sunxiaoxuan.guest/intensure"
SESSION="intensure"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

R='\033[0m'; G='\033[32m'; Y='\033[33m'; C='\033[36m'
BOLD='\033[1m'; DIM='\033[2m'

step() { echo -e "\n${BOLD}${C}═══ $1 ═══${R}"; }
ok()   { echo -e "  ${G}✅${R} $1"; }
warn() { echo -e "  ${Y}⚠️${R}  $1"; }
info() { echo -e "  ${DIM}$1${R}"; }

TARGETS=(
    "assurance_mvp"
    "live_env.py"
    "state_server.py"
    "controller_app"
    "osken-manager"
    "_start_all_mn.py"
)

step "1/3 发送停止信号"
if [ "$FORCE" -eq 1 ]; then
    warn "--force：直接 SIGKILL（跳过优雅收尾，可能留半截写入）"
    SIG="-9"
else
    info "SIGTERM（优雅退出：落盘 + 关 DB + 停拓扑）"
    SIG="-15"
fi

for t in "${TARGETS[@]}"; do
    N=$(pgrep -f "$t" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$N" -gt 0 ]; then
        sudo pkill $SIG -f "$t" 2>/dev/null || true
        info "$t: 发了 SIG${SIG#-}（$N 个）"
    fi
done

if [ "$FORCE" -eq 0 ]; then
    echo ""
    info "等优雅退出（最多 8 秒）..."
    for i in $(seq 1 8); do
        ALIVE=0
        for t in "${TARGETS[@]}"; do
            pgrep -f "$t" >/dev/null 2>&1 && ALIVE=1
        done
        [ "$ALIVE" -eq 0 ] && break
        sleep 1
    done
fi

step "2/3 清理 tmux 会话与 Mininet"
tmux kill-session -t "$SESSION" 2>/dev/null || true
ok "tmux session '$SESSION' 已删"

sudo mn -c 2>/dev/null || true
MN_LEFT=$(pgrep -f "mininet:" 2>/dev/null | wc -l | tr -d ' ')
if [ "$MN_LEFT" -eq 0 ]; then
    ok "Mininet 已清理"
else
    warn "还有 $MN_LEFT 个 mininet 进程残留，强杀"
    sudo pkill -9 -f "mininet:" 2>/dev/null || true
    sudo pkill -9 -f "ovs-vswitchd --pidfile.*mininet" 2>/dev/null || true
fi

step "3/3 最终确认"
LEFT=0
for t in "${TARGETS[@]}"; do
    N=$(pgrep -f "$t" 2>/dev/null | wc -l | tr -d ' ')
    [ "$N" -gt 0 ] && { warn "$t 还剩 $N 个"; LEFT=1; }
done
if [ "$LEFT" -eq 0 ]; then
    ok "全部组件已停止"
    echo ""
    echo "  DB 数据保留（intensure_dev 不受影响），可直接 bash start_all.sh 重启"
    exit 0
else
    warn "仍有残留 —— 查: pgrep -af 'osken|mininet|assurance_mvp|live_env'"
    exit 1
fi
