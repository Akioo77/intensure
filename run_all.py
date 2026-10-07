"""一键启动: os-ken 控制器 + Flask REST API.

架构: 单进程, eventlet green threads.
  - 主线程: os-ken manager (SDN 控制器)
  - Green thread: Flask REST API (读 NETWORK_STATE)

也支持分离进程模式: STATE_FILE 用于跨进程共享.
"""
import eventlet
eventlet.monkey_patch()  # 必须在所有 socket 之前

import os
import sys
import time
import threading

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

# 状态文件: 用于与外部 demo.py / fault_injector 等通信
STATE_FILE = '/tmp/network_state.json'


def start_http_server(host='0.0.0.0', port=8080):
    """在 green thread 中跑 Flask."""
    from state_server import app
    # 让 Flask 在 eventlet hub 中跑 (它会 monkey patch 后用 green socket)
    try:
        app.run(host=host, port=port, debug=False, use_reloader=False,
                threaded=True)
    except Exception as e:
        print(f"❌ HTTP server crashed: {e}", file=sys.stderr)


def start_controller():
    """主线程: 跑 os-ken manager."""
    from os_ken.cmd.manager import main as osken_main
    sys.argv = ['osken-manager', '--verbose', 'controller_app']
    osken_main()


def main():
    print("=" * 60)
    print("🚀 SDN 状态采集控制器 + REST API")
    print("=" * 60)

    # 1. 清旧状态
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    # 2. 起 HTTP server (green thread)
    print(f"\n🌐 启动 REST API (port 8080)...")
    eventlet.spawn(start_http_server, '0.0.0.0', 8080)
    eventlet.sleep(2)  # 等 Flask 起来

    # 3. 健康检查
    import urllib.request, json
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3) as r:
            print(f"   健康检查: {json.loads(r.read())}")
    except Exception as e:
        print(f"   ⚠ 健康检查失败: {e}")

    # 3.5 启动 OVS 详细日志采集（独立模块，不影响主流程）
    #     - 启动失败也不阻塞控制器
    #     - 用于归因链路状态变化（"是谁让端口 up/down 的"）
    try:
        from ovs_logger import start_global
        if start_global():
            print(f"   OVS 详细日志: ✅ 已启动 (查询: GET /api/ovs/log)")
        else:
            print(f"   OVS 详细日志: ⚠ 启动失败（不影响主流程）")
    except ImportError:
        print(f"   OVS 详细日志: ⚠ ovs_logger 模块未找到（跳过）")
    except Exception as e:
        print(f"   OVS 详细日志: ⚠ 启动异常: {e}（不影响主流程）")

    # 4. 跑控制器 (主线程, 阻塞)
    print(f"\n🎮 启动 os-ken 控制器 (controller_app)...")
    print(f"   OpenFlow: 6633 / 6653")
    print(f"   REST API: http://localhost:8080/api/...")
    print()
    try:
        start_controller()
    except KeyboardInterrupt:
        print("\n\n⚠ 收到 Ctrl+C, 退出...")
    except SystemExit:
        pass
    finally:
        # 关闭时优雅停止 OVSLogger（独立于控制器启停，不影响主流程）
        try:
            from ovs_logger import stop_global
            stop_global()
        except Exception as e:
            print(f"⚠ OVSLogger 关闭异常: {e}", file=sys.stderr)


if __name__ == '__main__':
    main()
