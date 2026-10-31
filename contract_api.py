"""contract_api.py — 跨模块契约格式的视图层（v3.20.0）

背景（docs/cross-module-api-contract.md §4.1 / §5.1）：
  契约要求采集模块暴露 `GET /api/intensure/v1/state/current` 等带前缀端点，
  而我们实际暴露的是 `/api/state`（不带前缀）。对方按契约调 → 全部 404。

为什么不能简单改路径：
  改前缀会破坏现有联调（dashboard、演示脚本、对方已写好的调用都在用 /api/*）。
  所以做法是 **加别名**：旧的 /api/* 全部保留，新的 /api/intensure/v1/* 并行提供。

注意：内部 state 是「我们自己写的数据结构」，契约要的是「对方要看的结构」，
两者不是一回事 —— switches 内部是 dict（按 dpid 键），契约要的是 list；
契约还要 version（乐观锁）、down_ports / down_links（我们算好给对方直接用）。
所以这里做**显式转换**，而不是换个 URL 直接透传。

纯函数模块：不读网络、不发请求、不依赖 Flask。
"""
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from actual_state_builder import ActualStateBuilder

# 交换机本地管理端口（OpenFlow 1.3：OFPP_LOCAL = 0xfffffffe）
# 它永远 LINK_DOWN、没有线缆，不算真实端口 —— 契约样例里也只有数据端口
OFPP_LOCAL = 0xfffffffe

# 端口事件类型（控制器实际会记的）
PORT_EVENT_TYPES = ('port_link_status',)
LINK_EVENT_TYPES = ('link_discovered', 'link_lost')
SWITCH_EVENT_TYPES = ('switch_connected', 'switch_disconnected')


def _iso_utc_now() -> str:
    """UTC ISO 8601（毫秒精度，带 Z）—— 契约 §4.1 的 captured_at 格式。"""
    return (datetime.now(timezone.utc)
            .isoformat(timespec='milliseconds')
            .replace('+00:00', 'Z'))


def _switch_name(dpid_hex: str) -> str:
    """dpid → 设备名（Mininet 约定：末位数字 → s1 / s2）。"""
    try:
        return f's{int(dpid_hex, 16)}'
    except (ValueError, TypeError):
        return f's_{str(dpid_hex)[-4:]}'


def _port_label(port: dict) -> str:
    """端口名 → 短标签（'s1-eth2' → 'eth2'）。"""
    name = port.get('name') or ''
    if name and '-' in name:
        return name.split('-')[-1]
    if name:
        return name
    return f'p{port.get("port_no", "?")}'


def build_switches(state: dict) -> List[Dict[str, Any]]:
    """内部 switches dict → 契约的 list。

    每个元素: {"dpid": "0000000000000001", "ports": [{port_no, name, state, speed_mbps}]}
    """
    result = []
    for dpid in sorted(state.get('switches', {}).keys()):
        sw = state.get('switches', {}).get(dpid) or {}
        ports = []
        for p in sw.get('ports', []):
            if p.get('port_no') == OFPP_LOCAL:
                continue
            curr_speed = p.get('curr_speed')
            ports.append({
                'port_no': p.get('port_no'),
                'name': p.get('name'),
                'state': p.get('state', 0),
                'speed_mbps': int(curr_speed) // 1000 if curr_speed else 0,
            })
        result.append({'dpid': dpid, 'ports': ports})
    return result


def build_links(state: dict) -> List[Dict[str, Any]]:
    """内部 links dict（"dpid:port" → peer）→ 契约的成对 list。

    形如: [{"a": {"dpid","port_no","name"}, "b": {...}, "last_seen": ts}]
    """
    links = state.get('links', {}) or {}
    switches = state.get('switches', {}) or {}

    def port_name(dpid, pn) -> Optional[str]:
        for pp in (switches.get(dpid, {}) or {}).get('ports', []):
            if pp.get('port_no') == pn:
                return pp.get('name')
        return None

    pairs = []
    seen = set()
    for key, v in links.items():
        try:
            dpid, port = key.split(':')
            dpid, port = dpid, int(port)
            other = (v['peer_dpid'], v['peer_port'])
            pkey = frozenset([(dpid, port), other])
            if pkey in seen:
                continue
            seen.add(pkey)
            pairs.append({
                'a': {'dpid': dpid, 'port_no': port, 'name': port_name(dpid, port)},
                'b': {'dpid': other[0], 'port_no': other[1],
                      'name': port_name(other[0], other[1])},
                'last_seen': v['last_seen'],
            })
        except Exception:
            continue
    return pairs


