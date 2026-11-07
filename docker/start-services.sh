#!/usr/bin/env bash
# 容器内启动脚本：os-ken 控制器 + REST API
#
# 为什么不用 start_all.sh？
#   start_all.sh 会起 tmux + Mininet —— 容器里没有网络拓扑（见 Dockerfile 顶部说明），
#   也没有 tmux 的必要。这两个组件在容器里跑不起来也不该跑。
#
# 采集层在容器里只有两个进程：
#   ① controller_app  os-ken 控制器，监听 6653 等交换机来连
#   ② state_server    REST API，8080，读 /tmp/network_state.json
#
# ⚠️ 容器内不跑 assurance_mvp.py（本地 mock 保障模块）——
#    联调时它会跟对方真模块抢写 assurance.* 那几张表（见伙伴文档 §2.5）。
#    本地自测要用它的话，单独起一个进程即可。
set -uo pipefail

cd /app
export PYTHONPATH=/app

LOG_DIR=/var/log/intensure
mkdir -p "$LOG_DIR" /tmp

# 本地地址不走代理（VM/容器里常设 http_proxy 但没设 no_proxy，
# 连 127.0.0.1 会被代理劫持 → 健康检查假失败）
export no_proxy="${no_proxy:-127.0.0.1,localhost,::1}"
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost,::1}"

echo "[start-services] 启动 os-ken 控制器（OpenFlow 6653）"
PYTHONPATH=. osken-manager controller_app > "$LOG_DIR/controller.log" 2>&1 &
CTRL_PID=$!

echo "[start-services] 启动 REST API（8080）"
python3 state_server.py --host 0.0.0.0 --port 8080 \
    --state-file /tmp/network_state.json > "$LOG_DIR/state_server.log" 2>&1 &
API_PID=$!

shutdown() {
    echo "[start-services] 收到停止信号，收尾"
    kill -TERM "$CTRL_PID" "$API_PID" 2>/dev/null || true
    sleep 1
    kill -KILL "$CTRL_PID" "$API_PID" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# 任一进程退出就整体退出（容器编排会负责重启）
wait -n "$CTRL_PID" "$API_PID"
echo "[start-services] 有组件退出了，容器结束"
shutdown
