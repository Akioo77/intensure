#!/usr/bin/env bash
# ovs_attribution_demo.sh —— 链路故障的「证据链」演示
#
# 讲的故事：**这条链路到底为什么断了？**
#
#   故障注入 → 设备侧客观事实（OVS 日志）→ 我方事件时间线 → 三者对齐
#
# ⚠️ **这个 demo 会诚实地讲清边界**（演示时老师/对方最关心的就是这点）：
#   · OVS 日志提供的是**设备侧客观事实**（端口映射、包处理、协议状态）
#     —— 它证明"链路真的 down 了，不是我方数据造假的"
#   · 但"**是谁做的**"（哪个操作、什么时候）来自**我方操作审计**，
#     OVS 日志不会写"谁下的命令"
#   → 归因是「设备侧事实 + 我方操作记录」两条证据**交叉**得出的，
#     不是单靠 OVS 日志就能指认操作者
#
# 用法（在 VM 内，或能访问 8080 的地方）：
#   bash demo_scripts/ovs_attribution_demo.sh
#
# 依赖：采集层在跑（start_all.sh），OVS 日志采集器已启动（v3.22.0 起
#       state_server 会自动拉起；用 INTENSURE_OVS_LOG=0 可关）。
set -uo pipefail

export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

API="${INTENSURE_API:-http://127.0.0.1:8080}"
FAULT_TARGET="${1:-s1-eth2}"
DB_NAME="${INTENSURE_DB:-intensure_dev}"

# OVS 日志噪音很大（dbg 级别下 dpif 每秒几十条）→ 归因时按关键词过滤
LOG_FILTER="${2:-s1-eth2}"

step() { echo ""; echo "${C_BOLD}${C_CYAN}═══ $* ═══${C_RESET}"; }

# ============ 0. 前置检查 ============
step "0/5 前置检查"

if ! curl -s -m 3 --noproxy '*' -o /dev/null "${API}/api/health"; then
  ui_err "采集层没响应（$API）"
  ui_step "起服务: cd ~/projects/intensure && bash start_all.sh"
  exit 1
fi
ui_ok "采集层在线"

STATS=$(curl -s -m 5 --noproxy '*' "${API}/api/ovs/log/stats")
RUNNING=$(echo "$STATS" | json_get running)
BUF=$(echo "$STATS" | json_get buffer_size)
LOGFILE=$(echo "$STATS" | json_get log_file)
if [[ "$RUNNING" != "true" ]]; then
  ui_err "OVS 日志采集器没在跑 —— 归因没有设备侧证据"
  ui_step "看 ${API}/api/ovs/log/stats；state_server 启动时会自动拉起"
  ui_step "或用 INTENSURE_OVS_LOG=0 显式关闭（那就没有 OVS 侧证据了）"
  exit 1
fi
ui_ok "OVS 日志采集器在跑"
ui_kv "日志文件" "$LOGFILE"
ui_kv "已缓存" "${BUF:-0} 条"

