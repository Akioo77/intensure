"""完整 demo: Mininet + os-ken 控制器 + 状态采集 + 验证 + 故障注入 + 恢复.

这个 demo 是端到端集成, 一个脚本完成所有事:
  1. 启动 os-ken 控制器 (后台)
  2. 启动 Flask REST API (后台)
  3. 启动 Mininet (主进程)
  4. 跑 pingall 验证基础连通
  5. 注入链路故障
  6. 注入时延 + 丢包
  7. 验证状态变化
  8. 恢复全部
  9. 验证恢复

用法: sudo python3 demo_full.py

⚠ 注意: 本文件不能 import eventlet / monkey_patch!
  eventlet 会把 select 替换成不支持 poll() 的版本,
  导致 mininet.cli 导入失败 (ImportError: cannot import name 'poll').
  只有 run_all.py (子进程) 才需要 eventlet.
"""

import os
import sys
import time
import json
import threading
import subprocess

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)


# ============ 颜色 ============
class C:
    GRN = '\033[92m'
    RED = '\033[91m'
    YLW = '\033[93m'
    BLU = '\033[94m'
    GRY = '\033[90m'
    BLD = '\033[1m'
    END = '\033[0m'


def ok(m): print(f"  {C.GRN}✓{C.END} {m}")
def fail(m): print(f"  {C.RED}✗{C.END} {m}")
def warn(m): print(f"  {C.YLW}⚠{C.END} {m}")
def info(m): print(f"  {C.BLU}ℹ{C.END} {m}")
def header(t):
    print(f"\n{C.BLD}{C.BLU}{'=' * 70}\n  {t}\n{'=' * 70}{C.END}")


def start_controller_background():
    """后台启动 os-ken 控制器 + Flask.

    ⚠ 坑: stdout 不能接 PIPE 而不读!
      os-ken --verbose 一直往 stdout 打日志, 管道缓冲 (64KB)
      填满后控制器写阻塞, 整个 eventlet 进程冻结 (REST 无响应,
      packet-in 不处理, ping 全部挂起).
      正确做法: 重定向到日志文件.
    """
    info("启动 controller + REST API (后台)...")
    controller_log = open('/tmp/controller.log', 'w')
    proc = subprocess.Popen(
        ['python3', os.path.join(PROJECT_DIR, 'run_all.py')],
        stdout=controller_log,
        stderr=subprocess.STDOUT,
        cwd=PROJECT_DIR,
        env={**os.environ, 'PYTHONPATH': PROJECT_DIR},
    )
    # 等健康检查
    import urllib.request
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2) as r:
                if json.loads(r.read()).get('status') == 'ok':
                    return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("controller not ready in 30s")


def wait_for_state(c, predicate, timeout=30, msg=""):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if predicate(c.get_state()):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    warn(f"超时: {msg}")
    return False


