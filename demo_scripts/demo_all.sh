#!/usr/bin/env bash
# demo_all.sh - 一键端到端演示（intensure + assurance-agent 协同）
# 用法: bash demo_all.sh                # 交互模式（按回车继续）
#       bash demo_all.sh --auto          # 自动模式（不暂停）
#       bash demo_all.sh --skip=00       # 跳过预检
#       bash demo_all.sh --fault=high_latency  # 指定故障类型
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 解析参数
AUTO=0
SKIP_STEPS=""
FAULT_TYPE="link_down"
for arg in "$@"; do
  case "$arg" in
    --auto) AUTO=1; SKIP_WAIT=1 ;;
    --skip=*) SKIP_STEPS="${arg#--skip=}" ;;
    --fault=*) FAULT_TYPE="${arg#--fault=}" ;;
    --help|-h)
      echo "用法: bash demo_all.sh [选项]"
      echo ""
      echo "选项:"
      echo "  --auto              自动模式（不等待按键）"
      echo "  --skip=00,01,...    跳过指定步骤"
      echo "  --fault=TYPE        故障类型: link_down | high_latency | packet_loss | flow_missing"
      echo "  --help              显示帮助"
      exit 0
      ;;
  esac
done
export SKIP_WAIT=${SKIP_WAIT:-0}

# 决定哪些步骤要跑
should_run() {
  local step="$1"
  if [[ -z "$SKIP_STEPS" ]]; then
    return 0
  fi
  [[ ",$SKIP_STEPS," != *",$step,"* ]]
}

# 大标题
clear 2>/dev/null || true
cat << 'BANNER'
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║     intensure + assurance-agent 端到端协同演示                 ║
║     ─────────────────────────────────────────                 ║
║     intensure = 状态采集 + 可视化                              ║
║     assurance-agent = 一致性 + 诊断 + 自愈 + 验证              ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
BANNER
echo ""
if [[ "$AUTO" == "1" ]]; then
  echo "▶ 自动模式（不暂停）"
else
  echo "▶ 交互模式（每步按回车继续）"
fi
echo "▶ 故障类型: $FAULT_TYPE"
echo ""

# 按顺序跑
ERR=0

if should_run "00"; then
  bash "$SCRIPT_DIR/00_pre_check.sh" || { echo "❌ 预检失败，退出"; exit 1; }
fi

if should_run "01"; then
  bash "$SCRIPT_DIR/01_register_intent.sh" || ERR=$?
fi

if should_run "02"; then
  bash "$SCRIPT_DIR/02_collect_state.sh" || ERR=$?
fi

if should_run "03"; then
  bash "$SCRIPT_DIR/03_check_baseline.sh" || ERR=$?
fi

if should_run "04"; then
  bash "$SCRIPT_DIR/04_inject_fault.sh" "$FAULT_TYPE" || ERR=$?
fi

if should_run "05"; then
  bash "$SCRIPT_DIR/05_check_violated.sh" || ERR=$?
fi

if should_run "06"; then
  bash "$SCRIPT_DIR/06_diagnose.sh" || ERR=$?
fi

if should_run "07"; then
  bash "$SCRIPT_DIR/07_heal.sh" || ERR=$?
fi

if should_run "08"; then
  bash "$SCRIPT_DIR/08_verify_recovered.sh" || ERR=$?
fi

# 总结
echo ""
echo "═══════════════════════════════════════════════════════════════"
if [[ $ERR -eq 0 ]]; then
  echo "  ✅ 演示完成（所有步骤跑通）"
else
  echo "  ⚠️  演示完成（部分步骤有错误，错误码: $ERR）"
fi
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "清理: bash $SCRIPT_DIR/99_reset.sh"

exit $ERR
