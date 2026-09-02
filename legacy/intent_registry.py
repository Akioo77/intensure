"""意图注册表 - 管理激活意图的增删改查。

意图（Intent）：用户网络意图的结构化表示，由意图翻译智能体产生。
本模块负责存储、检索、持久化意图，并维护按"主体"和"链路"的索引以加速冲突检测。

数据结构：
{
  "intent_id": "string",
  "timestamp": float,
  "user_statement": "natural language",
  "semantics": {"type": "connectivity|isolation|qos", "endpoints": [...]},
  "target_state": {"connectivity": [...], "link_status": [...], "qos": [...]},
  "priority": 1-10,
  "status": "active|superseded|revoked",
  "conflicts": [...]
}

索引：
- by_host: { host_name: set of intent_ids }
- by_link: { link_name: set of intent_ids }
"""
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

# 持久化文件
PERSIST_FILE = '/tmp/intent_registry.json'

# 线程锁
_lock = threading.RLock()

# 主存储
_intents: Dict[str, Dict] = {}
# 索引
_index_by_host: Dict[str, set] = {}   # host_name -> intent_ids
_index_by_link: Dict[str, set] = {}   # link_name -> intent_ids


def _intent_subjects(intent: Dict) -> set:
    """提取意图涉及的所有主机名（用于索引）."""
    subjects = set()
    for ep in intent.get('semantics', {}).get('endpoints', []):
        name = ep.get('name')
        if name:
            subjects.add(name)
        for h in ep.get('hosts', []):
            subjects.add(h)
    # 也从 connectivity 中提取
    for c in intent.get('target_state', {}).get('connectivity', []):
        if 'src' in c:
            subjects.add(c['src'])
        if 'dst' in c:
            subjects.add(c['dst'])
    return subjects


def _intent_links(intent: Dict) -> set:
    """提取意图涉及的所有链路名（用于索引）."""
    links = set()
    for ls in intent.get('target_state', {}).get('link_status', []):
        if 'link' in ls:
            links.add(ls['link'])
    for q in intent.get('target_state', {}).get('qos', []):
        if 'link' in q:
            links.add(q['link'])
    return links


def _rebuild_index():
    """重建索引（一般只在加载时调用）."""
    global _index_by_host, _index_by_link
    _index_by_host = {}
    _index_by_link = {}
    for intent_id, intent in _intents.items():
        if intent.get('status') != 'active':
            continue
        for s in _intent_subjects(intent):
            _index_by_host.setdefault(s, set()).add(intent_id)
        for l in _intent_links(intent):
            _index_by_link.setdefault(l, set()).add(intent_id)


def add(intent: Dict, check_conflicts: bool = True) -> Dict:
    """添加意图到注册表.

    返回: {"intent_id": "...", "conflicts": [...], "status": "active|rejected"}
    """
    with _lock:
        if 'intent_id' not in intent:
            intent['intent_id'] = f'intent-{int(time.time()*1000)}'
        intent.setdefault('timestamp', time.time())
        intent.setdefault('status', 'active')
        intent.setdefault('conflicts', [])
        intent_id = intent['intent_id']

        if check_conflicts:
            import conflict_detector
            topology = _current_topology()
            conflicts = conflict_detector.detect_conflicts(
                intent, _active_intents(), topology)
            intent['conflicts'] = conflicts
        else:
            intent['conflicts'] = []

        _intents[intent_id] = intent
        # 更新索引
        for s in _intent_subjects(intent):
            _index_by_host.setdefault(s, set()).add(intent_id)
        for l in _intent_links(intent):
            _index_by_link.setdefault(l, set()).add(intent_id)
        _save()

        return {
            'intent_id': intent_id,
            'conflicts': intent['conflicts'],
            'status': 'active',
        }


def get(intent_id: str) -> Optional[Dict]:
    """获取意图."""
    with _lock:
        return _intents.get(intent_id)


def list_all(include_inactive: bool = False) -> List[Dict]:
    """列出所有意图."""
    with _lock:
        if include_inactive:
            return list(_intents.values())
        return [i for i in _intents.values() if i.get('status') == 'active']


def _active_intents() -> List[Dict]:
    """返回所有 active 意图（不含已撤销/覆盖的）."""
    return [i for i in _intents.values() if i.get('status') == 'active']


def remove(intent_id: str, reason: str = 'revoked') -> Dict:
    """移除/撤销意图."""
    with _lock:
        if intent_id not in _intents:
            return {'status': 'not_found'}
        intent = _intents[intent_id]
        intent['status'] = reason  # revoked | superseded
        # 从索引移除
        for s in _intent_subjects(intent):
            if s in _index_by_host:
                _index_by_host[s].discard(intent_id)
        for l in _intent_links(intent):
            if l in _index_by_link:
                _index_by_link[l].discard(intent_id)
        _save()
        return {'status': 'ok', 'intent_id': intent_id, 'reason': reason}


def find_by_host(host: str) -> List[Dict]:
    """按主机名查询相关意图."""
    with _lock:
        ids = _index_by_host.get(host, set())
        return [_intents[i] for i in ids if i in _intents
                and _intents[i].get('status') == 'active']


def find_by_link(link: str) -> List[Dict]:
    """按链路查询相关意图."""
    with _lock:
        ids = _index_by_link.get(link, set())
        return [_intents[i] for i in ids if i in _intents
                and _intents[i].get('status') == 'active']


def clear():
    """清空（测试用）."""
    with _lock:
        _intents.clear()
        _index_by_host.clear()
        _index_by_link.clear()
        _save()


def _current_topology() -> Dict:
    """从 state_server 文件或内存读取当前拓扑.

    期望格式: {
      "switches": {"s1": {"ports": [...]}, ...},
      "links": {"s1:s1-eth3": {"peer_dpid": "s2", "peer_port": 3}, ...},
      "hosts": {"h1": {"dpid": "s1", "port": 1}, ...}
    }
    """
    state_file = '/tmp/network_state.json'
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
            return {
                'switches': state.get('switches', {}),
                'links': state.get('links', {}),
                'hosts': state.get('hosts', {}),
            }
        except Exception:
            pass
    return {'switches': {}, 'links': {}, 'hosts': {}}


def _save():
    """持久化到文件."""
    try:
        with open(PERSIST_FILE, 'w') as f:
            json.dump(list(_intents.values()), f, indent=2)
    except Exception as e:
        print(f'[intent_registry] save failed: {e}')


def load(path: str = PERSIST_FILE) -> int:
    """从文件加载，返回加载数."""
    global _intents
    if not os.path.exists(path):
        return 0
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        if isinstance(data, list):
            _intents = {i['intent_id']: i for i in data if 'intent_id' in i}
        elif isinstance(data, dict):
            _intents = data
        _rebuild_index()
        return len(_intents)
    except Exception as e:
        print(f'[intent_registry] load failed: {e}')
        return 0


def stats() -> Dict:
    """统计信息."""
    with _lock:
        active = _active_intents()
        return {
            'total': len(_intents),
            'active': len(active),
            'with_conflicts': sum(1 for i in active if i.get('conflicts')),
            'by_type': {
                'connectivity': sum(1 for i in active
                                    if i.get('semantics', {}).get('type') == 'connectivity'),
                'isolation': sum(1 for i in active
                                 if i.get('semantics', {}).get('type') == 'isolation'),
                'qos': sum(1 for i in active
                           if i.get('semantics', {}).get('type') == 'qos'),
            },
        }


if __name__ == '__main__':
    # 自测
    load()
    print(f'Loaded: {stats()}')