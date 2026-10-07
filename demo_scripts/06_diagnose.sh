#!/usr/bin/env bash
# 06_diagnose.sh - 根因诊断（确定性算法）
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

source "$SCRIPT_DIR/.env" 2>/dev/null || { ui_err ".env 不存在"; exit 1; }

ui_title "Step 6: 根因诊断"

ui_section "演示流"
ui_step "POST $BASE/api/v1/assurance/diagnose?intent_id=$INTENT_ID"
ui_step "assurance-agent 跑诊断器"
ui_step "返回: root_causes + confidence + 候选原因 + 修复策略"
echo ""

RESP=$(curl -s -X POST "$BASE/api/v1/assurance/diagnose?intent_id=$INTENT_ID" 2>&1)

STATUS=$(echo "$RESP" | json_get "status")
if [[ "$STATUS" == "DIAGNOSING" ]]; then
  ui_ok "诊断完成，状态: $(status_badge "$STATUS")"
else
  ui_warn "状态: $(status_badge "$STATUS")"
fi

echo ""
echo "${C_BOLD}${C_CYAN}  📋 诊断结果${C_RESET}"
echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
diag = d.get('diagnosis') or {}
print(f'    置信度: {diag.get(\"confidence\", \"?\")}')

print()
print('    根因列表:')
for i, rc in enumerate(diag.get('root_causes', []), 1):
    print(f'    [{i}] {rc.get(\"cause_type\", \"?\")}')
    desc = rc.get('description', '')
    print(f'        描述: {desc[:100]}...' if len(desc) > 100 else f'        描述: {desc}')

    ev = rc.get('evidence', {})
    if ev.get('match_score'):
        print(f'        匹配度: {ev.get(\"match_score\")}')
    if ev.get('confirmed_signals'):
        print(f'        确认信号: {ev.get(\"confirmed_signals\")[:3]}')

    # 修复策略
    strategies = ev.get('repair_strategies', [])
    if strategies:
        print(f'        修复策略 ({len(strategies)} 个):')
        for s in strategies[:3]:
            print(f'          - {s.get(\"strategy\")} (risk: {s.get(\"risk_level\", \"?\")})')
"

echo "$RESP" > "$SCRIPT_DIR/.diagnose.json"

echo ""
echo "${C_BOLD}${C_YELLOW}  🔍 诊断完成${C_RESET}"
echo "${C_DIM}  下一步: 跑 07_heal.sh 触发自愈${C_RESET}"

ui_pause