def main():
    print(f"\n{C.BLD}🚀 SDN 状态采集 + 验证 端到端 Demo{C.END}")
    print(f"   项目目录: {PROJECT_DIR}\n")

    # 0. 清理残留
    info("清理 Mininet/OVS 残留...")
    subprocess.run(['sudo', 'mn', '-c'], capture_output=True)
    subprocess.run(['sudo', 'pkill', '-9', 'mn'], capture_output=True)
    subprocess.run(['sudo', 'pkill', '-9', 'osken'], capture_output=True)
    time.sleep(1)

    # 1. 启动 controller (后台)
    ctrl_proc = start_controller_background()
    ok("Controller + REST API 已就绪 (PID {})".format(ctrl_proc.pid))

    # 2. 启动 Mininet
    info("启动 Mininet 拓扑...")
    from mininet.net import Mininet
    from mininet.node import RemoteController, OVSSwitch
    from mininet.log import setLogLevel
    from topo_simple import SimpleTopo
    from state_collector import StateCollector
    from fault_injector import FaultInjector

    setLogLevel('warning')  # 安静点

    net = Mininet(
        topo=SimpleTopo(),
        controller=lambda name: RemoteController(name, ip='127.0.0.1', port=6653),
        switch=OVSSwitch,
        autoSetMacs=True,
        autoStaticArp=True,
    )
    net.start()
    ok("Mininet 已启动")

    # 3. 状态采集客户端
    c = StateCollector('http://127.0.0.1:8080')

    # 4. 等交换机 + 主机
    header("Step 1: 等交换机 + 主机发现")
    if not wait_for_state(c, lambda s: len(s.get('switches', {})) >= 2,
                          msg="交换机连接"):
        fail("交换机没连上")
        cleanup(net, ctrl_proc)
        return
    ok("2 个交换机已连接")

    if not wait_for_state(c, lambda s: len(s.get('hosts', {})) >= 4,
                          timeout=15, msg="主机发现"):
        warn("主机未全部发现 (可能 ARP 未完成)")

    # 让 L2 学习 / 流表安装完成
    time.sleep(2)
    state = c.get_state()
    print(f"     交换机: {len(state['switches'])}")
    print(f"     主机: {len(state['hosts'])}")

    # 5. 验证 1: 基础拓扑 + Schema
    header("Step 2: 基础验证 (Schema + Sanity + Consistency + Completeness)")
    report = c.validate()
    print_report(report)
    if not report['ok']:
        warn("基础验证有 issue, 但继续")

    # 6. Pingall + 流量采集
    header("Step 3: 跑 pingall + 采集流量")
    info("运行 pingall (12 个 ping 对, 应全通)...")
    ping_result = net.pingAll()
    if ping_result == 0.0:
        ok("pingall: 0% dropped")
    else:
        warn(f"pingall: {ping_result}% dropped")

    # 触发更多流量: h1 -> h4 (跨交换机, 走 s1-s2 链路)
    info("触发跨交换机流量 h1 -> h4 (走 s1-s2 链路)...")
    h1, h4 = net.get('h1'), net.get('h4')
    for _ in range(3):
        h1.cmd('ping -c 5 -W 1 10.0.0.4 > /dev/null')

    # 等 stats 更新 (5s 周期)
    info("等 stats 刷新 (5s)...")
    time.sleep(6)

    state = c.get_state()
    total_rx = sum(s.get('rx_bytes', 0)
                    for stats in state.get('port_stats', {}).values()
                    for s in stats)
    print(f"     全网累计 rx_bytes: {total_rx}")
    if total_rx > 0:
        ok(f"检测到流量 ({total_rx} bytes)")
    else:
        fail("没有流量! 检查控制器")

    # 7. 验证 2: 流表 + 计数器
    header("Step 4: 流表验证")
    flows = state.get('flows', {})
    total_flows = sum(len(v) for v in flows.values())
    print(f"     流表条目: {total_flows}")
    if total_flows > 0:
        ok(f"已安装 {total_flows} 条流表")
        # 列出每个 switch 的流
        for dpid, fl in flows.items():
            print(f"       {dpid}: {len(fl)} flows")
    else:
        warn("流表为空")

    # 8. 故障注入 1: 链路 down
    header("Step 5: 注入故障 1 — 关闭 s1-s2 链路")
    fi = FaultInjector(net)
    baseline = c.get_state()
    fi.link_down('s1', 's2')
    info("等控制器感知端口 down...")
    time.sleep(2)

    # 验证端口状态
    state = c.get_state()
    for dpid, sw in state.get('switches', {}).items():
        for p in sw.get('ports', []):
            if 's1-eth' in p.get('name', '') or 's2-eth' in p.get('name', ''):
                state_bits = p.get('state', 0)
                # OFPPS_LINK_DOWN = 1
                if state_bits & 1:
                    ok(f"  {dpid} {p['name']} state=down")

    # 现在 h1 <-> h2/h4 应该不通 (走 s1-s2 链路)
    info("测试跨交换机连通性 (应失败):")
    h1 = net.get('h1')
    h2 = net.get('h2')
    result = h1.cmd('ping -c 2 -W 1 10.0.0.2')  # h1 -> h2
    if '100% packet loss' in result or '0 received' in result:
        ok("h1 → h2 不通 (符合预期)")
    else:
        warn(f"意外: h1 → h2 通了?\n{result}")

    # h1 <-> h3 应通 (同交换机 s1)
    result = h1.cmd('ping -c 2 -W 1 10.0.0.3')
    if '0% packet loss' in result or '1 received' in result or '2 received' in result:
        ok("h1 → h3 通 (符合预期)")
    else:
        warn(f"意外: h1 → h3 不通?\n{result}")

    # 9. 故障注入 2: 链路恢复 + 加时延 + 丢包
    header("Step 6: 恢复链路 + 注入时延")
    fi.link_up('s1', 's2')
    info("链路已恢复, 加 100ms 时延...")
    fi.delay('s1', 's2', delay_ms=100)
    time.sleep(1)

    info("测试时延 (h1 -> h2):")
    result = h1.cmd('ping -c 3 -W 2 10.0.0.2')
    # 提取平均时延
    import re
    m = re.search(r'avg[^=]*= ([\d.]+)', result)
    if m:
        rtt = float(m.group(1))
        if rtt > 100:
            ok(f"时延生效: avg {rtt}ms")
        else:
            warn(f"时延不明显: {rtt}ms")

    # 10. 清除所有故障
    header("Step 7: 清除所有故障")
    fi.clear_all()
    ok("已清除全部故障")

    time.sleep(2)

    # 11. 最终验证
    header("Step 8: 最终验证")
    state = c.get_state()
    report_after = c.validate(state, baseline=baseline)
    print_report(report_after)

    # 12. 清理
    cleanup(net, ctrl_proc)

    print()
    print(f"{C.BLD}{C.GRN}🎉 Demo 完成!{C.END}")
    print(f"   报告已保存在对话输出中")


def cleanup(net, ctrl_proc):
    info("清理...")
    try:
        net.stop()
    except Exception:
        pass
    try:
        ctrl_proc.terminate()
        ctrl_proc.wait(timeout=5)
    except Exception:
        ctrl_proc.kill()
    subprocess.run(['sudo', 'mn', '-c'], capture_output=True)


def print_report(report):
    print(f"     {C.GRY}issues: {report['issue_count']}, "
          f"errors: {report['by_level']['error']}, "
          f"warnings: {report['by_level']['warning']}, "
          f"info: {report['by_level']['info']}{C.END}")
    for issue in report['issues'][:8]:
        lvl = issue['level']
        col = {'error': C.RED, 'warning': C.YLW, 'info': C.BLU}[lvl]
        print(f"     {col}[{lvl}]{C.END} {issue['category']}: {issue['message']}")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n\n⚠ Ctrl+C')
        sys.exit(1)
