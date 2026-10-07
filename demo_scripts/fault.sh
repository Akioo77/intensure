#!/bin/bash
# fault.sh - 演示用故障注入包装（Mac 侧，一条命令搞定）
#
# 用法（在 Mac 终端跑）:
#   bash fault.sh cut           # 💥 断骨干链路（最常用！演示"链路中断"）
#   bash fault.sh restore       # ✅ 恢复骨干链路
#   bash fault.sh delay         # 🐢 加时延（演示"性能退化"）
#   bash fault.sh loss          # 📉 加丢包
#   bash fault.sh show          # 📊 查看当前状态
#   bash fault.sh clean         # 🧹 清理所有故障
#
# 高级用法（自定义）:
#   bash fault.sh down s1-eth3 s2-eth3     # 断指定链路
#   bash fault.sh up   s1-eth3 s2-eth3     # 恢复指定链路
#   bash fault.sh delay s1-eth3 200        # 指定接口加 200ms 时延

set -uo pipefail

VM_SCRIPT="/home/sunxiaoxuan.guest/intensure/fault_cli.sh"
BACKBONE="s1-eth3 s2-eth3"   # 骨干链路接口（演示默认目标）

# 颜色
R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[34m'; N=$'\033[0m'; BOLD=$'\033[1m'

run_vm() {
  colima ssh -- bash -c "sudo $VM_SCRIPT $*"
}

ACTION="${1:-}"

case "$ACTION" in
  # ============ 演示快捷命令 ============
  cut)
    echo "${BOLD}${R}💥 注入故障：断开骨干链路（s1 ↔ s2）${N}"
    echo "${Y}   → 观察点：dashboard 变红 / live_env 日志 loss=100% / A 同学收到 VIOLATED${N}"
    echo ""
    run_vm "down $BACKBONE"
    ;;

  restore)
    echo "${BOLD}${G}✅ 恢复：骨干链路重新接通${N}"
    echo "${Y}   → 观察点：dashboard 变绿 / live_env 日志 reachable=True${N}"
    echo ""
    run_vm "up $BACKBONE"
    ;;

  delay)
    echo "${BOLD}${Y}🐢 注入故障：加时延 100ms（双向共 200ms）${N}"
    echo "${Y}   → 观察点：live_env 日志 latency 从 ~10ms 飙升到 ~200ms${N}"
    echo ""
    run_vm "delay s1-eth3 100"
    run_vm "delay s2-eth3 100"
    ;;

  loss)
    echo "${BOLD}${Y}📉 注入故障：丢包 50%${N}"
    echo "${Y}   → 观察点：live_env 日志 loss 上升${N}"
    echo ""
    run_vm "loss s1-eth3 50"
    run_vm "loss s2-eth3 50"
    ;;

  show)
    echo "${BOLD}${B}📊 当前链路状态${N}"
    echo ""
    run_vm "show"
    ;;

  clean)
    echo "${BOLD}${B}🧹 清理所有故障${N}"
    echo ""
    run_vm "clean"
    ;;

  # ============ 高级用法（透传）============
  down|up)
    run_vm "$@"
    ;;

  *)
    cat << 'USAGE'
fault.sh - 演示用故障注入

【演示快捷命令】（推荐）
  bash fault.sh cut          💥 断开骨干链路
  bash fault.sh restore      ✅ 恢复骨干链路
  bash fault.sh delay        🐢 加时延 100ms
  bash fault.sh loss         📉 加丢包 50%
  bash fault.sh show         📊 查看状态
  bash fault.sh clean        🧹 清理全部

【高级用法】
  bash fault.sh down <intf1> <intf2>     断指定链路
  bash fault.sh up   <intf1> <intf2>     恢复指定链路
  bash fault.sh delay <intf> <ms>        指定接口加时延
  bash fault.sh loss  <intf> <pct>       指定接口加丢包

【演示推荐流程】
  1. bash fault.sh show        ← 先展示一切正常
  2. bash fault.sh cut         ← 注入故障（⚡ 高潮点）
  3. （A 同学系统响应）
  4. bash fault.sh restore     ← 恢复
  5. bash fault.sh show        ← 确认恢复

【注意】
  - 需要 colima VM 在运行
  - 故障注入后 live_env 会在 15 秒内检测到
  - 骨干链路 = s1-eth3 ↔ s2-eth3
USAGE
    exit 1
    ;;
esac
