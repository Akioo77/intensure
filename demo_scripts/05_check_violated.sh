#!/usr/bin/env bash
# 05_check_violated.sh - 推送故障状态，触发 assurance-agent VIOLATED 状态
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

source "$SCRIPT_DIR/.env" 2>/dev/null || { ui_err ".env 不存在"; exit 1; }

ui_title "Step 5: 检测违规（推故障 ActualState）"

ui_section "演示流"
ui_step "POST $BASE/api/v1/assurance/check"
ui_step "（携带故障 ActualState）"
ui_step "期望结果: status=VIOLATED, violations>=1"
echo ""

if [[ ! -f "$SCRIPT_DIR/.fault_state.json" ]]; then
  ui_err ".fault_state.json 不存在，先跑 04_inject_fault.sh"
  exit 1
fi

FAULT=$(cat "$SCRIPT_DIR/.fault_state.json")
BODY_FILE=$(mktemp)
python3 - "$INTENT_ID" "$FAULT" > "$BODY_FILE" <<'PYEOF'
import json, sys
intent_id = sys.argv[1]
fault = json.loads(sys.argv[2])
print(json.dumps({"intent_id": intent_id, "actual": fault}))
PYEOF

RESP=$(curl -s -X POST "$BASE/api/v1/assurance/check" \
  -H "Content-Type: application/json" \
  --data-binary @"$BODY_FILE" 2>&1)
rm -f "$BODY_FILE"

STATUS=$(echo "$RESP" | json_get "status")
VIOLATIONS_COUNT=$(echo "$RESP" | python3 -c "import sys, json; d=json.load(sys.stdin); print(len(d.get('violations',[])))" 2>/dev/null)

if [[ "$STATUS" == "VIOLATED" ]]; then
  ui_ok "检测到违规！状态: $(status_badge "$STATUS")"
  ui_kv "violations 数量" "$VIOLATIONS_COUNT"
  echo ""
  echo "${C_DIM}  Violation 详情:${C_RESET}"
  echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for i, v in enumerate(d.get('violations', []), 1):
    print(f'    [{i}] types: {v.get(\"violation_types\", [])}')
    exp = v.get('expected', {})
    act = v.get('actual', {})
    print(f'        expected.reachable={exp.get(\"reachable\")}, actual.reachable={act.get(\"reachable\")}')
    if v.get('down_links'):
        print(f'        down_links: {v.get(\"down_links\")}')
"
else
  ui_warn "状态异常: $(status_badge "$STATUS")"
  echo "$RESP" | json_pretty | head -30
fi

# 保存
echo "$RESP" > "$SCRIPT_DIR/.check_violated.json"

echo ""
echo "${C_BOLD}${C_YELLOW}  🔥 违规已记录${C_RESET}"
echo "${C_DIM}  下一步: 跑 06_diagnose.sh 做根因分析${C_RESET}"

ui_pause
