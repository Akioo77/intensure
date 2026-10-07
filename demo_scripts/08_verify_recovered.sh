#!/usr/bin/env bash
# 08_verify_recovered.sh - 验证恢复（推送"已恢复"的 ActualState）
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

source "$SCRIPT_DIR/.env" 2>/dev/null || { ui_err ".env 不存在"; exit 1; }

ui_title "Step 8: 验证恢复"

ui_section "演示流"
ui_step "POST $BASE/api/v1/assurance/verify"
ui_step "（携带"已恢复"的 actual_after_heal）"
ui_step "期望结果: status=RECOVERED, healing_verdict=SUCCESS"
echo ""

NOW=$(python3 -c "from datetime import datetime, timezone, timedelta; print(datetime.now(timezone(timedelta(hours=8))).isoformat())")

RECOVERED_ACTUAL=$(python3 << PYEOF
import json
from datetime import datetime, timezone, timedelta

actual = {
    'intent_id': '$INTENT_ID',
    'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
    'source_host': '192.168.10.5',
    'destination_host': '10.0.50.10',
    'reachable': True,
    'latency_ms': 15.0,
    'packet_loss': 0.0,
    'expected_flow_present': True,
    'down_links': [],
    'down_ports': [],
    'conflicting_flows': []
}
print(json.dumps(actual))
PYEOF
)

ui_section "POST /api/v1/assurance/verify"
echo "$RECOVERED_ACTUAL" | json_pretty | head -15
echo ""

BODY_FILE=$(mktemp)
python3 - "$INTENT_ID" "$RECOVERED_ACTUAL" > "$BODY_FILE" <<'PYEOF'
import json, sys
intent_id = sys.argv[1]
actual = json.loads(sys.argv[2])
print(json.dumps({"intent_id": intent_id, "actual_after_heal": actual}))
PYEOF

RESP=$(curl -s -X POST "$BASE/api/v1/assurance/verify" \
  -H "Content-Type: application/json" \
  --data-binary @"$BODY_FILE" 2>&1)
rm -f "$BODY_FILE"

STATUS=$(echo "$RESP" | json_get "status")
HEALING_VERDICT=$(echo "$RESP" | json_get "healing_verdict")

if [[ "$STATUS" == "RECOVERED" ]]; then
  ui_ok "恢复成功！状态: $(status_badge "$STATUS")"
  ui_kv "healing_verdict" "$HEALING_VERDICT"
elif [[ "$STATUS" == "VIOLATED" ]] && [[ "$HEALING_VERDICT" == "BLOCKED" ]]; then
  ui_warn "自愈被闸门拦截（这是设计预期）"
  ui_step "状态: $(status_badge "$STATUS")"
  ui_step "healing_verdict: $HEALING_VERDICT"
  ui_step "如果想看恢复，需要先在 04 注入的故障被实际修复"
  ui_step "（演示时可以手动调 intensure 的故障恢复接口）"
else
  echo "  状态: $(status_badge "$STATUS")"
  echo "  healing_verdict: $HEALING_VERDICT"
fi

echo "$RESP" > "$SCRIPT_DIR/.verify.json"

ui_section "完整状态历史"
curl -s "$BASE/api/v1/assurance/record/$INTENT_ID" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'    最终状态: {d.get(\"status\")}')
print()
print('    状态机轨迹:')
for h in d.get('status_history', []):
    print(f'      {h[\"status\"]:12s} @ {h[\"at\"]}')
print()
print(f'    重试: {d.get(\"heal_retries\")}/{d.get(\"max_heal_retries\")}')
"

echo ""
echo "${C_BOLD}${C_GREEN}  ✅ 端到端闭环演示完成！${C_RESET}"
echo ""
echo "${C_DIM}  清理: bash 99_reset.sh${C_RESET}"

ui_pause
