"""常驻实验环境（v2.0 联调版）：启动 Mininet 拓扑 + 周期探测 + 上报 ActualState。

每次探测周期：
  1. 跑 pingall（12 对）测可达性 + 时延
  2. 读 controller 内部 state（via /api/state）
  3. 组装 ActualState（用 ActualStateBuilder）
  4. POST /check 给保障模块（用 AssuranceReporter）

参数（环境变量优先）：
  ASSURANCE_URL  保障模块 base URL（如 http://192.168.1.100:8000/api/v1/assurance）
  INTENT_ID      上游透传的意图 ID（如 REQ-001-CI-001）
  PROBE_INTERVAL 探测周期秒数（默认 15）
  SRC_HOST / DST_HOST 源 / 目的 IP（可选）
  REPORT_MODE    'enable'（默认）/ 'disable'（只采集不上报）
"""
import json
import os
import re
import sys
import time
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel
from topo_simple import SimpleTopo

from actual_state_builder import ActualStateBuilder
from reporter import AssuranceReporter


# ============ 环境变量 / 配置 ============
ASSURANCE_URL = os.environ.get(
    'ASSURANCE_URL',
    'http://127.0.0.1:8000/api/v1/assurance')
INTENT_ID = os.environ.get('INTENT_ID', 'REQ-001-CI-001')
PROBE_INTERVAL = float(os.environ.get('PROBE_INTERVAL', '15'))
SRC_HOST = os.environ.get('SRC_HOST', '10.0.0.1')
DST_HOST = os.environ.get('DST_HOST', '10.0.0.4')
REPORT_MODE = os.environ.get('REPORT_MODE', 'enable')
LOCAL_API = 'http://127.0.0.1:8080'


# ============ 探测 ============

def safe_ping(host, dst_ip, count=3, timeout_s=6):
    """带超时保护的 ping（绕开 Mininet pingAll 的 pty 卡死问题）。

    当链路断开时，Mininet 的 net.pingAll() / node.cmd() 会卡在 pty 等待上
    （ping 子进程不退出 → 主循环死锁）。这里改用 subprocess + mnexec，
    强制 timeout 保护，保证探测循环永不死锁。

    参数:
        host: Mininet host 对象（用 host.pid 拿到命名空间 PID）
        dst_ip: 目标 IP
        count: ping 次数
        timeout_s: subprocess 硬超时（秒）

    返回:
        (loss_pct: float, latency_ms: float | None)
    """
    import subprocess
    try:
        r = subprocess.run(
            ['mnexec', '-a', str(host.pid), 'ping', '-c', str(count), '-W', '1', dst_ip],
            capture_output=True, text=True, timeout=timeout_s
        )
        out = r.stdout or ''
    except subprocess.TimeoutExpired:
        return (100.0, None)
    except Exception:
        return (100.0, None)

    m = re.search(r'(\d+(?:\.\d+)?)% packet loss', out)
    loss = float(m.group(1)) if m else 100.0
    m2 = re.search(r'avg[^=]*= ([\d.]+)', out)
    latency = float(m2.group(1)) if m2 else None
    return (loss, latency)


