"""意图冲突检测引擎 - 检测新意图与现有意图之间的冲突。

支持 4 种冲突类型:
  1. direct_contradiction  - 同主体反方向（connectivity vs isolation）
  2. resource_competition  - 资源竞争（带宽总和超过链路容量）
  3. latency_contradiction - 时延矛盾（max 延迟互相冲突）
  4. reachability_violation - 可达性违反（连通路径穿过被隔离主体）

每条冲突格式:
  {
    "type": "direct_contradiction",
    "with_intent": "intent-002-isolation",
    "severity": "high" | "medium" | "low",
    "description": "人话描述",
    "details": { ... 原始数据 ... }
  }

输入:
  new_intent: 新提交的意图 dict
  existing_intents: list of existing intent dicts
  topology: {switches, links, hosts} 拓扑信息
"""
from typing import Dict, List, Set, Tuple

# 链路默认容量（Mbps）- Mininet 模拟链路默认
DEFAULT_LINK_CAPACITY_MBPS = 100


# ============================================================
# 工具函数
# ============================================================

def _subject_pairs(intent: Dict) -> Set[Tuple[str, str]]:
    """提取意图涉及的所有主机对（无序对）.

    例如：{("研发", "市场"), ("财务", "研发")}
    """
    pairs = set()
    endpoints = intent.get('semantics', {}).get('endpoints', [])
    # 先把 endpoint 名称收集
    names = []
    for ep in endpoints:
        n = ep.get('name')
        if n:
            names.append(n)

    # 两两配对
    for i, a in enumerate(names):
        for b in names[i+1:]:
            pairs.add(tuple(sorted([a, b])))

    # 也从 connectivity 中提取
    for c in intent.get('target_state', {}).get('connectivity', []):
        if 'src' in c and 'dst' in c:
            pairs.add(tuple(sorted([c['src'], c['dst']])))
    return pairs


def _all_subjects(intent: Dict) -> Set[str]:
    """所有相关主体名（用于索引）."""
    subjects = set()
    for ep in intent.get('semantics', {}).get('endpoints', []):
        n = ep.get('name')
        if n:
            subjects.add(n)
    for c in intent.get('target_state', {}).get('connectivity', []):
        if 'src' in c:
            subjects.add(c['src'])
        if 'dst' in c:
            subjects.add(c['dst'])
    return subjects


def _links(intent: Dict) -> List[Dict]:
    """提取所有 link 约束."""
    out = []
    for ls in intent.get('target_state', {}).get('link_status', []):
        if 'link' in ls:
            out.append({'link': ls['link'], **ls})
    for q in intent.get('target_state', {}).get('qos', []):
        if 'link' in q:
            out.append({'link': q['link'], **q})
    return out


def _intent_type(intent: Dict) -> str:
    """提取意图类型."""
    return intent.get('semantics', {}).get('type', 'unknown')


def _priority(intent: Dict) -> int:
    """提取优先级（数字越小越高）."""
    return intent.get('priority', 5)


# ============================================================
# 检测 1: 直接矛盾（同主体反向）
# ============================================================

def check_direct_contradiction(new: Dict, existing: Dict) -> List[Dict]:
    """同主体对，一个要求连通一个要求隔离 → 直接矛盾."""
    if existing['intent_id'] == new.get('intent_id'):
        return []
    type_new = _intent_type(new)
    type_exi = _intent_type(existing)
    if {type_new, type_exi} != {'connectivity', 'isolation'}:
        return []  # 必须一方连通一方隔离

    # 比较主体对
    pairs_new = _subject_pairs(new)
    pairs_exi = _subject_pairs(existing)
    common = pairs_new & pairs_exi
    if not common:
        return []

    conflicts = []
    # 哪个是 isolation？
    isolation = new if type_new == 'isolation' else existing
    connectivity = new if type_new == 'connectivity' else existing

    for pair in common:
        conflicts.append({
            'type': 'direct_contradiction',
            'with_intent': isolation['intent_id'],
            'severity': 'high',
            'description': (
                f'冲突：{pair[0]}↔{pair[1]} 上，{isolation["intent_id"]} 要求隔离，'
                f'但 {connectivity["intent_id"]} 要求连通'
            ),
            'details': {
                'pair': list(pair),
                'isolation_intent': isolation['intent_id'],
                'connectivity_intent': connectivity['intent_id'],
            },
        })
    return conflicts


# ============================================================
# 检测 2: 资源竞争（带宽总和）
# ============================================================

