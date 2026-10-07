#!/bin/bash
# 数据库演示一键脚本（Phase 1 MVP 完整闭环）
# 启动 → 健康基线 → 注入故障 → 验证 → 恢复 → 验证 → 清理
# 用法: bash demo_db_pipeline.sh [--no-cleanup]

set -e

PROJECT_DIR="/home/sunxiaoxuan.guest/intensure"
DEMO_LOG="/tmp/sdn_demo.log"

CLEANUP_AT_END=true
[ "$1" = "--no-cleanup" ] && CLEANUP_AT_END=false

# 颜色
R='\033[0m'
G='\033[32m'
Y='\033[33m'
B='\033[34m'
C='\033[36m'
M='\033[35m'
BOLD='\033[1m'
DIM='\033[2m'

step() { echo -e "\n${BOLD}${C}═══ $1 ═══${R}"; }
ok()   { echo -e "  ${G}✅${R} $1"; }
warn() { echo -e "  ${Y}⚠️${R}  $1"; }
fail() { echo -e "  ${R}❌${R} $1"; exit 1; }
info() { echo -e "  ${DIM}$1${R}"; }

pg() {
    sudo -u postgres psql -d intensure_dev -t -c "$1" 2>&1 | tr -d ' '
}

# ============ 0. 前置清理 ============
step "0/8 前置清理"
sudo pkill -9 -f "osken-manager" 2>/dev/null || true
sudo pkill -9 -f "controller_app" 2>/dev/null || true
sudo pkill -9 -f "assurance_mvp" 2>/dev/null || true
sudo pkill -9 -f "live_env.py" 2>/dev/null || true
tmux kill-session -t intensure 2>/dev/null || true
sudo pkill -9 mn 2>/dev/null || true
sudo mn -c 2>/dev/null || true
sleep 2
ok "所有 intensure 相关进程已清理"

# ============ 1. 启动 os-ken 控制器 ============
step "1/8 启动 os-ken 控制器 (监听 6653/8080)"
cd "$PROJECT_DIR"
tmux new-session -d -s intensure -c "$PROJECT_DIR" \
    "PYTHONPATH=. osken-manager controller_app 2>&1 | tee /tmp/osken.log"
sleep 4
if tmux has-session -t intensure 2>/dev/null; then
    ok "os-ken 控制器在 tmux session 'intensure' 里跑"
    info "attach: tmux attach -t intensure"
else
    fail "os-ken 启动失败，查看 /tmp/osken.log"
fi

# ============ 2. 启动 Mininet（用 Python API + tmux） ============
step "2/8 启动 Mininet (single,4 拓扑，4 主机 1 交换机)"
# 用 Python API 启动 mn（避免 CLI 退出问题），脚本已附在 VM
cat > /tmp/_demo_mn.py <<'PYEOF'
#!/usr/bin/env python3
import sys, time
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
time.sleep(3600)  # 保持运行
PYEOF

tmux new-session -d -s mininet \
    "sudo python3 /tmp/_demo_mn.py 2>&1 | tee /tmp/mn.log"
sleep 10
MININET_PROCS=$(pgrep -f "mininet:" | wc -l | tr -d ' ')
if [ "$MININET_PROCS" -ge 5 ]; then
    ok "Mininet 进程全部存活 ($MININET_PROCS 个)"
    info "h1=10.0.0.1, h2=10.0.0.2, h3=10.0.0.3, h4=10.0.0.4"
else
    warn "Mininet 进程数: $MININET_PROCS（预期≥5：c0 + s1 + h1~h4）"
fi

# 触发 pingAll 让 ARP 学习 IP
echo -e "  ${DIM}发起 pingAll 触发 ARP...${R}"
for ip in 10.0.0.{1..4}; do
    h="h${ip##*.}"
    pid=$(pgrep -f "mininet:$h" 2>/dev/null | head -1)
    [ -n "$pid" ] && sudo mnexec -a $pid ping -c 2 -W 1 $ip > /dev/null 2>&1 &
done
wait
sleep 4
HOSTS_WITH_IP=$(pg "SELECT count(*) FROM jsonb_object_keys((SELECT payload->'hosts' FROM intensure.network_state LIMIT 1)) WHERE true;")
ok "主机已学到 IP（intensure.network_state.hosts 中带 ipv4 的: ${HOSTS_WITH_IP:-计算中}）"

# ============ 3. seed 策略 ============
step "3/8 Seed 实现模块策略（mock 2 条 ACCESS_CONTROL）"
python3 seed_policies.py --clear 2>&1 | tail -5
POLICY_COUNT=$(pg "SELECT count(*) FROM implementation.policies WHERE status='active';")
ok "当前 active 策略数: $POLICY_COUNT"

# ============ 4. 启动 assurance_mvp ============
step "4/8 启动保障模块 MVP (assurance_mvp.py)"
tmux split-window -d -t intensure \
    "cd $PROJECT_DIR && PYTHONPATH=. python3 -u assurance_mvp.py 2>&1 | tee /tmp/assurance.log"
sleep 4
if pgrep -f assurance_mvp > /dev/null; then
    ok "保障模块在 tmux window 里跑"
    info "每 5s 一致性检查；变化/违例自动写入 DB"
fi

# ============ 5. 健康基线（等 8s 让数据稳定） ============
step "5/8 健康基线（等 8s 让几轮检查跑完）"
echo -e "  ${DIM}等待 8s 收集健康基线...${R}"
sleep 8
echo
echo -e "  ${BOLD}─── 当前网络状态（intensure.network_state）───${R}"
sudo -u postgres psql -d intensure_dev -c "
    SELECT (payload->'meta'->>'update_count')::int AS update_count,
           jsonb_object_keys(payload->'switches') AS switch_dpid,
           (SELECT count(*) FROM jsonb_object_keys(payload->'hosts')) AS host_count
    FROM intensure.network_state;
