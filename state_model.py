"""网络状态模型 + 验证引擎。

这是 Part A 的核心: 不仅"采到数据", 还要"验证数据正确性"。

验证维度:
  1. Schema 验证    — 必要字段都在？类型对不对？
  2. Sanity 验证    — 数值合理？(rx_bytes >= 0, 端口号合法...)
  3. Consistency 验证 — 关联字段一致？(ports 列表长度 == num_ports)
  4. Completeness   — 期望的资源都在？(2 交换机, 4 主机, 5 链路)
  5. Connectivity   — 实际连通性 (用 pingall)
  6. Anomaly 检测   — 偏离基线？(端口意外 down, 流量骤降)

每条 issue 结构: {level, category, message, context}
  level: 'error' | 'warning' | 'info'
"""
import time
from typing import Dict, List, Any


# ============ Schema 定义 ============
# 期望的拓扑（来自 topo_simple.py）
EXPECTED_TOPOLOGY = {
    'switches': 2,                # s1, s2
    'hosts': 4,                   # h1, h2, h3, h4
    'inter_switch_links': 1,      # s1 <-> s2
    'host_switch_links': 4,       # h1/h3<->s1, h2/h4<->s2
    'total_links': 5,
    'expected_host_ips': ['10.0.0.1', '10.0.0.2', '10.0.0.3', '10.0.0.4'],
}

# OpenFlow 端口号常量（参考 OpenFlow 1.3 spec）
OFPP_IN_PORT = 0xfffffff8
OFPP_TABLE = 0xfffffff9
OFPP_NORMAL = 0xfffffffa
OFPP_FLOOD = 0xfffffffb
OFPP_ALL = 0xfffffffc
OFPP_CONTROLLER = 0xfffffffd
OFPP_LOCAL = 0xfffffffe
OFPP_ANY = 0xffffffff

VALID_SPECIAL_PORTS = {OFPP_IN_PORT, OFPP_TABLE, OFPP_NORMAL,
                       OFPP_FLOOD, OFPP_ALL, OFPP_CONTROLLER,
                       OFPP_LOCAL, OFPP_ANY}


# ============ 验证函数 ============

def _issue(level: str, category: str, message: str, **context) -> dict:
    return {
        'level': level,
        'category': category,
        'message': message,
        'time': time.time(),
        **context,
    }


def validate_schema(state: dict) -> List[dict]:
    """1. Schema: 必要字段存在 + 类型正确."""
    issues = []
    if not isinstance(state, dict):
        issues.append(_issue('error', 'schema', 'state is not a dict'))
        return issues

    # 顶层字段
    required = {'meta', 'switches', 'hosts', 'port_stats', 'flows', 'events',
                'links'}
    missing = required - set(state.keys())
    if missing:
        issues.append(_issue('error', 'schema',
                             f'missing top-level keys: {sorted(missing)}',
                             missing=sorted(missing)))

    # switches 应为 dict
    if not isinstance(state.get('switches'), dict):
        issues.append(_issue('error', 'schema', 'switches should be dict'))
    if not isinstance(state.get('hosts'), dict):
        issues.append(_issue('error', 'schema', 'hosts should be dict'))
    if not isinstance(state.get('port_stats'), dict):
        issues.append(_issue('error', 'schema', 'port_stats should be dict'))
    if not isinstance(state.get('flows'), dict):
        issues.append(_issue('error', 'schema', 'flows should be dict'))

    return issues


def validate_sanity(state: dict) -> List[dict]:
    """2. Sanity: 数值合理性."""
    issues = []

    # 端口 stats: rx/tx bytes 不能负
    for dpid, stats in state.get('port_stats', {}).items():
        for s in stats:
            if not isinstance(s, dict):
                issues.append(_issue('error', 'sanity',
                                     f'port_stat not dict',
                                     dpid=dpid))
                continue
            for key in ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets',
                        'rx_errors', 'tx_errors', 'rx_dropped', 'tx_dropped'):
                v = s.get(key, 0)
                if not isinstance(v, int):
                    issues.append(_issue('error', 'sanity',
                                         f'port_stat.{key} not int: {v!r}',
                                         dpid=dpid, port_no=s.get('port_no'),
                                         value=v))
                elif v < 0:
                    issues.append(_issue('error', 'sanity',
                                         f'port_stat.{key} is negative: {v}',
                                         dpid=dpid, port_no=s.get('port_no')))

            # 端口号合法性
            port_no = s.get('port_no')
            if port_no is None:
                issues.append(_issue('error', 'sanity',
                                     'port_no missing',
                                     dpid=dpid))
            elif port_no < 0 and port_no not in VALID_SPECIAL_PORTS:
                issues.append(_issue('warning', 'sanity',
                                     f'unusual port_no: {port_no}',
                                     dpid=dpid, port_no=port_no))

    # 交换机 connected_at 是数字
    for dpid, sw in state.get('switches', {}).items():
        ca = sw.get('connected_at')
        if ca is None or not isinstance(ca, (int, float)):
            issues.append(_issue('error', 'sanity',
                                 f'switch connected_at invalid',
                                 dpid=dpid))

    return issues


