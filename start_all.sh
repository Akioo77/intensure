#!/bin/bash
# 本地地址不走代理：VM 的 /etc/environment 设了 http_proxy 但没设 no_proxy，
# 连 127.0.0.1 都会被代理劫持 → curl 返回 502/空 → 健康检查假失败。
export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"
# start_all.sh - 一键启动 intensure 全部组件（演示现场用）
#
# 用法:
#   bash start_all.sh                 # 控制器 + REST + Mininet（+ 保障模块）
#   bash start_all.sh --with-live     # 额外起 live_env.py（周期探测 + 上报）
#   bash start_all.sh --no-mininet    # 不起 Mininet（只要控制器 + API）
#
# 组件与端口:
#   窗口 controller → os-ken 控制器   OpenFlow 6653
#   窗口 api        → REST API       http://127.0.0.1:8080
#   窗口 mininet    → Mininet 拓扑    4 主机 1 交换机
#   窗口 assurance  → 保障模块 MVP    每 5s 一致性检查
#   窗口 live       → live_env.py     周期探测 + 上报（--with-live 才起）
#
# 停止: bash stop_all.sh
# 状态: bash status.sh

set -uo pipefail

PROJECT_DIR="/home/sunxiaoxuan.guest/intensure"
SESSION="intensure"
DB_NAME="intensure_dev"

WITH_LIVE=0
WITH_MININET=1
for arg in "$@"; do
    case "$arg" in
        --with-live) WITH_LIVE=1 ;;
        --no-mininet) WITH_MININET=0 ;;
        --help|-h)
            echo "用法: bash start_all.sh [--with-live] [--no-mininet]"
            exit 0
            ;;
    esac
done

R='\033[0m'; G='\033[32m'; Y='\033[33m'; B='\033[34m'
C='\033[36m'; M='\033[35m'; BOLD='\033[1m'; DIM='\033[2m'

step() { echo -e "\n${BOLD}${C}═══ $1 ═══${R}"; }
ok()   { echo -e "  ${G}✅${R} $1"; }
warn() { echo -e "  ${Y}⚠️${R}  $1"; }
fail() { echo -e "  ${R}❌${R} $1"; exit 1; }
info() { echo -e "  ${DIM}$1${R}"; }

if [ ! -d "$PROJECT_DIR" ]; then
    fail "项目目录不存在: $PROJECT_DIR（先 colima ssh 确认 VM 路径）"
fi
cd "$PROJECT_DIR"

# 落文件日志（P1-2）—— 演示时排障能查历史
export INTENSURE_LOG_FILE=/var/log/intensure/intensure.log

# ============ 0. 前置清理 ============
step "0/4 清理旧会话"
tmux kill-session -t "$SESSION" 2>/dev/null || true
sudo pkill -9 -f "osken-manager" 2>/dev/null || true
sudo pkill -9 -f "controller_app" 2>/dev/null || true
sudo pkill -9 -f "state_server.py" 2>/dev/null || true
sudo pkill -9 -f "assurance_mvp" 2>/dev/null || true
sudo pkill -9 -f "live_env.py" 2>/dev/null || true
sudo pkill -9 -f "_demo_mn.py" 2>/dev/null || true
sudo mn -c 2>/dev/null || true
sleep 1
ok "旧进程与 tmux 会话已清理"

# ============ 1. 控制器 ============
step "1/4 启动 os-ken 控制器（OpenFlow 6653）"

# v3.21.0：状态文件是运行时快照，但如果它是别的用户（比如以前用 sudo 起的
# 控制器）留下的 root 属主文件，当前用户就写不进去 → 状态永远停在旧值，
# 而 state_server / dashboard 读到的都是过期数据（静默，很难查）。
# 这里是运行时快照，删了会重建，所以直接清掉。
if [ -f /tmp/network_state.json ] && [ ! -w /tmp/network_state.json ]; then
    warn "旧状态文件不可写（属主不是当前用户），删掉重建：/tmp/network_state.json"
    sudo rm -f /tmp/network_state.json 2>/dev/null || rm -f /tmp/network_state.json 2>/dev/null || true
fi

tmux new-session -d -s "$SESSION" -n controller -c "$PROJECT_DIR" \
    "PYTHONPATH=. osken-manager controller_app 2>&1 | tee /tmp/osken.log"
