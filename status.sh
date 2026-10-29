#!/bin/bash
# status.sh - intensure 运行状态总览（进程 + 端口 + DB 数据量）
#
# 用法: bash status.sh
#
# 演示前跑一次，确认该起的都起了、DB 有数据。

set -uo pipefail
# 本地地址不走代理：VM 的 /etc/environment 设了 http_proxy 但没设 no_proxy，
# 连 127.0.0.1 都会被代理劫持 → curl 返回 502/空 → 健康检查假失败。
export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"

PROJECT_DIR="/home/sunxiaoxuan.guest/intensure"
SESSION="intensure"
DB_NAME="intensure_dev"

R='\033[0m'; G='\033[32m'; Y='\033[33m'; R2='\033[31m'
C='\033[36m'; BOLD='\033[1m'; DIM='\033[2m'

step() { echo -e "\n${BOLD}${C}═══ $1 ═══${R}"; }
ok()   { echo -e "  ${G}✅${R} $1"; }
warn() { echo -e "  ${Y}⚠️${R}  $1"; }
bad()  { echo -e "  ${R2}❌${R} $1"; }
info() { echo -e "  ${DIM}$1${R}"; }

# 用 TAB 分隔: psql 默认用 | 分隔, 但 read 按空白切分会拿不到第二列。
# tr 只删空格, 保留 TAB —— read 的默认 IFS 含 TAB, 仍能正确分列。
pg() {
    sudo -u postgres psql -d "$DB_NAME" -t -A -F $'\t' -c "$1" 2>&1 | tr -d ' '
}

cd "$PROJECT_DIR" 2>/dev/null || true

# ============ 1. 进程 ============
step "1/5 进程状态"
PROC_OK=0
PROC_BAD=0
check_proc() {
    local name="$1" pattern="$2"
    local n
    n=$(pgrep -f "$pattern" 2>/dev/null | wc -l | tr -d ' ')
    if [ "$n" -gt 0 ]; then
        ok "$name（$n 个进程）"
        PROC_OK=$((PROC_OK + 1))
    else
        bad "$name 未运行"
        PROC_BAD=$((PROC_BAD + 1))
    fi
}
check_proc "os-ken 控制器" "osken-manager"
check_proc "REST API (8080)" "state_server.py"
check_proc "Mininet 拓扑"    "mininet:"
check_proc "保障模块 MVP"    "assurance_mvp"

# live_env 是可选的(要 --with-live 才起), 未起不算失败。
LIVE_N=$(pgrep -f "live_env.py" 2>/dev/null | wc -l | tr -d ' ')
if [ "$LIVE_N" -gt 0 ]; then
    ok "live_env（$LIVE_N 个进程）"
else
    info "live_env 未运行（可选，需要时: bash start_all.sh --with-live）"
fi

# ============ 2. tmux ============
step "2/5 tmux 会话"
if tmux has-session -t "$SESSION" 2>/dev/null; then
    ok "session '$SESSION' 存在"
    tmux list-windows -t "$SESSION" -F "    窗口 #{window_index}: #{window_name}" 2>/dev/null | sed 's/^/  /'
else
    warn "session '$SESSION' 不存在"
fi

# ============ 3. 端口 ============
step "3/5 端口监听"
check_port() {
    local port="$1" label="$2"
    if ss -tln 2>/dev/null | grep -q ":$port "; then
        ok "$label ($port)"
    else
        warn "$label ($port) 未监听"
    fi
}
check_port 6653 "OpenFlow (os-ken)"
check_port 8080 "REST API"

HEALTH=$(curl -s -m 5 --noproxy '*' http://127.0.0.1:8080/api/health 2>/dev/null)
if [ -n "$HEALTH" ]; then
    ok "/api/health 响应: $(echo "$HEALTH" | head -c 100)"
else
    bad "/api/health 无响应"
fi

# ============ 4. DB 数据量 ============
step "4/5 DB 数据量（$DB_NAME）"
if ! pg "SELECT 1" >/dev/null 2>&1; then
    bad "数据库不可达"
else
    printf "    %-38s %s\n" "表" "行数"
    pg "SELECT 'intensure.network_state', count(*) FROM intensure.network_state
        UNION ALL SELECT 'intensure.state_history', count(*) FROM intensure.state_history
        UNION ALL SELECT 'intensure.probe_results', count(*) FROM intensure.probe_results
        UNION ALL SELECT 'intensure.port_events', count(*) FROM intensure.port_events
        UNION ALL SELECT 'intensure.link_events', count(*) FROM intensure.link_events
        UNION ALL SELECT 'assurance.consistency_checks', count(*) FROM assurance.consistency_checks
        UNION ALL SELECT 'assurance.diagnoses', count(*) FROM assurance.diagnoses
        UNION ALL SELECT 'assurance.healing_intents', count(*) FROM assurance.healing_intents
        UNION ALL SELECT 'assurance.healing_history', count(*) FROM assurance.healing_history
        UNION ALL SELECT 'assurance.intent_states', count(*) FROM assurance.intent_states
        UNION ALL SELECT 'implementation.policies', count(*) FROM implementation.policies
        UNION ALL SELECT 'shared.audit_log', count(*) FROM shared.audit_log
        UNION ALL SELECT 'shared.module_health', count(*) FROM shared.module_health" \
    | while read -r tbl cnt; do
        [ -n "$tbl" ] && printf "    %-38s %s\n" "$tbl" "$cnt"
    done

    echo ""
    info "最近一致性检查:"
    # 用 chr(9)(TAB) 分隔: 不能用空格, pg() 的 tr -d ' ' 会删掉。
    pg "SELECT checked_at||chr(9)||intent_id||chr(9)||result FROM assurance.consistency_checks ORDER BY checked_at DESC LIMIT 3" \
    | sed 's/^/    /'
fi

# ============ 5. 汇总 ============
step "5/5 汇总"
echo "    运行中: $PROC_OK   未运行: $PROC_BAD"
if [ "$PROC_BAD" -eq 0 ]; then
    ok "全部组件正常"
    exit 0
else
    warn "有 $PROC_BAD 个组件没起 —— 试试: bash start_all.sh"
    exit 1
fi
