#!/bin/bash
# fault_cli.sh - 不依赖 Mininet net 对象的故障注入 CLI
# 用法:
#   sudo ./fault_cli.sh down s1-eth3 s2-eth3    # 断开骨干链路（两端）
#   sudo ./fault_cli.sh up   s1-eth3 s2-eth3    # 恢复
#   sudo ./fault_cli.sh delay s1-eth3 100       # 加100ms时延
#   sudo ./fault_cli.sh show                    # 显示当前接口状态
#   sudo ./fault_cli.sh clean                   # 清理所有 TC + 恢复

ACTION="${1:-}"
shift 2>/dev/null

case "$ACTION" in
  down)
    for intf in "$@"; do
      sw="${intf%%-eth*}"
      ip link set "$intf" down 2>/dev/null && echo "  ✓ ip link $intf down"
      ovs-ofctl -O OpenFlow13 mod-port "$sw" "$intf" down 2>/dev/null && echo "  ✓ ovs mod-port $sw/$intf down"
    done
    echo "💥 故障已注入：${*}"
    ;;
  up)
    for intf in "$@"; do
      sw="${intf%%-eth*}"
      ip link set "$intf" up 2>/dev/null && echo "  ✓ ip link $intf up"
      ovs-ofctl -O OpenFlow13 mod-port "$sw" "$intf" up 2>/dev/null && echo "  ✓ ovs mod-port $sw/$intf up"
    done
    echo "✅ 已恢复：${*}"
    ;;
  delay)
    intf="$1"; ms="$2"
    tc qdisc replace dev "$intf" root netem delay "${ms}ms" 2>/dev/null && echo "  ✓ $intf 加时延 ${ms}ms"
    ;;
  loss)
    intf="$1"; pct="$2"
    tc qdisc replace dev "$intf" root netem loss "${pct}%" 2>/dev/null && echo "  ✓ $intf 加丢包 ${pct}%"
    ;;
  show)
    echo "=== 接口状态 ==="
    ip -br link show | grep -E 's[0-9]+-eth' | awk '{print "  " $1 " " $2}'
    echo ""
    echo "=== OVS 端口状态 ==="
    for sw in s1 s2; do
      echo "  [$sw]"
      ovs-ofctl -O OpenFlow13 show "$sw" 2>/dev/null | grep -E "port|state" | head -6 | sed 's/^/    /'
    done
    ;;
  clean)
    for intf in $(ip -br link show | grep -oE 's[0-9]+-eth[0-9]+'); do
      sw="${intf%%-eth*}"
      ip link set "$intf" up 2>/dev/null
      ovs-ofctl -O OpenFlow13 mod-port "$sw" "$intf" up 2>/dev/null
      tc qdisc del dev "$intf" root 2>/dev/null
    done
    echo "✅ 已清理所有故障"
    ;;
  *)
    echo "用法: $0 <down|up|delay|loss|show|clean> [args...]"
    echo ""
    echo "示例:"
    echo "  $0 down s1-eth3 s2-eth3    # 断骨干链路"
    echo "  $0 up   s1-eth3 s2-eth3    # 恢复"
    echo "  $0 delay s1-eth3 100       # 100ms 时延"
    echo "  $0 show                    # 查看状态"
    echo "  $0 clean                   # 清理"
    exit 1
    ;;
esac