def measure_latency(net):
    """h1 → h4 平均 RTT（跨 s1-s2 骨干链路）。"""
    try:
        out = net.get('h1').cmd('ping -c 3 -W 1 10.0.0.4')
        m = re.search(r'avg[^=]*= ([\d.]+)', out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def read_state_from_controller():
    """从本地 controller REST 读 NETWORK_STATE。"""
    try:
        with urllib.request.urlopen(f'{LOCAL_API}/api/state', timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def check_flow_present(state):
    """极简版：当前有任何流表条目即视为 present。

    精确版本需要按 cookie 比对，本 live_env 用简版。
    """
    flows = state.get('flows', {})
    return any(flow_list for flow_list in flows.values())


# ============ 主流程 ============

def main():
    setLogLevel('warning')
    info_lines = []
    info_lines.append(f'LIVE_ENV_STARTED (v2.0 联调版)')
    info_lines.append(f'  assurance_url: {ASSURANCE_URL}')
    info_lines.append(f'  intent_id:     {INTENT_ID}')
    info_lines.append(f'  interval:      {PROBE_INTERVAL}s')
    info_lines.append(f'  report_mode:   {REPORT_MODE}')

    # Reporter
    reporter = (AssuranceReporter(ASSURANCE_URL, timeout_s=5, retry=2)
                if REPORT_MODE == 'enable' else None)
    builder = ActualStateBuilder()

    # Mininet
    net = Mininet(
        topo=SimpleTopo(),
        controller=lambda n: RemoteController(n, ip='127.0.0.1', port=6653),
        switch=OVSSwitch, autoSetMacs=True, autoStaticArp=True)
    net.start()
    info_lines.append('MININET_STARTED')

    # 基础时延
    try:
        net.get('s1').cmd('tc qdisc replace dev s1-eth3 root netem delay 5ms')
        net.get('s2').cmd('tc qdisc replace dev s2-eth3 root netem delay 5ms')
        info_lines.append('BASELINE_DELAY_APPLIED (5ms on s1-s2)')
    except Exception as e:
        info_lines.append(f'BASELINE_DELAY_FAILED: {e}')

    print('\n'.join(info_lines), flush=True)

    # 初次探测（让 L2 学习 + 流表安装）；带超时保护，避免卡死
    time.sleep(3)
    loss, _ = safe_ping(net.get('h1'), '10.0.0.4', count=3, timeout_s=8)
    print(f'INITIAL_PINGALL: {loss}% loss', flush=True)

    cycle = 0
    while True:
        cycle += 1
        try:
            t0 = time.time()

            # 探测（只测 h1→h4，带 subprocess 超时保护，故障时不会卡死）
            loss_pct, latency_ms = safe_ping(net.get('h1'), '10.0.0.4', count=3, timeout_s=6)
            reachable = (loss_pct == 0.0)

            # 读 state
            state = read_state_from_controller()
            flow_present = check_flow_present(state)

            # 组装 ActualState
            actual = builder.build(
                intent_id=INTENT_ID,
                source_host=SRC_HOST,
                destination_host=DST_HOST,
                state=state,
                reachability_result={
                    'reachable': reachable,
                    'latency_ms': latency_ms,
                    'packet_loss': loss_pct / 100.0,
                },
                expected_flow_present=flow_present,
            )

            # **选择性写 DB**：探针异常才写 probe_results
            try:
                import db_writer
                db_writer.write_probe_with_decision(
                    src_host=SRC_HOST, dst_host=DST_HOST,
                    reachable=reachable, loss_pct=loss_pct / 100.0,
                    latency_ms=latency_ms,
                )
            except Exception as e:
                print(f'  WARN: db probe write failed: {e}', flush=True)

            # 上报 connectivity 历史到本地 controller（供 dashboard 「自愈前后对比」面板使用）
            try:
                probe_payload = json.dumps({
                    'reachable': ['all'] if reachable else [],
                    'unreachable': [] if reachable else ['10.0.0.4'],
                    'loss_pct': loss_pct,
                    'latency_ms': latency_ms,
                }).encode('utf-8')
                req = urllib.request.Request(
                    f'{LOCAL_API}/api/probe',
                    data=probe_payload,
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                urllib.request.urlopen(req, timeout=2).read()
            except Exception:
                pass  # connectivity 上报失败不影响主流程

            # 上报
            if reporter:
                resp = reporter.check(INTENT_ID, actual)
                status = resp['status_code']
                err = resp['error']
                body_status = (resp['body'] or {}).get('status', '?')
                print(f'CYCLE_{cycle:04d} reachable={reachable} '
                      f'latency={latency_ms}ms loss={loss_pct}% '
                      f'flow={flow_present} '
                      f'POST→HTTP {status} body.status={body_status} '
                      f'took={int((time.time()-t0)*1000)}ms',
                      flush=True)
                if err:
                    print(f'  WARN: {err}', flush=True)
            else:
                # disable 模式：只打印 ActualState
                print(f'CYCLE_{cycle:04d} reachable={reachable} '
                      f'latency={latency_ms}ms loss={loss_pct}% '
                      f'flow={flow_present} (REPORT_MODE=disable, no POST)',
                      flush=True)

            # 触发跨交换机流量
            net.get('h1').cmd('ping -c 2 -W 1 10.0.0.4 > /dev/null 2>&1')
            net.get('h2').cmd('ping -c 2 -W 1 10.0.0.3 > /dev/null 2>&1')
        except Exception as e:
            print(f'CYCLE_{cycle:04d} ERROR: {e}', flush=True)

        time.sleep(PROBE_INTERVAL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nCtrl+C, exiting...', flush=True)
        sys.exit(0)