def validate_consistency(state: dict) -> List[dict]:
    """3. Consistency: 关联字段一致."""
    issues = []

    # num_ports == len(ports)
    for dpid, sw in state.get('switches', {}).items():
        ports = sw.get('ports', [])
        num = sw.get('num_ports', 0)
        if len(ports) != num:
            issues.append(_issue('warning', 'consistency',
                                 f'num_ports={num} != len(ports)={len(ports)}',
                                 dpid=dpid))

    # port_stats 里出现的 dpid 应该在 switches 里
    for dpid in state.get('port_stats', {}):
        if dpid not in state.get('switches', {}):
            issues.append(_issue('warning', 'consistency',
                                 f'port_stats has dpid not in switches: {dpid}',
                                 dpid=dpid))

    # flows 同上
    for dpid in state.get('flows', {}):
        if dpid not in state.get('switches', {}):
            issues.append(_issue('warning', 'consistency',
                                 f'flows has dpid not in switches: {dpid}',
                                 dpid=dpid))

    # hosts 引用的 dpid/port 应该在交换机/端口里
    sw_ports = {}
    for dpid, sw in state.get('switches', {}).items():
        sw_ports[dpid] = {p.get('port_no') for p in sw.get('ports', [])}
    for mac, host in state.get('hosts', {}).items():
        dpid = host.get('dpid')
        port = host.get('port')
        if dpid not in state.get('switches', {}):
            issues.append(_issue('warning', 'consistency',
                                 f'host on unknown switch: {dpid}',
                                 mac=mac))
        elif port not in sw_ports.get(dpid, set()):
            # 允许: 端口还没被发现（stats 还在等）
            issues.append(_issue('info', 'consistency',
                                 f'host on port {port} not in discovered ports yet',
                                 mac=mac, dpid=dpid, port=port))

    return issues


def validate_completeness(state: dict) -> List[dict]:
    """4. Completeness: 期望资源全到位."""
    issues = []
    n_sw = len(state.get('switches', {}))
    n_host = len(state.get('hosts', {}))

    if n_sw < EXPECTED_TOPOLOGY['switches']:
        issues.append(_issue('warning', 'completeness',
                             f'expected {EXPECTED_TOPOLOGY["switches"]} switches, got {n_sw}',
                             expected=EXPECTED_TOPOLOGY['switches'], actual=n_sw))
    elif n_sw > EXPECTED_TOPOLOGY['switches']:
        issues.append(_issue('info', 'completeness',
                             f'more switches than expected: {n_sw}',
                             actual=n_sw))

    if n_host < EXPECTED_TOPOLOGY['hosts']:
        issues.append(_issue('info', 'completeness',
                             f'expected {EXPECTED_TOPOLOGY["hosts"]} hosts, got {n_host}',
                             expected=EXPECTED_TOPOLOGY['hosts'], actual=n_host))

    # 找已知 IP
    known_ips = set()
    for h in state.get('hosts', {}).values():
        for ip in h.get('ipv4', []):
            known_ips.add(ip)
    expected_ips = set(EXPECTED_TOPOLOGY['expected_host_ips'])
    missing_ips = expected_ips - known_ips
    if missing_ips:
        issues.append(_issue('info', 'completeness',
                             f'hosts with these IPs not discovered yet: {sorted(missing_ips)}',
                             missing_ips=sorted(missing_ips)))

    # 链路发现完整性 (LLDP): 期望交换机间链路条数
    links = state.get('links', {})
    # 去重成对
    pairs = set()
    for key, v in links.items():
        try:
            dpid, port = key.split(':')
            pairs.add(frozenset([(dpid, int(port)), (v.get('peer_dpid'), v.get('peer_port'))]))
        except Exception:
            continue
    n_links = len(pairs)
    if n_links < EXPECTED_TOPOLOGY['inter_switch_links']:
        issues.append(_issue('info', 'completeness',
                             f'expected {EXPECTED_TOPOLOGY["inter_switch_links"]} inter-switch link(s), got {n_links}',
                             expected=EXPECTED_TOPOLOGY['inter_switch_links'],
                             actual=n_links))

    return issues


