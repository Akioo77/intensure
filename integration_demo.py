"""端到端联调 Demo：注册 → 激活 → 探测 → 上报 → 故障 → 复测。

流程：
  1. 启动 os-ken 控制器 + REST（run_all.py 子进程）
  2. 启动 Mininet 拓扑（topo_simple：2 交换机 4 主机）
  3. 手工注册测试意图（POST /register-request 到保障模块）
  4. 激活（POST /activate）
  5. 周期探测 + ActualState 组装 + POST /check
  6. 注入链路故障 → 等探测失败 → POST /check
  7. 修复链路 → 复测 → POST /verify
  8. 输出 Reporter 统计 + 清理

支持两种模式：
  --mode real  : 真实联调（连接同学 A 的保障模块）
  --mode mock  : 本地起一个 mock 保障模块（FastAPI 单文件），用于离线验证
  --mode skip  : 跳过保障模块（只演示采集模块内部）

用法：
  # 真实联调
  python3 integration_demo.py \\
      --assurance-url http://192.168.1.100:8000/api/v1/assurance \\
      --intent-id REQ-001-CI-001

  # 本地 mock（用于联调前的流程验证）
  python3 integration_demo.py --mode mock --intent-id REQ-001-CI-001

  # 仅采集演示（不连保障模块）
  python3 integration_demo.py --mode skip
"""
import argparse
import json
import os
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from actual_state_builder import ActualStateBuilder
from reporter import AssuranceReporter


# ============ 颜色 ============
class C:
    GRN = '\033[92m'
    RED = '\033[91m'
    YLW = '\033[93m'
    BLU = '\033[94m'
    GRY = '\033[90m'
    BLD = '\033[1m'
    END = '\033[0m'


def ok(m): print(f"  {C.GRN}✓{C.END} {m}", flush=True)
def fail(m): print(f"  {C.RED}✗{C.END} {m}", flush=True)
def warn(m): print(f"  {C.YLW}⚠{C.END} {m}", flush=True)
def info(m): print(f"  {C.BLU}ℹ{C.END} {m}", flush=True)
def header(t):
    print(f"\n{C.BLD}{C.BLU}{'=' * 70}\n  {t}\n{'=' * 70}{C.END}", flush=True)


# ============ Controller 启动 ============

def start_controller_local(port: int = 8080):
    """启动本地 run_all.py（含 os-ken + REST API）。"""
    info(f"启动 controller + REST API (localhost:{port})...")
    log = open('/tmp/integration_controller.log', 'w')
    proc = subprocess.Popen(
        ['python3', os.path.join(PROJECT_DIR, 'run_all.py')],
        stdout=log, stderr=subprocess.STDOUT,
        cwd=PROJECT_DIR,
        env={**os.environ, 'PYTHONPATH': PROJECT_DIR},
    )
    # 等 health
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2) as r:
                if json.loads(r.read()).get('status') == 'ok':
                    return proc
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("controller not ready in 30s")


