"""单元测试：ActualStateBuilder

覆盖场景：
  1. 基本字段映射（intent_id / checked_at / reachable / expected_flow_present）
  2. down_links 提取（链路双向任一端 down）
  3. down_ports 提取（含 OFPP_LOCAL 跳过）
  4. 选填字段：source_host / destination_host 为 None 时不传
  5. latency_ms / packet_loss 显式传 null 时输出 null
  6. 字段映射：dpid → swN 字符串
  7. 双向链路去重（A-B 和 B-A 是同一条）
  8. 时间戳格式（ISO 8601 + +08:00）
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from actual_state_builder import (
    ActualStateBuilder, OFPPS_LINK_DOWN, OFPP_LOCAL,
)


PASS = '✅'
FAIL = '❌'
passed = 0
failed = 0


def check(name, condition, detail=''):
    global passed, failed
    if condition:
        print(f'  {PASS} {name}')
        passed += 1
    else:
        print(f'  {FAIL} {name}  {detail}')
        failed += 1


# ============ 测试用例 ============

def make_state(switches=None, links=None):
    """构造测试用 state."""
    return {
        'switches': switches or {},
        'links': links or {},
    }


def test_basic_fields():
    print('\n[1] 基本字段映射')
    builder = ActualStateBuilder()
    state = make_state()
    actual = builder.build(
        intent_id='REQ-001-CI-001',
        source_host='10.0.0.1',
        destination_host='10.0.0.2',
        state=state,
        reachability_result={'reachable': True, 'latency_ms': 12.3,
                              'packet_loss': 0.0},
        expected_flow_present=True,
    )
    check('intent_id 必填', actual['intent_id'] == 'REQ-001-CI-001')
    check('reachable 必填', actual['reachable'] is True)
    check('expected_flow_present 必填', actual['expected_flow_present'] is True)
    check('source_host 透传', actual['source_host'] == '10.0.0.1')
    check('destination_host 透传', actual['destination_host'] == '10.0.0.2')
    check('latency_ms 透传', actual['latency_ms'] == 12.3)
    check('packet_loss 透传', actual['packet_loss'] == 0.0)


def test_down_link_extraction():
    print('\n[2] down_links 提取')
    # sw1 port 3 down，links 显示 sw1-p2 ↔ sw2-p3
    state = make_state(
        switches={
            '0000000000000001': {
                'ports': [
                    {'port_no': OFPP_LOCAL, 'state': 1, 'name': 's1'},
                    {'port_no': 1, 'state': 0, 'name': 's1-eth1'},
                    {'port_no': 2, 'state': OFPPS_LINK_DOWN, 'name': 's1-eth2'},
                ],
            },
            '0000000000000002': {
                'ports': [
                    {'port_no': OFPP_LOCAL, 'state': 1, 'name': 's2'},
                    {'port_no': 3, 'state': 0, 'name': 's2-eth3'},
                ],
            },
        },
        links={
            '0000000000000001:2': {
                'peer_dpid': '0000000000000002',
                'peer_port': 3,
            },
        },
    )

    builder = ActualStateBuilder()
    actual = builder.build(
        intent_id='REQ-001', source_host=None, destination_host=None,
        state=state,
        reachability_result={'reachable': False},
        expected_flow_present=False,
    )
    check('down_links 已提取', 'down_links' in actual,
          f'actual keys: {list(actual.keys())}')
    check('down_links 格式正确',
          actual.get('down_links') == ['s1:eth2-s2:eth3'],
          f"got: {actual.get('down_links')}")


def test_down_ports_skip_local():
    print('\n[3] down_ports 跳过 OFPP_LOCAL')
    state = make_state(
        switches={
            '0000000000000001': {
                'ports': [
                    {'port_no': OFPP_LOCAL, 'state': OFPPS_LINK_DOWN, 'name': 's1'},
                    {'port_no': 1, 'state': OFPPS_LINK_DOWN, 'name': 's1-eth1'},
                ],
            },
        },
    )
    builder = ActualStateBuilder()
    actual = builder.build(
        intent_id='REQ-001', source_host=None, destination_host=None,
        state=state,
        reachability_result={'reachable': False},
        expected_flow_present=False,
    )
    check('down_ports 只含数据口', actual.get('down_ports') == ['s1:eth1'],
          f"got: {actual.get('down_ports')}")


def test_optional_fields_none():
    print('\n[4] 选填字段为 None 时不输出')
    builder = ActualStateBuilder()
    state = make_state()
    actual = builder.build(
        intent_id='REQ-001',
        source_host=None, destination_host=None,
        state=state,
        reachability_result={'reachable': True},  # 没传 latency/packet_loss
        expected_flow_present=True,
    )
    check('source_host=None 不传', 'source_host' not in actual)
    check('destination_host=None 不传', 'destination_host' not in actual)
    check('latency_ms 缺省不传', 'latency_ms' not in actual)
    check('packet_loss 缺省不传', 'packet_loss' not in actual)


def test_latency_null_explicit():
    print('\n[5] latency_ms 显式 null 时输出 null')
    builder = ActualStateBuilder()
    state = make_state()
    actual = builder.build(
        intent_id='REQ-001',
        source_host=None, destination_host=None,
        state=state,
        reachability_result={'reachable': False, 'latency_ms': None, 'packet_loss': None},
        expected_flow_present=True,
    )
    check('latency_ms=null 输出 null', 'latency_ms' in actual and actual['latency_ms'] is None,
          f"got: {actual.get('latency_ms')}")
    check('packet_loss=null 输出 null', 'packet_loss' in actual and actual['packet_loss'] is None,
          f"got: {actual.get('packet_loss')}")


def test_dpid_to_switch_mapping():
    print('\n[6] dpid → swN 映射')
    # 自定义映射
    builder = ActualStateBuilder({'0000000000000001': 'sw1', '0000000000000002': 'sw2'})
    state = make_state(
        switches={
            '0000000000000001': {'ports': [{'port_no': 1, 'state': OFPPS_LINK_DOWN}]},
        },
    )
    actual = builder.build(
        intent_id='REQ-001', source_host=None, destination_host=None,
        state=state,
        reachability_result={'reachable': False},
        expected_flow_present=False,
    )
    check('自定义映射生效', 'sw1:p1' in actual.get('down_ports', []),
          f"got: {actual.get('down_ports')}")
    # 注：自定义 sw1 + 端口无 name → fallback 'p{port_no}'，所以是 'sw1:p1'

    # 默认映射
    builder2 = ActualStateBuilder()
    state2 = make_state(
        switches={'0000000000000003': {'ports': [{'port_no': 5, 'state': OFPPS_LINK_DOWN}]}},
    )
    actual2 = builder2.build(
        intent_id='REQ-001', source_host=None, destination_host=None,
        state=state2,
        reachability_result={'reachable': False},
        expected_flow_present=False,
    )
    check('默认映射 sN 推断（Mininet 约定）', 's3:p5' in actual2.get('down_ports', []),
          f"got: {actual2.get('down_ports')}")
    # 注：默认 + 端口无 name → fallback 'p{port_no}'


def test_dedup_bidirectional_link():
    print('\n[7] 双向链路去重（A-B 与 B-A 是同一条）')
    state = make_state(
        switches={
            '0000000000000001': {'ports': [{'port_no': 1, 'state': OFPPS_LINK_DOWN}]},
            '0000000000000002': {'ports': [{'port_no': 1, 'state': OFPPS_LINK_DOWN}]},
        },
        links={
            # 双向都记录（LLDP 自动发现两端互指）
            '0000000000000001:1': {
                'peer_dpid': '0000000000000002', 'peer_port': 1,
            },
            '0000000000000002:1': {
                'peer_dpid': '0000000000000001', 'peer_port': 1,
            },
        },
    )
    builder = ActualStateBuilder()
    actual = builder.build(
        intent_id='REQ-001', source_host=None, destination_host=None,
        state=state,
        reachability_result={'reachable': False},
        expected_flow_present=False,
    )
    check('双向链路去重为 1 条',
          len(actual.get('down_links', [])) == 1,
          f"got: {actual.get('down_links')}")


def test_iso_timestamp():
    print('\n[8] ISO 8601 时间戳（含 +08:00）')
    builder = ActualStateBuilder()
    state = make_state()
    actual = builder.build(
        intent_id='REQ-001', source_host=None, destination_host=None,
        state=state,
        reachability_result={'reachable': True},
        expected_flow_present=True,
    )
    check('checked_at 字段存在', 'checked_at' in actual)
    check('checked_at 含 +08:00', '+08:00' in actual['checked_at'],
          f"got: {actual['checked_at']}")


def main():
    print('=' * 60)
    print('ActualStateBuilder 单元测试')
    print('=' * 60)
    test_basic_fields()
    test_down_link_extraction()
    test_down_ports_skip_local()
    test_optional_fields_none()
    test_latency_null_explicit()
    test_dpid_to_switch_mapping()
    test_dedup_bidirectional_link()
    test_iso_timestamp()
    print('\n' + '=' * 60)
    print(f'测试结果: {passed} 通过 / {failed} 失败')
    print('=' * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()