def validate_anomaly(state: dict, baseline: dict = None) -> List[dict]:
    """5. Anomaly: 与基线对比.

    baseline: 之前的状态快照 (dict). 如果 None, 跳过 delta 检查.
    """
    issues = []

    # 检查端口状态: 任何端口 "down" 应该被记录
    # 注意: OVS local 管理端口 (OFPP_LOCAL) 没有线缆, 永远 LINK_DOWN, 跳过
    for dpid, sw in state.get('switches', {}).items():
        for p in sw.get('ports', []):
            if p.get('port_no') == OFPP_LOCAL:
                continue
            state_bits = p.get('state', 0)
            # OFPPS_LINK_DOWN = 1 << 0
            if state_bits & 1:
                issues.append(_issue('warning', 'anomaly',
                                     f'port link down',
                                     dpid=dpid,
                                     port_no=p.get('port_no'),
                                     port_name=p.get('name')))

    # baseline 对比
    if baseline:
        # 端口计数器应单调递增（除重启外）
        for dpid, stats in state.get('port_stats', {}).items():
            base_stats = baseline.get('port_stats', {}).get(dpid, [])
            base_by_port = {s['port_no']: s for s in base_stats}
            for s in stats:
                port_no = s['port_no']
                if port_no in base_by_port:
                    for key in ('rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets'):
                        if s.get(key, 0) < base_by_port[port_no].get(key, 0):
                            issues.append(_issue('warning', 'anomaly',
                                                 f'counter went down (counter reset?)',
                                                 dpid=dpid, port_no=port_no,
                                                 metric=key))
                            break

        # 主机消失
        prev_macs = set(baseline.get('hosts', {}).keys())
        curr_macs = set(state.get('hosts', {}).keys())
        disappeared = prev_macs - curr_macs
        if disappeared:
            issues.append(_issue('warning', 'anomaly',
                                 f'{len(disappeared)} hosts disappeared',
                                 count=len(disappeared),
                                 macs=sorted(disappeared)[:5]))

        # 交换机断开
        prev_dpids = set(baseline.get('switches', {}).keys())
        curr_dpids = set(state.get('switches', {}).keys())
        dead = prev_dpids - curr_dpids
        if dead:
            issues.append(_issue('error', 'anomaly',
                                 f'{len(dead)} switches disconnected',
                                 dpids=sorted(dead)))

    # meta.last_update 不应过旧 (数据陈旧)
    last_update = state.get('meta', {}).get('last_update', 0)
    age = time.time() - last_update if last_update else float('inf')
    if age > 30:
        issues.append(_issue('warning', 'anomaly',
                             f'state is stale ({age:.1f}s old)',
                             age_sec=round(age, 1)))

    return issues


def validate_state(state: dict, baseline: dict = None,
                   connectivity: dict = None) -> dict:
    """主入口: 跑全部验证, 返回报告."""
    issues = []
    issues += validate_schema(state)
    issues += validate_sanity(state)
    issues += validate_consistency(state)
    issues += validate_completeness(state)
    issues += validate_anomaly(state, baseline)

    # connectivity 由 demo 注入
    if connectivity is not None:
        reachable = connectivity.get('reachable', [])
        unreachable = connectivity.get('unreachable', [])
        if unreachable:
            issues.append(_issue('error', 'connectivity',
                                 f'{len(unreachable)} host pairs unreachable',
                                 pairs=unreachable[:10]))

    # 汇总
    by_level = {'error': 0, 'warning': 0, 'info': 0}
    for i in issues:
        by_level[i['level']] = by_level.get(i['level'], 0) + 1

    return {
        'ok': by_level['error'] == 0,
        'issue_count': len(issues),
        'by_level': by_level,
        'issues': issues,
        'validated_at': time.time(),
    }


# ============ CLI ============
if __name__ == '__main__':
    """直接运行: python3 state_model.py [state.json]"""
    import sys
    import json

    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            state = json.load(f)
    else:
        # 默认读文件
        try:
            with open('/tmp/network_state.json') as f:
                state = json.load(f)
        except FileNotFoundError:
            print("✗ /tmp/network_state.json not found, run controller first")
            sys.exit(1)

    report = validate_state(state)
    print(json.dumps(report, indent=2, default=str))

    sys.exit(0 if report['ok'] else 1)
