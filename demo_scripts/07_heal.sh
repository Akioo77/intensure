#!/usr/bin/env bash
# 07_heal.sh - 自愈（生成 healing_intent + guard_checks 评估）
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

source "$SCRIPT_DIR/.env" 2>/dev/null || { ui_err ".env 不存在"; exit 1; }

ui_title "Step 7: 自愈（healing_intent + guard_checks）"

ui_section "演示流"
ui_step "POST $BASE/api/v1/assurance/heal?intent_id=$INTENT_ID"
ui_step "assurance-agent 生成 healing_intent（包含 goal + primitives + strategy）"
ui_step "guard_checks 评估 14 项安全闸门"
ui_step "返回: verdict=ALLOW 或 BLOCKED（被某闸门拦截）"
echo ""

RESP=$(curl -s -X POST "$BASE/api/v1/assurance/heal?intent_id=$INTENT_ID" 2>&1)

VERDICT=$(echo "$RESP" | json_get "verdict")
if [[ "$VERDICT" == "ALLOW" ]]; then
  ui_ok "自愈决策: ALLOW"
elif [[ "$VERDICT" == "BLOCKED" ]]; then
  ui_warn "自愈决策: BLOCKED（被闸门拦截，这是设计预期 — 安全第一）"
else
  echo "  决策: $VERDICT"
fi

echo ""
echo "${C_BOLD}${C_CYAN}  🩹 Healing Intent${C_RESET}"
echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
hi = d.get('healing_intent') or {}
print(f'    healing_id:  {hi.get(\"healing_id\")}')
print(f'    goal:        {hi.get(\"goal\")}')
print(f'    priority:    {hi.get(\"priority\")}')
print(f'    goal_source: {hi.get(\"goal_source\")}')

ctx = hi.get('context', {})
if ctx:
    print(f'    context:     {list(ctx.keys())[:5]}')

print()
print('    触发的 violation 类型:')
for vt in hi.get('violated_types', []):
    print(f'      - {vt}')
"

echo ""
echo "${C_BOLD}${C_CYAN}  🚧 Guard Checks (14 项闸门)${C_RESET}"
echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
checks = d.get('guard_checks', [])
passed = sum(1 for c in checks if c.get('passed'))
print(f'    通过: {passed}/{len(checks)}')
print()
for c in checks:
    icon = '✓' if c.get('passed') else '✗'
    color = '\033[32m' if c.get('passed') else '\033[31m'
    level = c.get('level', '?')
    print(f'    {color}{icon}\033[0m [{c.get(\"gate\", \"?\")}] {c.get(\"rule\", \"?\")} ({level})')
    reason = c.get('reason', '')
    if reason:
        print(f'        ↳ {reason[:90]}')
"

if [[ -n "$(echo "$RESP" | json_get "blocked_by")" ]]; then
  echo ""
  echo "${C_BOLD}${C_RED}  ❌ Blocked By:${C_RESET}"
  echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for b in d.get('blocked_by', []):
    print(f'    - {b}')
"
fi

echo "$RESP" > "$SCRIPT_DIR/.heal.json"

echo ""
echo "${C_BOLD}${C_YELLOW}  🩹 自愈评估完成${C_RESET}"
echo "${C_DIM}  下一步: 跑 08_verify_recovered.sh 验证（如果 ALLOW）${C_RESET}"

ui_pause