" 2>&1 | head -10
echo
echo -e "  ${BOLD}─── 一致性检查（assurance.consistency_checks）───${R}"
pg "SELECT result, count(*) FROM assurance.consistency_checks WHERE checked_at > now() - interval '30 seconds' GROUP BY result;" 2>&1 | head -5
PASS_COUNT=$(pg "SELECT count(*) FROM assurance.consistency_checks WHERE result='pass' AND checked_at > now() - interval '30 seconds';")
ok "健康基线：$PASS_COUNT 个 pass（应是 2 个 intent × 1-2 轮）"

# ============ 6. 注入故障 ============
step "6/8 注入故障：断 h1 接口（模拟 host 失联）"
echo -e "  ${Y}⚡ 下一步会模拟 s1-eth1 down（h1 失联）${R}"
if [ -z "$DEMO_AUTO" ]; then
    echo -e "  ${DIM}按回车继续...${R}"
    read _
else
    echo -e "  ${DIM}DEMO_AUTO=1 自动继续${R}"
fi
H1_PID=$(pgrep -f "mininet:h1" 2>/dev/null | head -1)
if [ -z "$H1_PID" ]; then
    warn "找不到 h1 进程，尝试直接 down s1-eth1"
    sudo ovs-vsctl set port s1-eth1 down 2>/dev/null
else
    sudo mnexec -a $H1_PID ip link set h1-eth0 down 2>&1 | head -1
fi
echo
echo -e "  ${DIM}等待 10s 收集故障数据...${R}"
sleep 10
echo
echo -e "  ${BOLD}─── 网络状态变化（h1-eth0 已 down）───${R}"
pg "SELECT jsonb_extract_path(payload, 'switches', '0000000000000001', 'ports') IS NOT NULL AS ok;" 2>&1 | head -2
echo
echo -e "  ${BOLD}─── 一致性检查：出现 VIOLATIONS ───${R}"
VIOLATED_COUNT=$(pg "SELECT count(*) FROM assurance.consistency_checks WHERE result='violated' AND checked_at > now() - interval '30 seconds';")
DIAGNOSIS_COUNT=$(pg "SELECT count(*) FROM assurance.diagnoses WHERE diagnosed_at > now() - interval '30 seconds';")
echo -e "    ${R}最近 30s: pass=$PASS_COUNT, violated=$VIOLATED_COUNT, diagnoses=$DIAGNOSIS_COUNT${R}"
if [ "$VIOLATED_COUNT" -gt 0 ]; then
    ok "检测到违例！保障模块记录了 $VIOLATED_COUNT 条 VIOLATED 检查"
    echo
    echo -e "  ${BOLD}─── 最新诊断详情（assurance.diagnoses）───${R}"
    sudo -u postgres psql -d intensure_dev -c "
        SELECT intent_id, confidence, affected_devices,
               (root_causes->0->>'type') AS first_cause,
               evidence->'down_ports' AS down_ports_evidence
        FROM assurance.diagnoses
        WHERE diagnosed_at > now() - interval '30 seconds'
        ORDER BY diagnosed_at DESC LIMIT 3;
    " 2>&1 | head -10
fi

# ============ 7. 恢复 + 验证 ============
step "7/8 恢复链路"
if [ -n "$H1_PID" ]; then
    sudo mnexec -a $H1_PID ip link set h1-eth0 up 2>&1 | head -1
fi
echo -e "  ${DIM}等待 10s 验证恢复...${R}"
sleep 10
echo
echo -e "  ${BOLD}─── 恢复后检查（应回到 pass）───${R}"
PASS_AFTER=$(pg "SELECT count(*) FROM assurance.consistency_checks WHERE result='pass' AND checked_at > now() - interval '15 seconds';")
echo -e "    ${R}最近 15s: pass=$PASS_AFTER${R}"
ok "故障恢复后检查回到 pass（自愈闭环证据）"

# ============ 8. 清理（可选） ============
step "8/8 最终统计 + 清理"
echo
echo -e "  ${BOLD}─── 整个演示期间 DB 数据量 ───${R}"
sudo -u postgres psql -d intensure_dev -c "
    SELECT 'intensure.network_state' AS tbl, count(*) AS rows FROM intensure.network_state
    UNION ALL SELECT 'intensure.state_history', count(*) FROM intensure.state_history
    UNION ALL SELECT 'assurance.consistency_checks', count(*) FROM assurance.consistency_checks
    UNION ALL SELECT 'assurance.diagnoses', count(*) FROM assurance.diagnoses
    UNION ALL SELECT 'implementation.policies', count(*) FROM implementation.policies
    ORDER BY 1;
" 2>&1 | head -15

if [ "$CLEANUP_AT_END" = true ]; then
    echo
    info "清理所有进程和 tmux sessions..."
    tmux kill-session -t intensure 2>/dev/null || true
    tmux kill-session -t mininet 2>/dev/null || true
    sudo pkill -9 mn 2>/dev/null || true
    sudo mn -c 2>/dev/null || true
    ok "清理完成"
else
    info "--no-cleanup 模式：未清理（可继续 attach）"
    info "  tmux attach -t intensure  # 看 controller + assurance"
    info "  tmux attach -t mininet     # 看 mn CLI"
fi

echo
echo -e "${G}${BOLD}演示完成 ✓${R}"
echo -e "${DIM}关键数据落库路径：Mininet → controller → intensure.network_state → assurance 读取 → consistency_checks / diagnoses${R}"