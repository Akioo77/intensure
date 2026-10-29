#!/usr/bin/env bash
# 99_reset.sh - 演示后清理（清空环境状态 + 删除临时文件）
set -uo pipefail
# 本地地址不走代理：VM 的 /etc/environment 设了 http_proxy 但没设 no_proxy，
# 连 127.0.0.1 都会被代理劫持 → curl 返回 502/空 → 健康检查假失败。
export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/ui.sh"

source "$SCRIPT_DIR/.env" 2>/dev/null || {
  ui_warn ".env 不存在，只清理本地临时文件"
  rm -f "$SCRIPT_DIR"/{.current_intent,.assurance_base,.env,.actual_state.json,.fault_state.json,.check_baseline.json,.check_violated.json,.diagnose.json,.heal.json,.verify.json,.pre_check.json}
  ui_ok "本地临时文件已清理"
  exit 0
}

ui_title "Step 99: 清理"

ui_section "删除中间状态文件"
rm -f "$SCRIPT_DIR"/{.current_intent,.assurance_base,.env,.actual_state.json,.fault_state.json,.check_baseline.json,.check_violated.json,.diagnose.json,.heal.json,.verify.json,.pre_check.json}
ui_ok "本地临时文件已清理"

ui_section "assurance-agent 记录保留"
ui_step "A 同学那边的记录会保留（用于审计和回放）"
ui_step "如果想彻底清理，联系 A 同学"

echo ""
echo "${C_BOLD}${C_GREEN}  ✅ 清理完成${C_RESET}"
echo ""
