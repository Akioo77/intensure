"""自愈意图生成器 - 将偏差(deviation)转换为可执行的自愈意图(healing intent)。

输入: 一致性检验产生的 deviation
输出: 机器可读的自愈意图 dict, 描述"该让意图实现智能体做什么"

偏差类型 → 自愈意图模板:
  link_status (链路DOWN/UP不一致)   → reroute_link / recover_link
  connectivity (连通性违反)         → reapply_acl / probe_recheck
  qos (时延/丢包超限)               → adjust_qos / boost_bandwidth
  reachability_violation            → reroute (改路径)
  conflict (与其他意图冲突)         → supersede_intent

自愈意图 Schema:
  {
    "healing_id": "heal-xxx",
    "action": "reroute_link|recover_link|reapply_acl|adjust_qos|...",
    "target": { ... },          # 操作对象
    "deadline_ms": 5000,        # 期望完成时间
    "rollback_action": "..." ,  # 失败时回滚
    "parameters": { ... },      # 操作参数
    "rationale": "...",         # 人话解释
  }
"""
import time
from typing import Dict, List


# 动作类型常量
ACTION_REROUTE = 'reroute_link'
ACTION_RECOVER = 'recover_link'
ACTION_REAPPLY_ACL = 'reapply_acl'
ACTION_ADJUST_QOS = 'adjust_qos'
ACTION_BOOST_BANDWIDTH = 'boost_bandwidth'
ACTION_PROBE_RECHECK = 'probe_recheck'
ACTION_SUPERSEDE = 'supersede_intent'


# 默认参数
DEFAULT_DEADLINE_MS = 5000
MAX_PUSH_ATTEMPTS = 3
MAX_VERIFY_ATTEMPTS = 5


def _make_id(prefix='heal') -> str:
    return f'{prefix}-{int(time.time()*1000)}'


def generate(deviation: Dict, intent: Dict = None) -> Dict:
    """根据 deviation 生成自愈意图.

    参数:
      deviation: 一致性检验输出, 形如:
        {
          "type": "link_status" | "connectivity" | "qos" | ...,
          "severity": "critical" | "warning" | "info",
          "link": "s1-s2",            # link_status
          "expected": "up", "actual": "down",   # link_status
          "assertion": {...},         # connectivity / qos
          "actual": 30, "expected_max": 50,      # qos
          "message": "...",
        }
      intent: 触发的意图对象 (可选, 提供更丰富的生成上下文)

    返回: healing intent dict
    """
    dtype = deviation.get('type', '')
    if dtype == 'link_status':
        return _gen_link_status(deviation, intent)
    elif dtype == 'connectivity':
        return _gen_connectivity(deviation, intent)
    elif dtype == 'qos':
        return _gen_qos(deviation, intent)
    elif dtype == 'reachability_violation':
        return _gen_reachability(deviation, intent)
    elif dtype == 'conflict':
        return _gen_conflict(deviation, intent)
    else:
        return _gen_generic(deviation, intent)


def _gen_link_status(dev: Dict, intent: Dict) -> Dict:
    """链路 DOWN → 重新激活链路."""
    link = dev.get('link', 'unknown')
    actual = dev.get('actual', 'down')
    if actual == 'down':
        action = ACTION_RECOVER
        target = {'type': 'link', 'link': link}
        rollback = ACTION_REROUTE
        rationale = f'链路 {link} 实际 DOWN, 自动恢复'
    else:  # 实际 UP, 期望 DOWN (罕见, 隔离态)
        action = ACTION_REROUTE
        target = {'type': 'link', 'link': link, 'state': 'down'}
        rollback = ACTION_RECOVER
        rationale = f'链路 {link} 实际 UP, 期望 DOWN (异常)'
    return {
        'healing_id': _make_id(),
        'action': action,
        'target': target,
        'deadline_ms': DEFAULT_DEADLINE_MS,
        'rollback_action': rollback,
        'parameters': {
            'method': 'reapply_physical_link',
            'fallback_path': _alt_path_around(link),
        },
        'rationale': rationale,
        'source_deviation': dev,
        'generated_at': time.time(),
    }


def _gen_connectivity(dev: Dict, intent: Dict) -> Dict:
    """连通性违反 → 重下发 ACL 或重新探测."""
    assertion = dev.get('assertion', {})
    allow = assertion.get('allow', True)
    src = assertion.get('src', '?')
    dst = assertion.get('dst', '?')

    if allow:
        # 要求连通但不通 → 重下发 ACL/路由
        action = ACTION_REAPPLY_ACL
        target = {'type': 'acl', 'src': src, 'dst': dst, 'allow': True}
        rationale = f'连通意图违规：{src} ↔ {dst} 不通, 重新下发 ACL 允许'
    else:
        # 要求隔离但能通 → 重下发隔离策略
        action = ACTION_REAPPLY_ACL
        target = {'type': 'acl', 'src': src, 'dst': dst, 'allow': False}
        rationale = f'隔离意图违规：{src} ↔ {dst} 仍互通, 重新下发隔离策略'

    return {
        'healing_id': _make_id(),
        'action': action,
        'target': target,
        'deadline_ms': DEFAULT_DEADLINE_MS,
        'rollback_action': ACTION_PROBE_RECHECK,
        'parameters': {
            'method': 'reapply_acl_via_controller',
            'src_hosts': _resolve_hosts_safe(intent, src),
            'dst_hosts': _resolve_hosts_safe(intent, dst),
        },
        'rationale': rationale,
        'source_deviation': dev,
        'generated_at': time.time(),
    }


