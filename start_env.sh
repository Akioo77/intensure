#!/bin/bash
# 用法: ./start_env.sh
# 启动 Mininet + os-ken SDN 环境（前置条件：colima 已起）

set -e
PROJECT_DIR="/home/sunxiaoxuan.guest/intensure"

cd "$PROJECT_DIR"

# 1. 清理
echo "🧹 清理残留..."
sudo pkill -9 mn 2>/dev/null || true
sudo pkill -9 osken 2>/dev/null || true
sudo mn -c 2>/dev/null || true
sleep 1

# 2. 起控制器
echo "🚀 启动 SDN 控制器 (os-ken)..."
PYTHONPATH="$PROJECT_DIR" osken-manager simple_switch_13 > /tmp/osken.log 2>&1 &
OSKEN_PID=$!
sleep 4
echo "   osken PID: $OSKEN_PID (监听 6633/6653)"

# 3. 起 Mininet（4 主机 1 交换机 demo）
echo "🌐 启动 Mininet 拓扑 (single,4)..."
echo ""
sudo mn --topo single,4 \
       --controller remote,ip=127.0.0.1,port=6653 \
       --switch ovsk,protocols=OpenFlow13

# 4. 清理
echo ""
echo "🧹 Mininet 已退出，关闭控制器..."
kill $OSKEN_PID 2>/dev/null || true
sudo mn -c 2>/dev/null || true