sleep 4
if tmux has-session -t "$SESSION" 2>/dev/null; then
    ok "控制器已起（窗口 controller）"
    info "日志: /tmp/osken.log"
else
    fail "控制器启动失败 —— 看 /tmp/osken.log"
fi

# ============ 2. REST API ============
step "2/4 启动 REST API（8080）"
tmux new-window -t "$SESSION" -n api -c "$PROJECT_DIR" \
    "python3 state_server.py --port 8080 2>&1 | tee /tmp/state_server.log"
sleep 3
if curl -s -m 5 --noproxy '*' http://127.0.0.1:8080/api/health 2>/dev/null | grep -q '"ok"\|"status"'; then
    ok "REST API 已响应 /api/health"
else
    RESP=$(curl -s -m 5 --noproxy '*' http://127.0.0.1:8080/api/health 2>&1 | head -c 120)
    if [ -n "$RESP" ]; then
        warn "API 有响应但内容异常: $RESP"
    else
        fail "REST API 没起来 —— 看 /tmp/state_server.log"
    fi
fi

# ============ 3. Mininet ============
if [ "$WITH_MININET" -eq 1 ]; then
    step "3/4 启动 Mininet 拓扑（4 主机 1 交换机）"
    cat > /tmp/_start_all_mn.py << 'PYEOF'
#!/usr/bin/env python3
"""start_all.sh 用的 Mininet 启动脚本（用 Python API，CLI 退出会连带杀进程）。"""
import sys
import time

sys.path.insert(0, '/home/sunxiaoxuan.guest/intensure')
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import SingleSwitchTopo
from mininet.log import setLogLevel, info

setLogLevel('info')
topo = SingleSwitchTopo(k=4)
net = Mininet(
    topo=topo,
    controller=lambda n: RemoteController(n, ip='127.0.0.1', port=6653),
    switch=OVSSwitch, autoSetMacs=True, autoStaticArp=True,
)
net.start()
info('MININET_STARTED\n')
net.pingAll()
info('PINGALL_DONE\n')
time.sleep(3600)
PYEOF
    tmux new-window -t "$SESSION" -n mininet \
        "sudo python3 /tmp/_start_all_mn.py 2>&1 | tee /tmp/mn.log"
    sleep 10
    MN_PROCS=$(pgrep -f "mininet:" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$MN_PROCS" -ge 5 ]; then
        ok "Mininet 进程全部存活（$MN_PROCS 个）"
        info "h1=10.0.0.1 h2=10.0.0.2 h3=10.0.0.3 h4=10.0.0.4"
    else
        warn "Mininet 进程数 $MN_PROCS（预期≥5：c0+s1+h1~h4）—— 看 /tmp/mn.log"
    fi
else
    step "3/4 跳过 Mininet（--no-mininet）"
fi

# ============ 4. 保障模块 / live_env ============
step "4/4 启动保障模块 MVP + 可选 live_env"
tmux new-window -t "$SESSION" -n assurance -c "$PROJECT_DIR" \
    "python3 -u assurance_mvp.py 2>&1 | tee /tmp/assurance.log"
ok "保障模块已起（窗口 assurance）"

if [ "$WITH_LIVE" -eq 1 ]; then
    tmux new-window -t "$SESSION" -n live -c "$PROJECT_DIR" \
        "python3 -u live_env.py 2>&1 | tee /tmp/live_env.log"
    ok "live_env 已起（窗口 live）"
else
    info "未起 live_env（加 --with-live 可开）"
fi

# ============ 汇总 ============
echo ""
echo -e "${BOLD}${M}════════════════════════════════════════${R}"
echo -e "${BOLD}  intensure 已启动${R}"
echo -e "${BOLD}${M}════════════════════════════════════════${R}"
tmux list-windows -t "$SESSION" -F "  窗口 #{window_index}: #{window_name}" 2>/dev/null
echo ""
echo "  查看界面:  tmux attach -t $SESSION"
echo "  切换窗口:  Ctrl+b 然后按数字键"
echo "  状态检查:  bash status.sh"
echo "  停止:      bash stop_all.sh"
echo ""
echo "  REST API:  http://127.0.0.1:8080/api/state"
echo "  Dashboard 隧道(本机): ssh -L 18080:127.0.0.1:18080 -N -f"
exit 0
