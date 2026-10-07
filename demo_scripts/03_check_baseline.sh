#!/usr/bin/env bash
# 03_check_baseline.sh - 基线检查：把 ActualState 推给 assurance-agent
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

source "$SCRIPT_DIR/.env" 2>/dev/null || { ui_err ".env 不存在"; exit 1; }

ui_title "Step 3: 基线检查（POST ActualState → assurance-agent）"

ui_section "演示流"
ui_step "intensure 采集 ActualState"
ui_step "POST $BASE/api/v1/assurance/check"
ui_step "assurance-agent 比对 expected vs actual"
ui_step "期望结果: status=ACTIVE, violations=[]"
echo ""

if [[ ! -f "$SCRIPT_DIR/.actual_state.json" ]]; then
  ui_err ".actual_state.json 不存在，先跑 02_collect_state.sh"
  exit 1
fi

# 推 ActualState 做基线检查
ui_section "POST /api/v1/assurance/check (Baseline)"
ACTUAL=$(cat "$SCRIPT_DIR/.actual_state.json")
# 用临时文件构造请求体，避免嵌套引号问题
BODY_FILE=$(mktemp)
python3 - "$INTENT_ID" "$ACTUAL" > "$BODY_FILE" <<'PYEOF'
import json, sys
intent_id = sys.argv[1]
actual = json.loads(sys.argv[2])
print(json.dumps({"intent_id": intent_id, "actual": actual}))
PYEOF

RESP=$(curl -s -X POST "$BASE/api/v1/assurance/check" \
  -H "Content-Type: application/json" \
  --data-binary @"$BODY_FILE" 2>&1)
rm -f "$BODY_FILE"

STATUS=$(echo "$RESP" | json_get "status")
VIOLATIONS=$(echo "$RESP" | python3 -c "import sys, json; d=json.load(sys.stdin); print(len(d.get('violations',[])))" 2>/dev/null)

if [[ "$STATUS" == "ACTIVE" ]]; then
  ui_ok "基线正常，状态: $(status_badge "$STATUS")"
  ui_kv "violations" "$VIOLATIONS"
  echo ""
  echo "${C_DIM}  健康基线建立 ✓ — 后续故障检测将以此为对比基准${C_RESET}"
else
  ui_warn "基线检查返回异常状态: $(status_badge "$STATUS")"
  ui_step "可能原因: 之前已经检查过，系统已有状态"
  echo "$RESP" | json_pretty | head -30
fi

# 保存 baseline 检查结果
echo "$RESP" > "$SCRIPT_DIR/.check_baseline.json"

ui_pause
