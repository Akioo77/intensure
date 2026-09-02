"""ActualState 组装器：内部 state → 联调说明 §4.2 约定的 ActualState 格式。

设计原则（v2.0）:
  1. 严格按采集模块联调说明 §4.2 输出字段
  2. down_links / down_ports 用 "swN:pN" 字符串格式（不是 dpid 数字）
  3. 内部数据 vs 联调契约的映射封装在本类内部
  4. 字段缺失时按"无 SLA 传 null / 链路未断不传"的规则处理
  5. 无副作用（不读网络、不发请求、纯函数）

主入口:
  builder = ActualStateBuilder(dpid_to_switch={'0000000000000001': 'sw1', ...})
  actual = builder.build(intent_id='REQ-001-CI-001', source_host='10.0.0.1',
                         destination_host='10.0.0.2',
                         state=NETWORK_STATE,
                         reachability_result={'reachable': True, 'latency_ms': 12.3,
                                              'packet_loss': 0.0},
                         expected_flow_present=True)
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any


# 交换机端口 LINK_DOWN 位（OFPPS_LINK_DOWN = 1 << 0）
OFPPS_LINK_DOWN = 1 << 0

# 本地管理端口永远 LINK_DOWN，跳过
OFPP_LOCAL = 0xfffffffe


class ActualStateBuilder:
    """将内部网络状态转换为联调契约的 ActualState 格式。"""

    def __init__(self, dpid_to_switch: Optional[Dict[str, str]] = None):
        """初始化。

        参数:
            dpid_to_switch: dpid_hex → 逻辑交换机名映射表，例如：
                {'0000000000000001': 'sw1', '0000000000000002': 'sw2'}
                如果不传，则使用默认映射（前 16 个字符截断 + '0x'）
        """
        self.dpid_to_switch = dpid_to_switch or {}

    # ============================================================
    # 公开 API
    # ============================================================

    def build(self,
              intent_id: str,
              source_host: Optional[str],
              destination_host: Optional[str],
              state: dict,
              reachability_result: dict,
              expected_flow_present: bool) -> dict:
        """组装一条 ActualState。

        参数:
            intent_id: 上游透传的意图 ID（如 REQ-001-CI-001）
            source_host: 实际源主机 IP（如 10.0.0.5），可传 None
            destination_host: 实际目的主机 IP，可传 None
            state: 内部 NETWORK_STATE 字典（含 switches/hosts/flows/links 等）
            reachability_result: 探测结果 {'reachable': bool, 'latency_ms': float|None,
                                          'packet_loss': float|None}
            expected_flow_present: 流表比对结果（手工或自动）

        返回:
            ActualState JSON dict（与联调 §4.2 字段完全一致）
        """
        now_iso = self._iso_now()

        down_links = self._extract_down_links(state)
        down_ports = self._extract_down_ports(state)

        actual = {
            'intent_id': intent_id,
            'checked_at': now_iso,
            'reachable': bool(reachability_result.get('reachable', False)),
            'expected_flow_present': bool(expected_flow_present),
        }

        # 选填字段：None/空值时不传
        if source_host:
            actual['source_host'] = source_host
        if destination_host:
            actual['destination_host'] = destination_host

        # latency_ms / packet_loss：无 SLA 需求时传 null
        if 'latency_ms' in reachability_result:
            actual['latency_ms'] = reachability_result['latency_ms']
        if 'packet_loss' in reachability_result:
            actual['packet_loss'] = reachability_result['packet_loss']

        if down_links:
            actual['down_links'] = down_links
        if down_ports:
            actual['down_ports'] = down_ports

        return actual

    def build_after_heal(self, **kwargs) -> dict:
        """复测场景的便捷封装（字段语义同 build）。"""
        return self.build(**kwargs)

    # ============================================================
    # 内部映射
    # ============================================================

    def _extract_down_links(self, state: dict) -> List[str]:
        """从 NETWORK_STATE.links 中找出 down 的链路。

        联调契约格式："sw1:p1-sw2:p2"，本端端口 down 时**双向计入**。
        输出按字典序排序，便于测试断言。
        """
        # 方法 1：从 switches.ports 里直接找 state & LINK_DOWN 的端口
        # 然后和 links 表交叉
        down_ports_by_dpid: Dict[str, set] = {}
        for dpid, sw in state.get('switches', {}).items():
            ports_down = set()
            for p in sw.get('ports', []):
                if p.get('port_no') == OFPP_LOCAL:
                    continue
                if p.get('state', 0) & OFPPS_LINK_DOWN:
                    ports_down.add(p.get('port_no'))
            if ports_down:
                down_ports_by_dpid[dpid] = ports_down

        down_pairs: List[tuple] = []
        seen_pairs: set = set()
        for key, link in state.get('links', {}).items():
            try:
                dpid_a, port_a_str = key.split(':')
                port_a = int(port_a_str)
            except (ValueError, AttributeError):
                continue
            peer_dpid = link.get('peer_dpid')
            peer_port = link.get('peer_port')
            if peer_dpid is None or peer_port is None:
                continue

            # 任一端 down → 整条链路记为 down
            a_down = port_a in down_ports_by_dpid.get(dpid_a, set())
            b_down = peer_port in down_ports_by_dpid.get(peer_dpid, set())
            if not (a_down or b_down):
                continue

            # 端点规范化为 (sw_a, p_a) 和 (sw_b, p_b)
            sw_a = self._dpid_to_switch_name(dpid_a)
            sw_b = self._dpid_to_switch_name(peer_dpid)
            ep_a = (sw_a, port_a)
            ep_b = (sw_b, peer_port)
            # 用 frozenset 去重（A-B 和 B-A 是同一条）
            pair = frozenset([ep_a, ep_b])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            # 输出顺序：字典序小的端点在前
            ep_sorted = sorted([ep_a, ep_b], key=lambda x: (x[0], x[1]))
            a, b = ep_sorted
            down_pairs.append((a, b))

        # 全链路字典序
        down_pairs.sort(key=lambda pair: (pair[0][0], pair[0][1], pair[1][0], pair[1][1]))
        return [f'{a[0]}:p{a[1]}-{b[0]}:p{b[1]}' for a, b in down_pairs]

    def _extract_down_ports(self, state: dict) -> List[str]:
        """提取所有 down 的端口（不限于交换机间链路）。

        联调契约格式："sw1:p1"，按 (switch, port) 字典序排序。
        """
        down_list: List[tuple] = []
        for dpid, sw in state.get('switches', {}).items():
            for p in sw.get('ports', []):
                if p.get('port_no') == OFPP_LOCAL:
                    continue
                if p.get('state', 0) & OFPPS_LINK_DOWN:
                    sw_name = self._dpid_to_switch_name(dpid)
                    down_list.append((sw_name, p.get('port_no')))
        down_list.sort()
        return [f'{sw}:p{port}' for sw, port in down_list]

    def _dpid_to_switch_name(self, dpid_hex: str) -> str:
        """dpid_hex → 联调约定的 swN 字符串。

        默认行为：如果没传映射表，从 dpid 末尾数字推断（'0000000000000001' → 'sw1'）。
        """
        if dpid_hex in self.dpid_to_switch:
            return self.dpid_to_switch[dpid_hex]
        # fallback：取 dpid 数字部分的末尾
        try:
            n = int(dpid_hex, 16)
            return f'sw{n}'
        except (ValueError, TypeError):
            return f'sw_{dpid_hex[-4:]}'

    @staticmethod
    def _iso_now() -> str:
        """返回带 +08:00 时区的 ISO 8601 时间戳。"""
        tz = timezone(timedelta(hours=8))
        return datetime.now(tz).isoformat(timespec='seconds')


# ============================================================
# CLI / 自检
# ============================================================

if __name__ == '__main__':
    import json

    # 构造示例 state
    sample_state = {
        'switches': {
            '0000000000000001': {
                'ports': [
                    {'port_no': OFPP_LOCAL, 'state': 1, 'name': 's1'},
                    {'port_no': 1, 'state': 0, 'name': 's1-eth1'},
                    {'port_no': 2, 'state': 0, 'name': 's1-eth2'},
                    {'port_no': 3, 'state': 4, 'name': 's1-eth3'},  # LIVE
                ],
            },
            '0000000000000002': {
                'ports': [
                    {'port_no': OFPP_LOCAL, 'state': 1, 'name': 's2'},
                    {'port_no': 1, 'state': 0, 'name': 's2-eth1'},
                    {'port_no': 2, 'state': 0, 'name': 's2-eth2'},
                    {'port_no': 3, 'state': 1, 'name': 's2-eth3'},  # DOWN
                ],
            },
        },
        'links': {
            '0000000000000001:3': {
                'peer_dpid': '0000000000000002', 'peer_port': 3,
                'peer_name': 's2-eth3',
            },
        },
    }

    builder = ActualStateBuilder()
    actual = builder.build(
        intent_id='REQ-001-CI-001',
        source_host='10.0.0.1',
        destination_host='10.0.0.2',
        state=sample_state,
        reachability_result={'reachable': False, 'latency_ms': None, 'packet_loss': None},
        expected_flow_present=True,
    )
    print(json.dumps(actual, indent=2, ensure_ascii=False))