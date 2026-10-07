#!/usr/bin/env bash
# 00_pre_check.sh - 联调预检（5 项：VM / 隧道 / intensure / assurance-agent / Mininet）
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

ui_title "Step 0: 联调预检"

PASS=0
FAIL=0

# 1. Colima VM
ui_section "[1/5] Colima VM 状态"
if command -v colima >/dev/null 2>&1; then
  if colima list 2>/dev/null | grep -q "Running"; then
    ui_ok "Colima VM 正在运行"
    colima list | grep -v "PROFILE" | head -1 | while read line; do
      ui_kv "VM 信息" "$line"
    done
    PASS=$((PASS+1))
  else
    ui_err "Colima VM 未运行"
    ui_warn "运行: colima start"
    FAIL=$((FAIL+1))
  fi
else
  ui_err "Colima 命令未找到"
  FAIL=$((FAIL+1))
fi

# 2. Dashboard 隧道
ui_section "[2/5] Dashboard 隧道（localhost:18080）"
if lsof -iTCP:18080 -sTCP:LISTEN >/dev/null 2>&1; then
  ui_ok "端口 18080 LISTEN"
  PID=$(lsof -iTCP:18080 -sTCP:LISTEN -t 2>/dev/null | head -1)
  ui_kv "PID" "$PID"
  PASS=$((PASS+1))
else
  ui_warn "端口 18080 未监听（演示时需要 dashboard 可视化）"
  ui_warn "起隧道: ssh -L 18080:127.0.0.1:18080 -N -f"
  FAIL=$((FAIL+1))
fi

# 3. intensure 本地服务
ui_section "[3/5] intensure 本地服务"
RESP=$(curl -s -m 3 http://127.0.0.1:18080/api/state 2>&1 || echo "FAIL")
HTTP=$(echo "$RESP" | head -c 20)
if echo "$RESP" | grep -q '"switches"\|"hosts"\|"links"'; then
  ui_ok "intensure 服务正常响应"
  PASS=$((PASS+1))
else
  ui_warn "intensure 服务未响应（演示时不需要，联调需要）"
  ui_warn "起服务: cd ~/workspace/projects/intensure && python3 state_server.py &"
  FAIL=$((FAIL+1))
fi

# 4. A 同学 assurance-agent（重试 3 次，应对 cloudflared 抖动）
ui_section "[4/5] A 同学 assurance-agent"
BASE="${ASSURANCE_BASE:-https://westminster-accounts-permitted-pursuant.trycloudflare.com}"
HEALTH=""
for i in 1 2 3; do
  HEALTH=$(curl -s -m 10 "$BASE/api/health" 2>&1 || echo "")
  if echo "$HEALTH" | grep -q '"ok"'; then
    break
  fi
  ui_warn "第 $i 次失败，重试..."
  sleep 2
done
if echo "$HEALTH" | grep -q '"ok"'; then
  ui_ok "assurance-agent 健康"
  ui_kv "地址" "$BASE"
  SERVICE=$(echo "$HEALTH" | json_get "service")
  RECORDS=$(echo "$HEALTH" | json_get "records")
  ui_kv "服务名" "$SERVICE"
  ui_kv "当前记录数" "$RECORDS"
  PASS=$((PASS+1))
else
  ui_err "assurance-agent 不可达"
  ui_warn "地址: $BASE"
  ui_warn "联系 A 同学重启 cloudflared"
  FAIL=$((FAIL+1))
fi

# 5. Mininet（演示时需要）
ui_section "[5/5] Mininet（可选，演示时用）"
if command -v mn >/dev/null 2>&1 || ssh -o ConnectTimeout=3 -o BatchMode=yes colima true 2>/dev/null; then
  ui_ok "Mininet 环境就绪（演示时启用）"
  PASS=$((PASS+1))
else
  ui_warn "Mininet 未就绪（演示时用 mock 数据即可）"
  PASS=$((PASS+1))  # 不算失败
fi

echo ""
echo "${C_BOLD}═══════════════════════════════════════════════════════${C_RESET}"
if [[ $FAIL -eq 0 ]]; then
  echo "${C_BOLD}${C_GREEN}  ✅ 预检全部通过 ($PASS/$((PASS+FAIL)))${C_RESET}"
else
  echo "${C_BOLD}${C_YELLOW}  ⚠️  $FAIL 项不通过（$PASS/$((PASS+FAIL))）${C_RESET}"
fi
echo "${C_BOLD}═══════════════════════════════════════════════════════${C_RESET}"

# 写预检报告
echo "{\"timestamp\":\"$(date -Iseconds)\",\"pass\":$PASS,\"fail\":$FAIL}" \
  > "$SCRIPT_DIR/.pre_check.json"

exit $FAIL