def check_resource_competition(new: Dict, existing: Dict) -> List[Dict]:
    """QoS 意图对同一链路带宽要求之和 > 容量 → 资源竞争."""
    if existing['intent_id'] == new.get('intent_id'):
        return []
    if _intent_type(new) != 'qos' and _intent_type(existing) != 'qos':
        return []

    # 按链路分组
    new_links = {l['link']: l for l in _links(new)
                 if l.get('min_bandwidth_mbps')}
    exi_links = {l['link']: l for l in _links(existing)
                 if l.get('min_bandwidth_mbps')}

    common_links = set(new_links.keys()) & set(exi_links.keys())
    if not common_links:
        return []

    conflicts = []
    for link in common_links:
        total = new_links[link]['min_bandwidth_mbps'] + exi_links[link]['min_bandwidth_mbps']
        cap = DEFAULT_LINK_CAPACITY_MBPS
        if total > cap:
            conflicts.append({
                'type': 'resource_competition',
                'with_intent': existing['intent_id'],
                'severity': 'high',
                'description': (
                    f'资源冲突：链路 {link} 被两个意图要求 '
                    f'{total}Mbps > 链路容量 {cap}Mbps'
                ),
                'details': {
                    'link': link,
                    'required_mbps': total,
                    'capacity_mbps': cap,
                    'over_mbps': total - cap,
                    'intent_a': new['intent_id'],
                    'intent_b': existing['intent_id'],
                },
            })
    return conflicts


# ============================================================
# 检测 3: 时延矛盾（max 延迟互相冲突）
# ============================================================

def check_latency_contradiction(new: Dict, existing: Dict) -> List[Dict]:
    """两个意图对同一链路提出互相冲突的时延要求.

    约束语义:
      min_latency_ms=X -> 实际时延必须 ≥ X
      max_latency_ms=Y -> 实际时延必须 ≤ Y
    矛盾: X > Y (不可能同时满足)
    """
    if existing['intent_id'] == new.get('intent_id'):
        return []

    new_links = {l['link']: l for l in _links(new)
                 if 'min_latency_ms' in l or 'max_latency_ms' in l}
    exi_links = {l['link']: l for l in _links(existing)
                 if 'min_latency_ms' in l or 'max_latency_ms' in l}

    common_links = set(new_links.keys()) & set(exi_links.keys())
    if not common_links:
        return []

    conflicts = []
    for link in common_links:
        n, e = new_links[link], exi_links[link]
        n_min = n.get('min_latency_ms')
        n_max = n.get('max_latency_ms')
        e_min = e.get('min_latency_ms')
        e_max = e.get('max_latency_ms')

        # 矛盾 1: new.min > existing.max
        if n_min is not None and e_max is not None and n_min > e_max:
            conflicts.append({
                'type': 'latency_contradiction',
                'with_intent': existing['intent_id'],
                'severity': 'medium',
                'description': (
                    f'时延冲突：{link} 上 {new["intent_id"]} 要求时延≥{n_min}ms，'
                    f'但 {existing["intent_id"]} 要求≤{e_max}ms，不可能同时满足'
                ),
                'details': {
                    'link': link,
                    'new_min_ms': n_min,
                    'existing_max_ms': e_max,
                },
            })
        # 矛盾 2: existing.min > new.max
        elif e_min is not None and n_max is not None and e_min > n_max:
            conflicts.append({
                'type': 'latency_contradiction',
                'with_intent': existing['intent_id'],
                'severity': 'medium',
                'description': (
                    f'时延冲突：{link} 上 {existing["intent_id"]} 要求时延≥{e_min}ms，'
                    f'但 {new["intent_id"]} 要求≤{n_max}ms，不可能同时满足'
                ),
                'details': {
                    'link': link,
                    'existing_min_ms': e_min,
                    'new_max_ms': n_max,
                },
            })
        # 矛盾 3: new.max vs existing.max 差异过大 (>5x) 可能一方设计不合理
        elif n_max is not None and e_max is not None:
            ratio = max(n_max, e_max) / max(min(n_max, e_max), 1)
            if ratio >= 5:
                conflicts.append({
                    'type': 'latency_warning',
                    'with_intent': existing['intent_id'],
                    'severity': 'low',
                    'description': (
                        f'时延警告：{link} 上两个意图 max_latency 差异较大 '
                        f'({n_max}ms vs {e_max}ms)'
                    ),
                    'details': {
                        'link': link,
                        'new_max_ms': n_max,
                        'existing_max_ms': e_max,
                    },
                })
    return conflicts


# ============================================================
# 检测 4: 可达性违反（连通路径穿过被隔离主体）
# ============================================================