def stop_controller(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def cleanup_mininet():
    """清理 Mininet / osken / 残留进程。
    
    ⚠️ 不用 'sudo mn -c': 它会 rm -f /tmp/*.log 把 controller log 也删了
    ⚠️ 不用 'pkill -f integration_demo': 会自杀
    """
    # 用具体进程名（不带 -f，避免匹配命令行）
    subprocess.run(['sudo', 'pkill', '-9', 'mininet'], capture_output=True)
    subprocess.run(['sudo', 'pkill', '-9', 'osken-manager'], capture_output=True)
    subprocess.run(['sudo', 'pkill', '-9', 'ofprotocol'], capture_output=True)
    subprocess.run(['sudo', 'pkill', '-9', 'ofdatapath'], capture_output=True)
    subprocess.run(['sudo', 'ovs-vsctl', '-timeout=2', '--if-exists', 'del-br', 's1'], capture_output=True)
    subprocess.run(['sudo', 'ovs-vsctl', '-timeout=2', '--if-exists', 'del-br', 's2'], capture_output=True)
    time.sleep(1)



# ============ Mininet 启动 ============

def start_mininet(controller_ip: str = '127.0.0.1', controller_port: int = 6653):
    """启动 Mininet（用 mininet 主进程方式，由调用方控制生命周期）。

    返回 Mininet net 对象。
    """
    from mininet.net import Mininet
    from mininet.node import RemoteController, OVSSwitch
    from mininet.log import setLogLevel
    from topo_simple import SimpleTopo

    setLogLevel('warning')
    net = Mininet(
        topo=SimpleTopo(),
        controller=lambda name: RemoteController(name, ip=controller_ip,
                                                  port=controller_port),
        switch=OVSSwitch,
        autoSetMacs=True, autoStaticArp=True,
    )
    net.start()
    return net


# ============ Controller State 读取 ============

def read_network_state(api_port: int = 8080) -> dict:
    """通过本地 API 读取当前 NETWORK_STATE 快照。"""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{api_port}/api/state', timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def wait_for_switches(api_port: int = 8080, n: int = 2, timeout: int = 30) -> bool:
    """等待 N 个交换机上线。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = read_network_state(api_port)
        if len(s.get('switches', {})) >= n:
            return True
        time.sleep(0.5)
    return False


def wait_for_hosts(api_port: int = 8080, n: int = 4, timeout: int = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = read_network_state(api_port)
        if len(s.get('hosts', {})) >= n:
            return True
        time.sleep(0.5)
    return False


# ============ 探测 ============

def probe_reachability(net) -> dict:
    """跑 pingall + 时延探测。

    返回 {'reachable': bool, 'latency_ms': float|None, 'packet_loss': float|None,
           'loss_pct': float (net.pingAll 返回值)}
    """
    import re
    loss_pct = net.pingAll()  # 返回丢包百分比
    reachable = (loss_pct == 0.0)

    latency_ms = None
    packet_loss = None
    try:
        out = net.get('h1').cmd('ping -c 3 -W 1 10.0.0.4')
        m = re.search(r'avg[^=]*= ([\d.]+)', out)
        if m:
            latency_ms = float(m.group(1))
        # 从 pingall 输出提取 packet_loss（小数）
        m2 = re.search(r'([\d.]+)% packet loss', out)
        if m2:
            packet_loss = float(m2.group(1)) / 100
    except Exception:
        pass

    return {
        'reachable': reachable,
        'latency_ms': latency_ms,
        'packet_loss': packet_loss,
        'loss_pct': loss_pct,
    }


def check_flow_present(net, state: dict) -> bool:
    """判断该意图对应的流表条目是否仍存在（极简版：只看是否有任何流表）。

    联调契约：expected_flow_present=True 当流表条目仍存在。
    简化策略：当前拓扑下"任何流表存在"即认为 present。
    精确版本需要根据 cookie 或 policy_refs 比对，本 demo 用简版。
    """
    flows = state.get('flows', {})
    for dpid, flow_list in flows.items():
        if flow_list:
            return True
    return False


# ============ 保障模块交互 ============

def register_test_intent(base_url: str, intent_id: str, src: str = '10.0.0.1',
                         dst: str = '10.0.0.2') -> bool:
    """手工注册一条测试意图（POST /register-request）。"""
    payload = {
        'request': {
            'schema_version': '0.1.0',
            'request_id': 'REQ-001',
            'raw_input': '测试意图：允许源到目的 IP 互通',
            'request_status': 'READY',
            'conflict_check_status': 'NOT_CHECKED',
            'requester': {'user_id': 'integration-demo', 'role': 'NORMAL_USER'},
            'recognized_actions': ['ALLOW'],
            'child_intents': [{
                'child_intent_id': intent_id,
                'intent_type': 'ACCESS_CONTROL',
                'action': 'ALLOW',
                'source': {
                    'resource_id': 'src-test',
                    'name': '测试源',
                    'cidr': f'{src}/32',
                    'vlan_id': 10,
                },
                'destination': {
                    'resource_id': 'dst-test',
                    'name': '测试目的',
                    'cidr': f'{dst}/32',
                    'vlan_id': 20,
                },
                'service': {'protocol': 'ANY', 'destination_port': None},
                'priority': None,
                'status': 'VALIDATED',
                'relation': None,
            }],
            'clarifications': [],
            'conflicts': [],
            'created_at': '2026-09-03T00:00:00+08:00',
        },
        'deployment_id': 'dep-demo-1',
        'deployment_status': 'SUCCESS',
        'devices': ['sw1', 'sw2'],
        'policy_refs': [],
    }
    try:
        req = urllib.request.Request(
            f'{base_url}/register-request',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            info(f"register-request: HTTP {resp.status}")
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        fail(f"register-request 失败: HTTP {e.code}")
        return False
    except Exception as e:
        warn(f"register-request 异常: {e}")
        return False


def activate_intent(base_url: str, intent_id: str) -> bool:
    """激活意图（POST /activate）。"""
    payload = {'intent_id': intent_id}
    try:
        req = urllib.request.Request(
            f'{base_url}/activate',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            info(f"activate: HTTP {resp.status}")
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        fail(f"activate 失败: HTTP {e.code}")
        return False
    except Exception as e:
        warn(f"activate 异常: {e}")
        return False


# ============ 主流程 ============

def main():
    parser = argparse.ArgumentParser(description='端到端联调 Demo')
    parser.add_argument('--mode', choices=['real', 'mock', 'skip'], default='real',
                        help='联调模式：real=真实联调 / mock=本地 mock / skip=跳过上报')
    parser.add_argument('--assurance-url',
                        default='http://127.0.0.1:8000/api/v1/assurance',
                        help='保障模块 API 地址')
    parser.add_argument('--intent-id', default='REQ-001-CI-001',
                        help='测试意图 ID（与上游确认稿的 child_intent_id 一致）')
    parser.add_argument('--controller-port', type=int, default=8080,
                        help='本地 controller REST 端口')
    parser.add_argument('--probe-cycles', type=int, default=2,
                        help='正常态探测周期数')
    parser.add_argument('--probe-interval', type=float, default=3.0,
                        help='探测间隔（秒）')
    args = parser.parse_args()

    print(f"\n{C.BLD}🚀 端到端联调 Demo (mode={args.mode}){C.END}")
    print(f"   保障模块: {args.assurance_url}")
    print(f"   测试意图: {args.intent_id}")
    print()

    ctrl_proc = None
    net = None
    mock_proc = None
    reporter: Optional[AssuranceReporter] = None
    builder = ActualStateBuilder()
    executor = ThreadPoolExecutor(max_workers=2)

    try:
        # ---------- Step 0: 清理 ----------
        header("Step 0: 清理残留")
        cleanup_mininet()
        ok("Mininet 已清理")

        # ---------- Step 1: 启动 controller + Mininet ----------
        header("Step 1: 启动 controller + Mininet")
        ctrl_proc = start_controller_local(args.controller_port)
        ok(f"Controller + REST 已就绪 (PID {ctrl_proc.pid})")

        # 等 controller 监听 6653（避免 Mininet 启动时 controller 未就绪）
        info("等 controller 监听 OpenFlow 端口 6653...")
        for _ in range(30):
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                if s.connect_ex(('127.0.0.1', 6653)) == 0:
                    s.close()
                    ok("Controller 6653 端口就绪")
                    break
                s.close()
            except Exception:
                pass
            time.sleep(0.5)

        net = start_mininet()
        ok("Mininet 已启动")

        # ---------- Step 2: 等待发现 ----------
        header("Step 2: 等交换机 + 主机发现")
        if wait_for_switches(args.controller_port, n=2):
            ok("2 个交换机已连接")
        else:
            fail("交换机未连接")
            return

        if wait_for_hosts(args.controller_port, n=4, timeout=15):
            ok("4 个主机已发现")
        else:
            warn("主机未全部发现")

        time.sleep(2)  # L2 学习

        # ---------- Step 3: 启动 mock（如果需要） ----------
        if args.mode == 'mock':
            info("启动本地 mock 保障模块...")
            mock_proc = subprocess.Popen(
                [sys.executable, os.path.join(PROJECT_DIR, 'mock_assurance_server.py'),
                 '--port', '8000'],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )
            time.sleep(2)
            ok("Mock 保障模块已启动（http://127.0.0.1:8000）")

        # ---------- Step 4: 注册 + 激活 ----------
        if args.mode != 'skip':
            header("Step 4: 注册测试意图 + 激活")
            if not register_test_intent(args.assurance_url, args.intent_id):
                warn("注册失败，但继续（可能已注册过）")
            else:
                ok("测试意图已注册")

            if not activate_intent(args.assurance_url, args.intent_id):
                warn("激活失败，但继续")
            else:
                ok("测试意图已激活")

        # ---------- Step 5: 准备 Reporter ----------
        if args.mode != 'skip':
            reporter = AssuranceReporter(args.assurance_url, timeout_s=5, retry=2)
            ok(f"Reporter 已就绪 → {args.assurance_url}")

        # ---------- Step 6: 周期探测 + 上报 ----------
        header(f"Step 6: 周期探测（{args.probe_cycles} 个周期）")

        def do_one_cycle(label: str):
            """执行一个探测 + 上报周期。"""
            state = read_network_state(args.controller_port)
            reach = probe_reachability(net)
            flow_present = check_flow_present(net, state)

            actual = builder.build(
                intent_id=args.intent_id,
                source_host='10.0.0.1',
                destination_host='10.0.0.4',
                state=state,
                reachability_result=reach,
                expected_flow_present=flow_present,
            )
            info(f"[{label}] reachable={reach['reachable']}, "
                 f"latency={reach['latency_ms']}, "
                 f"loss={reach['loss_pct']}%")

            if reporter:
                # 异步上报，不阻塞探测
                future = executor.submit(reporter.check, args.intent_id, actual)
                return future
            return None

        futures = []
        for i in range(args.probe_cycles):
            f = do_one_cycle(f'normal-{i+1}')
            if f:
                futures.append(f)
            time.sleep(args.probe_interval)

        # ---------- Step 7: 注入链路故障 ----------
        header("Step 7: 注入故障（s1-s2 链路 down）")
        from fault_injector import FaultInjector
        fi = FaultInjector(net)
        fi.link_down('s1', 's2')
        ok("链路已 down")
        time.sleep(2)

        info("等探测感知故障...")
        f = do_one_cycle('post-fault-1')
        if f:
            futures.append(f)
        time.sleep(args.probe_interval)
        f = do_one_cycle('post-fault-2')
        if f:
            futures.append(f)

        # ---------- Step 8: 修复 + 复测 ----------
        header("Step 8: 修复链路 + 复测上报（POST /verify）")
        fi.link_up('s1', 's2')
        ok("链路已恢复")
        time.sleep(2)

        state = read_network_state(args.controller_port)
        reach = probe_reachability(net)
        flow_present = check_flow_present(net, state)

        actual_after_heal = builder.build(
            intent_id=args.intent_id,
            source_host='10.0.0.1',
            destination_host='10.0.0.4',
            state=state,
            reachability_result=reach,
            expected_flow_present=flow_present,
        )
        info(f"[after-heal] reachable={reach['reachable']}, "
             f"latency={reach['latency_ms']}")

        if reporter:
            f = executor.submit(reporter.verify, args.intent_id, actual_after_heal)
            futures.append(f)

        # ---------- Step 8.5: 展示"故障期间"的 ActualState（含 down_links） ----------
        info("构造链路 down 期间的 ActualState 样例（给同学 A 对齐 down_links 字段）")
        # 模拟一次故障期间探测
        fault_state = {
            'switches': {
                '0000000000000001': {
                    'ports': [
                        {'port_no': 0xfffffffe, 'state': 1, 'name': 's1'},
                        {'port_no': 1, 'state': 0, 'name': 's1-eth1'},
                        {'port_no': 3, 'state': 4, 'name': 's1-eth3'},  # LIVE
                    ],
                },
                '0000000000000002': {
                    'ports': [
                        {'port_no': 0xfffffffe, 'state': 1, 'name': 's2'},
                        {'port_no': 3, 'state': 1, 'name': 's2-eth3'},  # DOWN
                    ],
                },
            },
            'links': {
                '0000000000000001:3': {
                    'peer_dpid': '0000000000000002', 'peer_port': 3,
                },
            },
        }
        fault_actual = builder.build(
            intent_id=args.intent_id,
            source_host='10.0.0.1',
            destination_host='10.0.0.4',
            state=fault_state,
            reachability_result={'reachable': False, 'latency_ms': None, 'packet_loss': None},
            expected_flow_present=True,
        )
        import json as _json
        print(_json.dumps(fault_actual, indent=2, ensure_ascii=False))

        # ---------- Step 9: 等所有异步上报完成 ----------
        header("Step 9: 等所有上报完成")
        for f in futures:
            try:
                resp = f.result(timeout=10)
                if resp and resp['status_code']:
                    ok(f"上报完成: HTTP {resp['status_code']} "
                       f"({resp['latency_ms']}ms)")
                elif resp:
                    warn(f"上报失败: {resp.get('error')}")
            except Exception as e:
                warn(f"上报异常: {e}")

        # ---------- Step 10: 统计 ----------
        header("Step 10: Reporter 统计")
        if reporter:
            stats = reporter.get_stats()
            print(json.dumps({k: v for k, v in stats.items()
                              if k not in ('recent_latency_ms', 'recent_status')},
                             indent=2, ensure_ascii=False))
            if stats['recent_latency_ms']:
                avg_lat = sum(stats['recent_latency_ms']) / len(stats['recent_latency_ms'])
                ok(f"平均响应时间: {avg_lat:.1f}ms")
            ok(f"成功率: {stats['success_calls']}/{stats['total_calls']}")

        # ---------- Step 11: 输出 ActualState 样例 ----------
        header("Step 11: ActualState 样例（给同学 A 对齐用）")
        print(json.dumps(actual_after_heal, indent=2, ensure_ascii=False))
        info("这是最近一次 ActualState，请同学 A 确认 down_links 等字段格式")

        print()
        print(f"{C.BLD}{C.GRN}🎉 端到端联调 Demo 完成!{C.END}")

    except KeyboardInterrupt:
        warn("Ctrl+C 中断")
    finally:
        executor.shutdown(wait=False)
        if net:
            try:
                net.stop()
            except Exception:
                pass
        if ctrl_proc:
            stop_controller(ctrl_proc)
        if mock_proc:
            mock_proc.terminate()
            try:
                mock_proc.wait(timeout=3)
            except Exception:
                mock_proc.kill()
        cleanup_mininet()


if __name__ == '__main__':
    main()