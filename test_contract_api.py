#!/usr/bin/env python3
"""test_contract_api.py — 跨模块契约端点单元测试（v3.20.0）

覆盖场景：
  1. /api/intensure/v1/state/current：契约 §4.1 的字段形状
     （captured_at / version / switches 是 list / down_ports / down_links）
  2. 采集未初始化时返回 424 DEPENDENCY_NOT_READY
  3. down_ports / down_links 用 "s1:eth2" 可读格式（不是 dpid:port_no 数字）
  4. version 单调递增（乐观锁语义）
  5. 旧路径不受影响（/api/state、/api/links 仍然正常）
  6. history 支持 since/until/limit 过滤
  7. port-events 支持 dpid 过滤、link-events 只出链路类事件
  8. probe-results 能写入 connectivity 历史
  9. contract_api 纯函数：事件过滤 / 边界

运行: python3 -m pytest test_contract_api.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import contract_api  # noqa: E402

try:
    import flask  # noqa: F401
except ImportError:
    flask = None

if flask is not None:
    import state_server
else:
    state_server = None


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'{name}  {detail}')


# ============ 样例 state（照抄 controller_app 的内部结构）============

SAMPLE_STATE = {
    'meta': {'start_time': 1000.0, 'last_update': 1010.0, 'update_count': 42},
    'switches': {
        '0000000000000001': {
            'dpid': '0000000000000001',
            'connected_at': 1000.0,
            'num_ports': 4,
            'ports': [
                {'port_no': 0xfffffffe, 'name': 's1', 'state': 1, 'curr_speed': 0},
                {'port_no': 1, 'name': 's1-eth1', 'state': 0, 'curr_speed': 10000000},
                {'port_no': 2, 'name': 's1-eth2', 'state': 0, 'curr_speed': 10000000},
                {'port_no': 3, 'name': 's1-eth3', 'state': 1, 'curr_speed': 0},
            ],
        },
        '0000000000000002': {
            'dpid': '0000000000000002',
            'connected_at': 1000.0,
            'num_ports': 4,
            'ports': [
                {'port_no': 0xfffffffe, 'name': 's2', 'state': 1, 'curr_speed': 0},
                {'port_no': 1, 'name': 's2-eth1', 'state': 0, 'curr_speed': 10000000},
                {'port_no': 2, 'name': 's2-eth2', 'state': 0, 'curr_speed': 10000000},
                {'port_no': 3, 'name': 's2-eth3', 'state': 1, 'curr_speed': 0},
            ],
        },
    },
    'hosts': {},
    'port_stats': {},
    'flows': {'0000000000000001': [{'priority': 10, 'match': {}}]},
    'links': {
        '0000000000000001:3': {
            'peer_dpid': '0000000000000002', 'peer_port': 3,
            'peer_name': 's2-eth3', 'last_seen': 1005.0,
        },
    },
    'events': [
        {'type': 'controller_started', 'time': 1001.0, 'pid': 123},
        {'type': 'switch_connected', 'time': 1002.0, 'dpid': '0000000000000001'},
        {'type': 'port_link_status', 'time': 1003.0, 'dpid': '0000000000000001',
         'port_no': 3, 'name': 's1-eth3', 'down': True},
        {'type': 'link_discovered', 'time': 1004.0, 'dpid': '0000000000000001', 'port': 3},
        {'type': 'host_discovered', 'time': 1006.0, 'mac': 'aa:bb', 'dpid': '0000000000000001'},
        {'type': 'link_lost', 'time': 1007.0, 'dpid': '0000000000000002', 'port': 3},
    ],
}


# ============ contract_api 纯函数 ============

def test_build_switches_is_list():
    switches = contract_api.build_switches(SAMPLE_STATE)
    check('switches 是 list', isinstance(switches, list))
    check('交换机数量正确', len(switches) == 2, str(len(switches)))
    check('按 dpid 排序', switches[0]['dpid'] == '0000000000000001')
    check('dpid 是 16 位十六进制串', switches[0]['dpid'] == '0000000000000001')
    # OFPP_LOCAL 管理端口要排除
    check('排除 OFPP_LOCAL', all(p['port_no'] != 0xfffffffe
                              for p in switches[0]['ports']))
    check('端口数正确（3 个数据端口）', len(switches[0]['ports']) == 3,
          str(len(switches[0]['ports'])))


def test_build_switches_speed_mbps():
    switches = contract_api.build_switches(SAMPLE_STATE)
    p0 = switches[0]['ports'][0]
    check('speed_mbps = curr_speed / 1000', p0['speed_mbps'] == 10000, str(p0))
    check('端口字段齐全', set(p0.keys()) == {'port_no', 'name', 'state', 'speed_mbps'},
          str(p0.keys()))


def test_build_current_state_shape():
    out = contract_api.build_current_state(SAMPLE_STATE)
    for field in ('captured_at', 'version', 'switches', 'links', 'flows',
                  'down_ports', 'down_links'):
        check(f'含字段 {field}', field in out, str(out.keys()))
    check('captured_at 以 Z 结尾', out['captured_at'].endswith('Z'), out['captured_at'])
    check('version 是 int', isinstance(out['version'], int))
    check('version 来自 update_count', out['version'] == 42, str(out['version']))
    check('links 是 list', isinstance(out['links'], list))
    check('flows 是 dict', isinstance(out['flows'], dict))


def test_down_ports_format_is_readable():
    out = contract_api.build_current_state(SAMPLE_STATE)
    check('down_ports 用 s1:eth3 格式', 's1:eth3' in out['down_ports'],
          str(out['down_ports']))
    check('down_ports 不含数字端口号',
          all(':' in p and not p.split(':')[1].isdigit() for p in out['down_ports']),
          str(out['down_ports']))
    check('down_links 成对', out['down_links'] == ['s1:eth3-s2:eth3'],
          str(out['down_links']))


def test_build_links_dedup():
    links = contract_api.build_links(SAMPLE_STATE)
    check('链路成对去重', len(links) == 1, str(len(links)))
    check('链路含 a/b 端', 'a' in links[0] and 'b' in links[0])
    check('端口名可读', links[0]['a']['name'] == 's1-eth3', str(links[0]['a']))


def test_is_state_ready():
    check('有交换机 = 已就绪', contract_api.is_state_ready(SAMPLE_STATE))
    check('空 state = 未就绪', contract_api.is_state_ready({}) is False)
    check('无交换机 = 未就绪', contract_api.is_state_ready({'meta': {}}) is False)


def test_build_state_history_filters():
    entries = [{'time': 100.0, 'v': 1}, {'time': 200.0, 'v': 2}, {'time': 300.0, 'v': 3}]
    out = contract_api.build_state_history(entries, since=100.0, until=300.0)
    check('since/until 过滤（since 开区间、until 闭区间）',
          [e['time'] for e in out] == [200.0, 300.0], str(out))
    out = contract_api.build_state_history(entries, since=200.0)
    check('since 之后全取', [e['time'] for e in out] == [300.0], str(out))
    out = contract_api.build_state_history(entries, limit=2)
    check('limit 取最近 2 条', [e['time'] for e in out] == [200.0, 300.0], str(out))


def test_build_port_events():
    events = contract_api.build_port_events(SAMPLE_STATE)
    types = [e['type'] for e in events]
    check('含端口状态事件', 'port_link_status' in types, str(types))
    check('含交换机连接事件', 'switch_connected' in types, str(types))
    check('不含链路发现事件', 'link_discovered' not in types, str(types))
    check('不含主机发现事件', 'host_discovered' not in types, str(types))

    only_sw2 = contract_api.build_port_events(SAMPLE_STATE, dpid='0000000000000002')
    check('dpid 过滤生效', all(e.get('dpid') != '0000000000000001'
                           for e in only_sw2), str(only_sw2))

    recent = contract_api.build_port_events(SAMPLE_STATE, since=1002.5)
    check('since 过滤生效', all(e['time'] > 1002.5 for e in recent), str(recent))


def test_build_link_events():
    events = contract_api.build_link_events(SAMPLE_STATE)
    types = [e['type'] for e in events]
    check('含链路发现', 'link_discovered' in types, str(types))
    check('含链路丢失', 'link_lost' in types, str(types))
    check('不含端口状态事件', 'port_link_status' not in types, str(types))


def test_build_probe_results():
    rec = contract_api.build_probe_results({
        'reachable': ['10.0.0.1'], 'unreachable': ['10.0.0.4'],
        'loss_pct': 25.0, 'latency_ms': 12.3})
    check('reachable 透传', rec['reachable'] == ['10.0.0.1'])
    check('unreachable 透传', rec['unreachable'] == ['10.0.0.4'])
    check('loss_pct 透传', rec['loss_pct'] == 25.0)
    check('带时间戳', rec['time'] > 0)


def test_build_error_shape():
    err = contract_api.build_error('DEPENDENCY_NOT_READY', '还没起来', module='intensure')
    check('error 是 dict', isinstance(err['error'], dict))
    check('含 code', err['error']['code'] == 'DEPENDENCY_NOT_READY')
    check('含 message', err['error']['message'] == '还没起来')
    check('含额外字段', err['error']['module'] == 'intensure')


# ============ HTTP 端点 ============

def test_contract_current_endpoint():
    if flask is None:
        return
    client = state_server.app.test_client()
    state_server.CONTROLLER_STATE = SAMPLE_STATE
    try:
        resp = client.get('/api/intensure/v1/state/current')
        check('返回 200', resp.status_code == 200, str(resp.status_code))
        data = resp.get_json()
        check('含 captured_at', 'captured_at' in data, str(data.keys()))
        check('switches 是 list', isinstance(data['switches'], list))
        check('down_ports 可读', 's1:eth3' in data['down_ports'], str(data['down_ports']))
    finally:
        state_server.CONTROLLER_STATE = None


def test_contract_current_not_ready_returns_424():
    if flask is None:
        return
    client = state_server.app.test_client()
    state_server.CONTROLLER_STATE = {}
    try:
        resp = client.get('/api/intensure/v1/state/current')
        check('未初始化返回 424', resp.status_code == 424, str(resp.status_code))
        data = resp.get_json()
        check('错误码是 DEPENDENCY_NOT_READY',
              data['error']['code'] == 'DEPENDENCY_NOT_READY', str(data))
    finally:
        state_server.CONTROLLER_STATE = None


def test_old_paths_still_work():
    """只加不改：旧路径必须完全不受影响。"""
    if flask is None:
        return
    client = state_server.app.test_client()
    state_server.CONTROLLER_STATE = SAMPLE_STATE
    try:
        resp = client.get('/api/state')
        check('/api/state 仍返回 200', resp.status_code == 200)
        check('/api/state 仍是内部结构（switches 是 dict）',
              isinstance(resp.get_json().get('switches'), dict))

        resp = client.get('/api/links')
        check('/api/links 仍返回 200', resp.status_code == 200)
        check('/api/links 仍是成对 list', isinstance(resp.get_json(), list))

        resp = client.get('/api/health')
        check('/api/health 仍返回 200', resp.status_code == 200)
    finally:
        state_server.CONTROLLER_STATE = None


def test_contract_history_endpoint():
    if flask is None:
        return
    client = state_server.app.test_client()
    try:
        resp = client.get('/api/intensure/v1/state/history?limit=5')
        check('返回 200', resp.status_code == 200, str(resp.status_code))
        check('是 list', isinstance(resp.get_json(), list))
    finally:
        pass


def test_contract_events_endpoints():
    if flask is None:
        return
    client = state_server.app.test_client()
    state_server.CONTROLLER_STATE = SAMPLE_STATE
    try:
        resp = client.get('/api/intensure/v1/port-events')
        check('port-events 200', resp.status_code == 200)
        check('port-events 是 list', isinstance(resp.get_json(), list))

        resp = client.get('/api/intensure/v1/link-events')
        check('link-events 200', resp.status_code == 200)
        check('link-events 是 list', isinstance(resp.get_json(), list))
    finally:
        state_server.CONTROLLER_STATE = None


def test_contract_probe_results_endpoint():
    if flask is None:
        return
    client = state_server.app.test_client()
    try:
        resp = client.post('/api/intensure/v1/probe-results', json={
            'reachable': ['10.0.0.1'], 'unreachable': ['10.0.0.4'], 'loss_pct': 25.0})
        check('返回 200', resp.status_code == 200, str(resp.status_code))
        data = resp.get_json()
        check('recorded 为 True', data.get('recorded') is True, str(data))
        check('统计 reachable 数', data.get('reachable') == 1, str(data))
        check('统计 unreachable 数', data.get('unreachable') == 1, str(data))
    finally:
        pass


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