def check_reachability_violation(new: Dict, existing: Dict, topology: Dict) -> List[Dict]:
    """如果 new 要求 A↔B 连通，但 BFS 路径必须穿过 existing 隔离的主体，则冲突.

    简化实现：用 LLDP 链路作为拓扑，subject 映射到连接的主机。
    """
    if existing['intent_id'] == new.get('intent_id'):
        return []
    if _intent_type(existing) != 'isolation':
        return []
    if _intent_type(new) != 'connectivity':
        return []

    # existing 隔离了哪些 subject
    iso_subjects = _all_subjects(existing)
    if not iso_subjects:
        return []

    # new 要求哪些对连通
    pairs_new = _subject_pairs(new)
    if not pairs_new:
        return []

    # 简化路径检查：拓扑中如果任何被隔离的 subject 在两个端点之间，则冲突
    # 用 BFS 找到所有端点对的最短路径，看是否经过被隔离节点
    graph = _build_graph(topology)
    if not graph:
        return []

    conflicts = []
    # 把 endpoints 映射到拓扑节点（简化：用名字直接当节点）
    for pair in pairs_new:
        a, b = pair
        path = _shortest_path(graph, a, b)
        if path and any(s in path for s in iso_subjects):
            isolated_in_path = [s for s in iso_subjects if s in path]
            conflicts.append({
                'type': 'reachability_violation',
                'with_intent': existing['intent_id'],
                'severity': 'high',
                'description': (
                    f'可达性冲突：{a}↔{b} 的连通路径需经过被 {existing["intent_id"]} '
                    f'隔离的主体 {isolated_in_path}'
                ),
                'details': {
                    'pair': list(pair),
                    'path': path,
                    'isolated_in_path': isolated_in_path,
                },
            })
    return conflicts


def _build_graph(topology: Dict) -> Dict:
    """从 LLDP 链路构建邻接表.

    节点 = 交换机 + 主机名（用 host 的 dpid + name 关联）
    """
    graph = {}

    # 构建 dpid -> switch_name 映射
    dpid_to_sw = {}
    port_name_to_sw = {}
    for sw_name, sw in topology.get('switches', {}).items():
        dpid = sw.get('dpid')
        if dpid:
            dpid_to_sw[dpid] = sw_name
        for p in sw.get('ports', []):
            pname = p.get('name', '')
            if pname:
                port_name_to_sw[pname] = sw_name

    edges = set()

    # 交换机间链路（按 dpid 匹配）
    for key, v in topology.get('links', {}).items():
        try:
            dpid_str, port_str = key.split(':')
            port = int(port_str)
            port_name = f"{dpid_to_sw.get(dpid_str, dpid_str)}-eth{port}"
            peer_dpid = v.get('peer_dpid')
            peer_port = v.get('peer_port')
            peer_name = f"{dpid_to_sw.get(peer_dpid, peer_dpid)}-eth{peer_port}"
            if port_name and peer_name and port_name != peer_name:
                src_node = port_name.rsplit('-eth', 1)[0]
                dst_node = peer_name.rsplit('-eth', 1)[0]
                edges.add((src_node, dst_node))
        except Exception:
            continue

    # 主机挂载（按 host 的 dpid 匹配到交换机）
    for host_name, h in topology.get('hosts', {}).items():
        dpid = h.get('dpid')
        port = h.get('port')
        if dpid and port is not None:
            sw_name = dpid_to_sw.get(dpid)
            if sw_name:
                edges.add((sw_name, host_name))

    # 构建邻接表（去重）
    for a, b in edges:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
    return graph


def _shortest_path(graph: Dict, src: str, dst: str) -> List[str]:
    """BFS 最短路径."""
    if src not in graph or dst not in graph:
        return []
    if src == dst:
        return [src]
    from collections import deque
    visited = {src}
    queue = deque([(src, [src])])
    while queue:
        node, path = queue.popleft()
        for nb in graph.get(node, set()):
            if nb in visited:
                continue
            visited.add(nb)
            new_path = path + [nb]
            if nb == dst:
                return new_path
            queue.append((nb, new_path))
    return []  # 不可达


# ============================================================
# 主入口
# ============================================================

def detect_conflicts(new_intent: Dict,
                     existing_intents: List[Dict],
                     topology: Dict) -> List[Dict]:
    """检测新意图与所有现有意图之间的冲突.

    返回冲突列表（每条都是 dict）
    """
    all_conflicts = []
    for existing in existing_intents:
        if existing.get('intent_id') == new_intent.get('intent_id'):
            continue
        all_conflicts.extend(check_direct_contradiction(new_intent, existing))
        all_conflicts.extend(check_resource_competition(new_intent, existing))
        all_conflicts.extend(check_latency_contradiction(new_intent, existing))
        all_conflicts.extend(check_reachability_violation(new_intent, existing, topology))
    return all_conflicts


def summarize(conflicts: List[Dict]) -> Dict:
    """汇总冲突统计."""
    by_type = {}
    by_severity = {'high': 0, 'medium': 0, 'low': 0}
    for c in conflicts:
        t = c.get('type', 'unknown')
        by_type[t] = by_type.get(t, 0) + 1
        sev = c.get('severity', 'low')
        by_severity[sev] = by_severity.get(sev, 0) + 1
    return {
        'total': len(conflicts),
        'by_type': by_type,
        'by_severity': by_severity,
        'has_high': by_severity['high'] > 0,
    }