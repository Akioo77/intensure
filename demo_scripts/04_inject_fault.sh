#!/usr/bin/env bash
# 04_inject_fault.sh - 注入故障（4 种可选）
# - link_down: 链路中断
# - high_latency: 延迟超标
# - packet_loss: 丢包
# - flow_missing: 策略漂移
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

source "$SCRIPT_DIR/.env" 2>/dev/null || { ui_err ".env 不存在，先跑 01"; exit 1; }

FAULT_TYPE="${1:-link_down}"

ui_title "Step 4: 故障注入 ($FAULT_TYPE)"

case "$FAULT_TYPE" in
  link_down|high_latency|packet_loss|flow_missing) ;;
  *)
    ui_err "未知故障类型: $FAULT_TYPE"
    ui_step "可选: link_down | high_latency | packet_loss | flow_missing"
    exit 1
    ;;
esac

ui_section "故障场景"
case "$FAULT_TYPE" in
  link_down)
    ui_step "物理链路中断（s1-eth2 down）"
    ui_step "期望 violation_types: REACHABILITY_FAILURE + POLICY_DRIFT" ;;
  high_latency)
    ui_step "延迟飙升到 300ms（阈值 50ms）"
    ui_step "期望 violation_types: LATENCY_EXCEEDED" ;;
  packet_loss)
    ui_step "丢包 50%（阈值 1%）"
    ui_step "期望 violation_types: PACKET_LOSS_EXCEEDED" ;;
  flow_missing)
    ui_step "期望流表条目缺失（policy 漂移）"
    ui_step "期望 violation_types: POLICY_DRIFT" ;;
esac

echo ""

# 问是否同时调用 intensure 的故障注入接口
ui_section "注入方式"
echo "  ${C_DIM}[1] 仅构造 ActualState（推荐，演示干净）${C_RESET}"
echo "  ${C_DIM}[2] 同时调用 intensure 故障注入接口（如果有）${C_RESET}"
echo ""
read -p "  选择 (默认 1): " CHOICE
CHOICE="${CHOICE:-1}"

if [[ "$CHOICE" == "2" ]]; then
  INJ=$(curl -s -X POST http://127.0.0.1:18080/api/v1/test/inject-fault \
    -H "Content-Type: application/json" \
    -d "{\"fault_type\": \"$FAULT_TYPE\", \"mode\": \"test\"}" 2>&1) || INJ=""
  if [[ -n "$INJ" ]] && ! echo "$INJ" | grep -q "404\|Not Found\|detail"; then
    ui_ok "intensure 故障注入成功"
    echo "$INJ" | head -c 300
    echo ""
  else
    ui_warn "intensure /api/v1/test/inject-fault 不可用"
    ui_step "回退到仅构造 ActualState"
  fi
fi

# 用 Python 构造故障 ActualState（写到独立文件避免 heredoc 嵌套问题）
FAULT_STATE=$(python3 << PYEOF
import json
from datetime import datetime, timezone, timedelta

base = {
    'intent_id': '$INTENT_ID',
    'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
    'source_host': '192.168.10.5',
    'destination_host': '10.0.50.10',
}

if "$FAULT_TYPE" == "link_down":
    actual = {
        **base,
        'reachable': False,
        'expected_flow_present': False,
        'down_links': ['s1-eth2'],
        'down_ports': [],
        'conflicting_flows': [],
        'latency_ms': None,
        'packet_loss': 1.0,
    }
elif "$FAULT_TYPE" == "high_latency":
    actual = {
        **base,
        'reachable': True,
        'expected_flow_present': True,
        'down_links': [],
        'down_ports': [],
        'conflicting_flows': [],
        'latency_ms': 300.0,
        'packet_loss': 0.0,
    }
elif "$FAULT_TYPE" == "packet_loss":
    actual = {
        **base,
        'reachable': True,
        'expected_flow_present': True,
        'down_links': [],
        'down_ports': [],
        'conflicting_flows': [],
        'latency_ms': 20.0,
        'packet_loss': 0.5,
    }
elif "$FAULT_TYPE" == "flow_missing":
    actual = {
        **base,
        'reachable': True,
        'expected_flow_present': False,
        'down_links': [],
        'down_ports': [],
        'conflicting_flows': [{'device': 's1', 'table_id': 0, 'cookie': 99999, 'reason': 'expected flow missing'}],
        'latency_ms': 15.0,
        'packet_loss': 0.0,
    }

print(json.dumps(actual, indent=2))
PYEOF
)

ui_section "故障 ActualState"
echo "$FAULT_STATE"

# 保存
echo "$FAULT_STATE" > "$SCRIPT_DIR/.fault_state.json"
ui_ok "已保存到 .fault_state.json"

echo ""
echo "${C_BOLD}${C_YELLOW}  ⚡ 故障 $FAULT_TYPE 已构造${C_RESET}"
echo "${C_DIM}  下一步: bash 05_check_violated.sh${C_RESET}"

ui_pause
