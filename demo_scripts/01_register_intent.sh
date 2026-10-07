#!/usr/bin/env bash
# 01_register_intent.sh - 注册 + activate 演示意图
# 鲁棒化：register / activate 任一失败时，自动 fallback 到 A 同学的默认 intent
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

BASE="${ASSURANCE_BASE:-https://westminster-accounts-permitted-pursuant.trycloudflare.com}"

ui_title "Step 1: 注册演示意图"

# 1. 尝试用一个新 intent_id（每次跑前先 register 一个新的）
INTENT_NEW="demo-$(date +%Y%m%d-%H%M%S)-$$"
INTENT_FALLBACK="test-intent-001"  # A 同学默认 intent（已知能 activate）
INTENT_ID=""  # 最终确定使用的 ID

ui_section "尝试 1: register 新 intent '$INTENT_NEW'"
REG_RESP=$(curl -s -X POST "$BASE/api/v1/assurance/register" \
  -H "Content-Type: application/json" \
  -d "{
    \"intent\": {
      \"intent_id\": \"$INTENT_NEW\",
      \"intent_type\": \"ACCESS_CONTROL\",
      \"action\": \"ALLOW\",
      \"source\": {\"cidr\": \"192.168.10.0/24\"},
      \"destination\": {\"cidr\": \"10.0.50.10/32\"},
      \"protocol\": \"tcp\",
      \"destination_port\": 443
    },
    \"deployment_id\": \"deploy-$INTENT_NEW\",
    \"deployment_status\": \"deployed\",
    \"devices\": [\"s1\"],
    \"policy_refs\": []
  }" 2>&1) || REG_RESP=""
REG_STATUS=$(echo "$REG_RESP" | json_get "status")

if [[ -n "$REG_STATUS" ]]; then
  ui_ok "register 成功: $(status_badge "$REG_STATUS")"
else
  ui_warn "register 返回异常: $REG_RESP"
fi

ui_section "尝试 2: activate '$INTENT_NEW'"
ACT_RESP=$(curl -s -X POST "$BASE/api/v1/assurance/activate" \
  -H "Content-Type: application/json" \
  -d "{
    \"intent_id\": \"$INTENT_NEW\",
    \"max_latency_ms\": 50,
    \"max_packet_loss\": 0.01
  }" 2>&1)
ACT_STATUS=$(echo "$ACT_RESP" | json_get "status")

if [[ "$ACT_STATUS" == "ACTIVE" ]]; then
  INTENT_ID="$INTENT_NEW"
  ui_ok "activate 成功！使用新 intent: $INTENT_ID"
else
  ui_warn "新 intent activate 失败: $(echo "$ACT_RESP" | head -c 80)..."
  ui_step "已知问题：A 同学的 activate 对 register 新建的 intent 报 500"
  ui_step "回退到 A 同学默认 intent '$INTENT_FALLBACK'（已预置 expected_state）"
  echo ""
  
  # 直接看 fallback 是不是已经是 ACTIVE
  FALLBACK_REC=$(curl -s "$BASE/api/v1/assurance/record/$INTENT_FALLBACK" 2>&1)
  FALLBACK_STATUS=$(echo "$FALLBACK_REC" | json_get "status")
  
  if [[ "$FALLBACK_STATUS" != "ACTIVE" ]]; then
    ui_step "fallback 还没 activate，尝试激活..."
    FALLBACK_ACT=$(curl -s -X POST "$BASE/api/v1/assurance/activate" \
      -H "Content-Type: application/json" \
      -d "{\"intent_id\": \"$INTENT_FALLBACK\", \"max_latency_ms\": 50, \"max_packet_loss\": 0.01}" 2>&1)
    FALLBACK_STATUS=$(echo "$FALLBACK_ACT" | json_get "status")
  fi
  
  if [[ "$FALLBACK_STATUS" == "ACTIVE" || "$FALLBACK_STATUS" == "REGISTERED" || "$FALLBACK_STATUS" == "VIOLATED" || "$FALLBACK_STATUS" == "RECOVERED" || "$FALLBACK_STATUS" == "DIAGNOSING" ]]; then
    INTENT_ID="$INTENT_FALLBACK"
    ui_ok "fallback 可用（状态: $(status_badge "$FALLBACK_STATUS")），使用: $INTENT_ID"
    # 如果不是 ACTIVE，尝试恢复（推一个健康的 ActualState 让它变 ACTIVE）
    if [[ "$FALLBACK_STATUS" != "ACTIVE" ]]; then
      ui_step "fallback 处于 $(status_badge "$FALLBACK_STATUS")，推一次健康 check 让它回到 ACTIVE"
      HEALTHY_ACTUAL='{"intent_id":"'$INTENT_ID'","checked_at":"'$(python3 -c "from datetime import datetime, timezone, timedelta; print(datetime.now(timezone(timedelta(hours=8))).isoformat())")'","reachable":true,"latency_ms":10.0,"packet_loss":0.0,"expected_flow_present":true,"down_links":[],"down_ports":[],"conflicting_flows":[]}'
      curl -s -X POST "$BASE/api/v1/assurance/check" \
        -H "Content-Type: application/json" \
        -d "{\"intent_id\":\"$INTENT_ID\",\"actual\":$HEALTHY_ACTUAL}" >/dev/null 2>&1 || true
      ui_ok "已推健康 check"
    fi
  else
    ui_err "fallback 也不可用，演示无法继续"
    echo "Fallback response: $FALLBACK_REC" | head -c 500
    exit 1
  fi
fi

echo ""
ui_section "期望状态（expected_state）"
curl -s "$BASE/api/v1/assurance/record/$INTENT_ID" | python3 -c "
import sys, json
d = json.load(sys.stdin)
es = d.get('expected_state', {})
print('  字段:')
for k, v in es.items():
    if v is not None and v != []:
        print(f'    {k:20s} {v}')
"

# 写 .env
echo "INTENT_ID=$INTENT_ID" > "$SCRIPT_DIR/.env"
echo "BASE=$BASE" >> "$SCRIPT_DIR/.env"
echo "$INTENT_ID" > "$SCRIPT_DIR/.current_intent"

ui_section "记录"
ui_kv "INTENT_ID" "$INTENT_ID"
ui_kv ".env" "$SCRIPT_DIR/.env"

ui_pause