# ============ 1. 基线 ============
step "1/5 记录注入前基线"
T0=$(python3 -c "import time; print(time.time())")
ui_kv "起始时间戳" "$T0"
BASE_PORT=$(curl -s -m 5 --noproxy '*' "${API}/api/state" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(''); raise SystemExit
for dpid,s in (d.get('switches') or {}).items():
    for p in (s.get('ports') or []):
        if p.get('name') == '${FAULT_TARGET}':
            print(p.get('state'))
")
ui_kv "端口 ${FAULT_TARGET} 状态" "${BASE_PORT:-未知}（OpenFlow state：4=UP, 1=DOWN）"

# ============ 2. 注入故障 ============
step "2/5 注入故障（link_down → ${FAULT_TARGET}）"
INJ=$(curl -s -m 15 --noproxy '*' -X POST "${API}/api/v1/test/inject-fault" \
  -H 'Content-Type: application/json' \
  -d "{\"fault_type\":\"link_down\",\"target\":\"${FAULT_TARGET}\",\"mode\":\"test\"}" 2>&1)
if echo "$INJ" | grep -q '"status":"ok"'; then
  ui_ok "故障已注入"
  echo "$INJ" | json_pretty | head -12
else
  ui_err "注入失败"
  echo "$INJ" | head -c 300
  exit 1
fi

echo ""
ui_step "等 8s 让设备侧状态与日志都稳定下来"
sleep 8

# ============ 3. 设备侧证据（OVS 日志）============
step "3/5 设备侧客观事实（OVS 日志）"
curl -s -m 10 --noproxy '*' "${API}/api/ovs/log?since=${T0}&limit=500" > /tmp/_ovs_win.json
python3 - "$LOG_FILTER" << 'PY'
import json, sys
key = sys.argv[1]
d = json.load(open('/tmp/_ovs_win.json'))
logs = d.get('logs') or []
print(f"  窗口内共 {len(logs)} 条日志（dbg 级别噪音大，下面按关键词过滤）")
mods = {}
for l in logs:
    mods[l['module']] = mods.get(l['module'], 0) + 1
print(f"  按模块: {mods}")

# 三类与「端口失效」相关的证据，优先级从强到弱
strong = [l for l in logs if 'nonexistent port' in l.get('msg', '')]
mid = [l for l in logs if key and key in l.get('msg', '')]
weak = [l for l in logs
        if any(w in l.get('msg', '').lower() for w in ('link state', 'port down', 'port up'))]

for label, group in (('设备侧明确承认端口失效', strong),
                     (f'涉及 {key} 的记录', mid),
                     ('链路状态变化类记录', weak)):
    if not group:
        continue
    print(f"  ▸ {label}（{len(group)} 条）：")
    for l in group[:5]:
        print(f"    [{l.get('ts_str')}] {l.get('module')}|{l.get('level')}| "
              f"{l.get('msg')[:110]}")
if not (strong or mid or weak):
    print("  ▸ 窗口内没有端口状态类记录 —— 说明这个动作在 OVS 侧不产生显式日志，")
    print("    端口 down 的权威事实来自**交换机给的 OpenFlow 端口状态**（见下一步），")
    print("    OVS 日志只是同期的设备侧上下文。")
PY

# ============ 4. 我方事件时间线 + 对齐 ============
step "4/5 我方事件时间线 + 与 OVS 日志对齐"
EVENTS=$(curl -s -m 8 --noproxy '*' "${API}/api/state" 2>/dev/null)
echo "$EVENTS" | python3 -c "
import json,sys
d=json.load(sys.stdin)
evs=[e for e in (d.get('events') or []) if e.get('type') in ('port_status','link_up','link_down','hosts_dropped','switch_disconnected')]
print('  最近的网络事件：')
for e in evs[-6:]:
    print('   ', e.get('type'), {k:v for k,v in e.items() if k not in ('type','ts','time')})
" 2>/dev/null || ui_warn "状态里没读到事件"

if command -v psql >/dev/null 2>&1; then
  ui_step "数据库里的端口事件（证据链的落库部分）"
  sudo -u postgres psql -d "$DB_NAME" -tAc \
    "SELECT to_char(occurred_at,'HH24:MI:SS')||'  '||port_name||'  '||event_type
     FROM intensure.port_events ORDER BY occurred_at DESC LIMIT 5;" 2>/dev/null \
    | sed 's/^/    /'
fi

ui_step "把端口事件与 OVS 日志做时间对齐（/api/ovs/log/correlate）"
CORR=$(curl -s -m 10 --noproxy '*' -X POST "${API}/api/ovs/log/correlate" \
  -H 'Content-Type: application/json' \
  -d "{\"link_events\":[{\"type\":\"link_down\",\"dpid\":\"0000000000000001\",\"port_no\":2,\"time\":$T0}],\"time_window\":5.0}" 2>&1)
echo "$CORR" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print('    (解析失败)', sys.stdin.read()[:200]); raise SystemExit
res=d.get('results') or []
print(f'    对齐结果 {len(res)} 组')
for r in res[:2]:
    le=r.get('link_event') or {}
    logs=r.get('ovs_logs') or []
    print(f\"    事件 {le.get('type')} @{le.get('time')} → 命中 {len(logs)} 条 OVS 日志\")
    for l in logs[:3]:
        print(f\"       [{l.get('module')}|{l.get('level')}] {l.get('msg')[:90]}\")
" 2>/dev/null || echo "    (对齐端点返回异常)"

# ============ 5. 恢复 + 结论 ============
step "5/5 恢复链路 + 结论"
curl -s -m 15 --noproxy '*' -X DELETE "${API}/api/v1/test/faults?target=${FAULT_TARGET}" \
  > /dev/null 2>&1
ui_ok "已恢复 ${FAULT_TARGET}"
sleep 5

echo ""
echo "${C_BOLD}${C_YELLOW}  结论（演示时照这个口径说，别夸大）${C_RESET}"
echo "${C_DIM}  ① 权威事实：端口 down 是**交换机直接告知的**（OpenFlow 端口状态 4=UP → 1=DOWN），${C_RESET}"
echo "${C_DIM}     不是我们自己算出来的 —— 这一点最经得起追问${C_RESET}"
echo "${C_DIM}  ② 可追溯：我方端口事件带精确时间戳落库（intensure.port_events）${C_RESET}"
echo "${C_DIM}  ③ 设备侧旁证：OVS 日志记录了故障时刻前后的设备侧活动（端口映射 / 包处理 /${C_RESET}"
echo "${C_DIM}     协议状态查询失败），用来交叉印证'那一刻设备侧确实在发生这些事'${C_RESET}"
echo "${C_DIM}  ④ 边界：**OVS 日志不写'谁下的命令'** —— 指认操作者要靠我方操作审计，${C_RESET}"
echo "${C_DIM}     两条证据交叉才敢下结论；采集层不做归因决策（那是保障模块的职责）${C_RESET}"
echo ""
ui_step "端口状态复核（交换机给的权威状态）："
curl -s -m 5 --noproxy '*' "${API}/api/state" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for dpid,s in (d.get('switches') or {}).items():
    for p in (s.get('ports') or []):
        if p.get('name') == '${FAULT_TARGET}':
            print('   ', p.get('name'), 'state =', p.get('state'), '(4=UP, 1=DOWN)')
" 2>/dev/null
echo ""