def build_current_state(state: dict,
                         dpid_to_switch: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """内部 state → 契约 §4.1 的 GET /api/intensure/v1/state/current 响应体。

    字段:
      captured_at  UTC ISO8601
      version      乐观锁版本号（meta.update_count，单调递增）
      switches     list（不是 dict）
      links        list（成对）
      flows        {dpid: [...]} 透传
      down_ports   ["s1:eth3"]      —— 我们算好，对方直接用
      down_links   ["s1:eth3-s2:eth3"]
    """
    builder = ActualStateBuilder(dpid_to_switch=dpid_to_switch)
    meta = state.get('meta', {}) or {}
    return {
        'captured_at': _iso_utc_now(),
        'version': int(meta.get('update_count', 0) or 0),
        'switches': build_switches(state),
        'links': build_links(state),
        'flows': state.get('flows', {}) or {},
        'down_ports': builder.down_ports(state),
        'down_links': builder.down_links(state),
    }


def is_state_ready(state: dict) -> bool:
    """采集是否已初始化（没有交换机 = 还没起来）。

    契约要求此时返回 424 DEPENDENCY_NOT_READY，而不是 200 空快照。
    """
    if not state:
        return False
    return bool(state.get('switches')) or bool(state.get('meta', {}).get('start_time'))


def build_state_history(entries: List[dict],
                        since: float = 0.0,
                        until: float = 0.0,
                        limit: int = 100) -> List[dict]:
    """历史状态条目 → 按 since/until 过滤 + limit 截断（取最近的 limit 条）。"""
    filtered = []
    for e in entries or []:
        t = e.get('time', 0) or 0
        if since and t <= since:
            continue
        if until and t > until:
            continue
        filtered.append(e)
    if limit and limit > 0:
        filtered = filtered[-limit:]
    return filtered


def build_port_events(state: dict,
                      dpid: Optional[str] = None,
                      since: float = 0.0,
                      limit: int = 200) -> List[dict]:
    """端口事件流（契约 GET /api/intensure/v1/port-events）。

    包含 port_link_status（端口 up/down 翻转）以及交换机连接事件
    （会改变端口可达性，一并给出）。
    """
    events = state.get('events', []) or []
    out = []
    for e in events:
        etype = e.get('type')
        if etype not in PORT_EVENT_TYPES and etype not in SWITCH_EVENT_TYPES:
            continue
        t = e.get('time', 0) or 0
        if since and t <= since:
            continue
        if dpid and e.get('dpid') and e.get('dpid') != dpid:
            continue
        out.append(e)
    if limit and limit > 0:
        out = out[-limit:]
    return out


def build_link_events(state: dict,
                      since: float = 0.0,
                      limit: int = 200) -> List[dict]:
    """链路事件流（契约 GET /api/intensure/v1/link-events）。

    链路发现 / 丢失（LLDP），并附带交换机连接事件。
    """
    events = state.get('events', []) or []
    out = []
    for e in events:
        etype = e.get('type')
        if etype not in LINK_EVENT_TYPES and etype not in SWITCH_EVENT_TYPES:
            continue
        t = e.get('time', 0) or 0
        if since and t <= since:
            continue
        out.append(e)
    if limit and limit > 0:
        out = out[-limit:]
    return out


def build_probe_results(body: dict) -> Dict[str, Any]:
    """契约 POST /api/intensure/v1/probe-results 的请求体 → 内部 connectivity 记录。

    body: {"reachable": [...], "unreachable": [...], "loss_pct": 0.0, "latency_ms": 12.3}
    """
    return {
        'time': time.time(),
        'reachable': body.get('reachable', []) or [],
        'unreachable': body.get('unreachable', []) or [],
        'loss_pct': body.get('loss_pct', 0),
        'latency_ms': body.get('latency_ms'),
    }


def build_error(code: str, message: str, **extra) -> Dict[str, Any]:
    """契约 §3 的统一错误体：{"error": {"code","message",...}}。"""
    err = {'code': code, 'message': message}
    err.update(extra)
    return {'error': err}
