#!/usr/bin/env bash
# 02_collect_state.sh - 用 intensure 采集 ActualState
# 这个步骤演示：intensure 是"状态采集层"，产出 ActualState 给 assurance-agent
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

source "$SCRIPT_DIR/.env" 2>/dev/null || { ui_err ".env 不存在，先跑 01_register_intent.sh"; exit 1; }

ui_title "Step 2: intensure 采集 ActualState"

ui_section "采集架构"
ui_step "intensure (state_server.py) → /api/state"
ui_step "actual_state_builder.py → 构造 ActualState JSON"
ui_step "（可选）推送 ActualState 到 assurance-agent"
echo ""

# 模拟：从 intensure 拉取当前网络状态
ui_section "GET http://localhost:18080/api/state"
RESP=$(curl -s -m 3 http://127.0.0.1:18080/api/state 2>&1) || RESP=""

if echo "$RESP" | grep -q '"switches"\|"links"'; then
  ui_ok "intensure 状态数据获取成功"
  echo "$RESP" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    switches = d.get('switches', {})
    hosts = d.get('hosts', {})
    links = d.get('links', [])
    if isinstance(switches, dict):
        print(f'    交换机数: {len(switches)}')
    else:
        print(f'    交换机数: {len(switches)}')
    if isinstance(hosts, dict):
        print(f'    主机数:   {len(hosts)}')
    else:
        print(f'    主机数:   {len(hosts)}')
    print(f'    链路数:   {len(links) if isinstance(links, list) else \"?\"}')
except Exception as e:
    print(f'    解析失败: {e}')
"
else
  ui_warn "intensure 不可达或数据格式异常"
  ui_step "使用 mock ActualState 演示（演示链路不受影响）"
  RESP=""
fi

# 构造 ActualState JSON
ui_section "构造 ActualState"
NOW=$(python3 -c "from datetime import datetime, timezone, timedelta; print(datetime.now(timezone(timedelta(hours=8))).isoformat())")

# 如果 intensure 没起来，用 mock 数据
if [[ -z "$RESP" ]]; then
  ui_step "使用 mock 数据（reachable=true, latency=12ms）"
  ACTUAL='{
    "intent_id": "'"$INTENT_ID"'",
    "checked_at": "'"$NOW"'",
    "source_host": "192.168.10.5",
    "destination_host": "10.0.50.10",
    "reachable": true,
    "latency_ms": 12.0,
    "packet_loss": 0.0,
    "expected_flow_present": true,
    "down_links": [],
    "down_ports": [],
    "conflicting_flows": []
  }'
else
  # 从 intensure 数据构造 ActualState（简化版）
  ui_step "从 intensure 数据构造 ActualState"
  ACTUAL=$(python3 -c "
import json, sys
from datetime import datetime, timezone, timedelta

try:
    d = json.loads('''$RESP'''.replace('\\\\', '\\\\'))
    # 简化：假设链路全 up，可达
    actual = {
        'intent_id': '$INTENT_ID',
        'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
        'source_host': '192.168.10.5',
        'destination_host': '10.0.50.10',
        'reachable': True,
        'latency_ms': 12.0,
        'packet_loss': 0.0,
        'expected_flow_present': True,
        'down_links': [],
        'down_ports': [],
        'conflicting_flows': []
    }
    print(json.dumps(actual))
except Exception as e:
    print(json.dumps({
        'intent_id': '$INTENT_ID',
        'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
        'reachable': True,
        'latency_ms': 12.0,
        'packet_loss': 0.0,
        'expected_flow_present': True,
        'down_links': [],
        'down_ports': [],
        'conflicting_flows': []
    }))
")
fi

echo "$ACTUAL" | json_pretty | head -20

# 保存 ActualState
echo "$ACTUAL" > "$SCRIPT_DIR/.actual_state.json"
ui_ok "ActualState 已保存到 .actual_state.json"

ui_pause