def _gen_qos(dev: Dict, intent: Dict) -> Dict:
    """QoS 不达标 → 调整 QoS 参数."""
    assertion = dev.get('assertion', {})
    link = assertion.get('link', 'unknown')
    actual = dev.get('actual', 0)

    if assertion.get('max_latency_ms') is not None:
        max_lat = assertion['max_latency_ms']
        # 目标时延减 20% 留 buffer
        target_lat = max(int(max_lat * 0.8), max_lat - 5)
        action = ACTION_ADJUST_QOS
        target = {'type': 'qos', 'link': link, 'metric': 'latency',
                  'current_ms': actual, 'target_ms': target_lat}
        rationale = (
            f'QoS违规：{link} 时延 {actual}ms > {max_lat}ms, '
            f'调整目标至 {target_lat}ms'
        )
    elif assertion.get('max_loss_pct') is not None:
        max_loss = assertion['max_loss_pct']
        action = ACTION_BOOST_BANDWIDTH
        target = {'type': 'qos', 'link': link, 'metric': 'loss',
                  'current_pct': actual, 'max_pct': max_loss}
        rationale = (
            f'QoS违规：{link} 丢包 {actual}% > {max_loss}%, '
            f'提升带宽/优先级'
        )
    else:
        action = ACTION_ADJUST_QOS
        target = {'type': 'qos', 'link': link}
        rationale = f'QoS违规：{link}, 通用调整'

    return {
        'healing_id': _make_id(),
        'action': action,
        'target': target,
        'deadline_ms': DEFAULT_DEADLINE_MS,
        'rollback_action': ACTION_PROBE_RECHECK,
        'parameters': {
            'method': 'update_tc_netem',
            'current_tc': None,
            'target_tc': None,
        },
        'rationale': rationale,
        'source_deviation': dev,
        'generated_at': time.time(),
    }


def _gen_reachability(dev: Dict, intent: Dict) -> Dict:
    """可达性违反 → 改路径绕开隔离节点."""
    pair = dev.get('pair', ['?', '?'])
    path = dev.get('path', [])
    isolated = dev.get('isolated_in_path', [])

    return {
        'healing_id': _make_id(),
        'action': ACTION_REROUTE,
        'target': {'type': 'path', 'src': pair[0], 'dst': pair[1]},
        'deadline_ms': DEFAULT_DEADLINE_MS,
        'rollback_action': ACTION_RECOVER,
        'parameters': {
            'method': 'install_bypass_flow',
            'avoid_nodes': isolated,
            'new_path': _compute_bypass(path, isolated),
        },
        'rationale': (
            f'可达性违规：{pair[0]}↔{pair[1]} 路径经过隔离节点 {isolated}, '
            f'绕过'
        ),
        'source_deviation': dev,
        'generated_at': time.time(),
    }


def _gen_conflict(dev: Dict, intent: Dict) -> Dict:
    """意图冲突 → 升级或撤销低优先级意图."""
    with_intent = dev.get('with_intent', '?')
    severity = dev.get('severity', 'medium')

    return {
        'healing_id': _make_id(),
        'action': ACTION_SUPERSEDE,
        'target': {'type': 'intent', 'intent_id': with_intent,
                   'strategy': 'priority_or_recency'},
        'deadline_ms': 1000,  # 冲突消解应快速
        'rollback_action': None,
        'parameters': {
            'method': 'call_conflict_resolver',
            'conflict_type': dev.get('type'),
            'severity': severity,
        },
        'rationale': (
            f'意图冲突：与 {with_intent} {dev.get("type", "")}, '
            f'触发自动消解'
        ),
        'source_deviation': dev,
        'generated_at': time.time(),
    }


def _gen_generic(dev: Dict, intent: Dict) -> Dict:
    """通用兜底."""
    return {
        'healing_id': _make_id(),
        'action': 'investigate',
        'target': {'type': 'unknown'},
        'deadline_ms': DEFAULT_DEADLINE_MS,
        'rollback_action': None,
        'parameters': {},
        'rationale': f'未知偏差类型 {dev.get("type")}: {dev.get("message", "?")}',
        'source_deviation': dev,
        'generated_at': time.time(),
    }


# ============================================================
# 辅助
# ============================================================

def _alt_path_around(failed_link: str) -> List[str]:
    """绕开故障链路的备用路径 (简化: 标记为待 BFS 计算)."""
    parts = failed_link.split('-')
    if len(parts) == 2:
        return [parts[0], 's3', parts[1]]  # 简单假设有 s3 中转
    return []


def _compute_bypass(path: List[str], avoid: List[str]) -> List[str]:
    """简单 BFS 绕开节点. 实际应调 conflict_detector._shortest_path."""
    if not path:
        return []
    # 简化: 直接去掉 avoid 节点
    return [n for n in path if n not in avoid]


def _resolve_hosts_safe(intent: Dict, name: str) -> List[str]:
    """从意图解析主体 (宽松版, 不依赖 fetch_actual)."""
    if not intent:
        return []
    for ep in intent.get('semantics', {}).get('endpoints', []):
        if ep.get('name') == name:
            return ep.get('hosts', [])
    return []


# ============================================================
# 批量生成 (用于一次性扫描所有偏差)
# ============================================================

def generate_batch(deviations: List[Dict],
                   intent: Dict = None) -> List[Dict]:
    """对一组偏差生成多个自愈意图.

    返回: List[healing_intent]
    """
    return [generate(d, intent) for d in deviations]