"""意图冲突自动消解建议器 - 给定冲突列表，生成消解策略建议。

4 种消解策略 (按优先级):
  1. priority_override   - 高优先级覆盖低优先级
  2. recency_override    - 最新意图覆盖最旧
  3. negotiation_split   - 双方协商让步（修改参数）
  4. reject              - 严重冲突无法消解，拒绝新意图

输入:
  conflict: 单条冲突 dict
  new_intent: 新提交的意图
  existing_intents: 当前所有意图列表

输出:
  Resolution {
    'strategy': 'priority_override' | 'recency_override' | 'negotiation_split' | 'reject',
    'action': 'accept_new' | 'accept_existing' | 'modify_new' | 'reject_new',
    'loser': 'new' | 'existing',
    'confidence': 0.0-1.0,
    'rationale': '人话解释',
    'modified_intent': {...},  # 仅 negotiation_split 时存在
    'next_step': '人工确认' | '自动应用',
  }

策略选择逻辑:
  ┌─ 安全类冲突（direct_contradiction + high）？
  │   ├─ 新优先级更高 → priority_override (accept_new)
  │   └─ 新优先级更低 → reject (reject_new)
  ├─ 可协商冲突（resource/latency/reachability）？
  │   ├─ 能生成 modified_intent → negotiation_split
  │   └─ 否则 → 优先级/时序
  └─ 其他（warning 级别）→ 优先级/时序覆盖

消解决策可被人工覆盖 (POST /api/conflicts/resolve)
"""
import time
from typing import Dict, List, Optional

# 策略常量
STRATEGY_PRIORITY = 'priority_override'
STRATEGY_RECENCY = 'recency_override'
STRATEGY_NEGOTIATE = 'negotiation_split'
STRATEGY_REJECT = 'reject'


# ============================================================
# 辅助函数
# ============================================================

def _priority(intent: Dict) -> int:
    """优先级（数字越小越高）."""
    return intent.get('priority', 5)


def _is_newer(a: Dict, b: Dict) -> bool:
    return a.get('timestamp', 0) > b.get('timestamp', 0)


def _can_negotiate(conflict: Dict) -> bool:
    """冲突是否可协商拆分.

    可协商:
      - resource_competition - 可调带宽
      - latency_contradiction - 可改时延限值
      - latency_warning - 改时延限值
      - reachability_violation - 可改路径

    不可协商:
      - direct_contradiction - 主体对方向对立，必须一方妥协
    """
    ct = conflict.get('type', '')
    return ct in ('resource_competition', 'latency_contradiction',
                  'latency_warning', 'reachability_violation')


def _is_security_critical(conflict: Dict) -> bool:
    """是否为不可调和的安全/隔离类冲突.

    direct_contradiction + severity=high 通常意味着安全策略 vs 业务需求
    """
    return (conflict.get('type') == 'direct_contradiction'
            and conflict.get('severity') == 'high')


# ============================================================
# 协商拆分：尝试生成 modified_intent
# ============================================================

def _try_negotiate(new_intent: Dict, existing: Dict,
                   conflict: Dict) -> Optional[Dict]:
    """尝试生成修改后的新意图（协商拆分）.

    各类型协商策略:
      - resource_competition: 降带宽到不冲突值
      - latency_contradiction: 时延改为中间值
      - latency_warning: 时延取较紧约束
      - reachability_violation: 当前简化版不支持（需 BFS 重算路径）
    """
    ct = conflict.get('type', '')
    modified = {
        'intent_id': new_intent.get('intent_id', 'modified'),
        'semantics': {**new_intent.get('semantics', {})},
        'target_state': {**new_intent.get('target_state', {})},
        'priority': new_intent.get('priority', 5),
        'status': 'active',
        'user_statement': new_intent.get('user_statement', '') + ' [协商修改]',
    }
    if 'timestamp' in new_intent:
        modified['timestamp'] = new_intent['timestamp']

    details = conflict.get('details', {})
    link = details.get('link')

    if ct == 'resource_competition':
        over = details.get('over_mbps', 0)
        if not (link and over > 0):
            return None
        # 新意图带宽降 over_mbps, 让总和 = 容量
        new_qos = []
        changed = False
        for q in modified['target_state'].get('qos', []):
            if q.get('link') == link and 'min_bandwidth_mbps' in q:
                new_min = max(1, q['min_bandwidth_mbps'] - over)
                new_qos.append({**q, 'min_bandwidth_mbps': new_min})
                changed = True
            else:
                new_qos.append(q)
        if not changed:
            return None
        modified['target_state']['qos'] = new_qos
        return modified

    elif ct in ('latency_contradiction', 'latency_warning'):
        n_min = details.get('new_min_ms')
        e_max = details.get('existing_max_ms')
        n_max = details.get('new_max_ms')
        e_min = details.get('existing_min_ms')

        target_ms = None
        if n_min and e_max:
            target_ms = max(e_max, (n_min + e_max) // 2 - 1)
        elif e_min and n_max:
            target_ms = min(n_max, (e_min + n_max) // 2 + 1)

        if not (link and target_ms):
            return None
        new_qos = []
        changed = False
        for q in modified['target_state'].get('qos', []):
            if q.get('link') == link:
                # 同时设置 max 和 min，确保落在合理区间
                new_q = {**q}
                if target_ms is not None:
                    new_q['max_latency_ms'] = target_ms
                    new_q['min_latency_ms'] = target_ms
                new_qos.append(new_q)
                changed = True
            else:
                new_qos.append(q)
        if not changed:
            return None
        modified['target_state']['qos'] = new_qos
        return modified

    elif ct == 'reachability_violation':
        # 简化版暂不自动重算路径，返回 None 走优先级/时序
        return None

    return None


# ============================================================
# 主入口：单条冲突消解
# ============================================================

def resolve_conflict(conflict: Dict,
                     new_intent: Dict,
                     existing_intents: List[Dict]) -> Dict:
    """对单条冲突生成消解建议.

    返回 Resolution dict
    """
    with_intent_id = conflict.get('with_intent')
    existing = next((i for i in existing_intents
                     if i.get('intent_id') == with_intent_id), None)
    if not existing:
        return {
            'strategy': STRATEGY_REJECT,
            'action': 'reject_new',
            'loser': 'new',
            'confidence': 1.0,
            'rationale': f'关联意图 {with_intent_id} 未找到, 无法消解',
            'next_step': '人工确认',
        }

    severity = conflict.get('severity', 'medium')
    new_pri = _priority(new_intent)
    exi_pri = _priority(existing)

    # ============ 规则 1: 安全类不可调和 → 优先级覆盖或拒绝 ============
    if _is_security_critical(conflict):
        if new_pri < exi_pri:
            return {
                'strategy': STRATEGY_PRIORITY,
                'action': 'accept_new',
                'loser': 'existing',
                'confidence': 0.92,
                'rationale': (
                    f'安全类冲突但新意图优先级更高 '
                    f'({new_pri} < {exi_pri})，覆盖现有意图'
                ),
                'next_step': '人工确认',
                'loser_intent_id': with_intent_id,
            }
        # 优先级相同 → 检查时序
        if new_pri == exi_pri and _is_newer(new_intent, existing):
            return {
                'strategy': STRATEGY_RECENCY,
                'action': 'accept_new',
                'loser': 'existing',
                'confidence': 0.68,
                'rationale': (
                    '安全类冲突且优先级相同，但新意图时间更新'
                ),
                'next_step': '人工确认',
                'loser_intent_id': with_intent_id,
            }
        return {
            'strategy': STRATEGY_REJECT,
            'action': 'reject_new',
            'loser': 'new',
            'confidence': 0.95,
            'rationale': (
                f'与现有安全意图冲突，新意图优先级 '
                f'({new_pri}) 不高于现有 ({exi_pri})，拒绝接受'
            ),
            'next_step': '自动拒绝',
            'loser_intent_id': new_intent.get('intent_id', 'new'),
        }

    # ============ 规则 2: 可协商冲突 → 协商拆分 ============
    if _can_negotiate(conflict):
        modified = _try_negotiate(new_intent, existing, conflict)
        if modified:
            return {
                'strategy': STRATEGY_NEGOTIATE,
                'action': 'modify_new',
                'loser': 'new',
                'confidence': 0.72,
                'rationale': (
                    f'冲突可协商拆分: {conflict.get("description", "")[:60]}... '
                    f'建议修改新意图参数'
                ),
                'next_step': '人工确认',
                'modified_intent': modified,
            }

    # ============ 规则 3: 优先级不同时按优先级 ============
    if new_pri != exi_pri:
        if new_pri < exi_pri:
            return {
                'strategy': STRATEGY_PRIORITY,
                'action': 'accept_new',
                'loser': 'existing',
                'confidence': 0.85,
                'rationale': (
                    f'新意图优先级 {new_pri} 高于现有 {exi_pri}，覆盖现有'
                ),
                'next_step': '人工确认',
                'loser_intent_id': with_intent_id,
            }
        return {
            'strategy': STRATEGY_PRIORITY,
            'action': 'reject_new',
            'loser': 'new',
            'confidence': 0.85,
            'rationale': (
                f'新意图优先级 {new_pri} 低于现有 {exi_pri}，拒绝接受'
            ),
            'next_step': '人工确认',
            'loser_intent_id': new_intent.get('intent_id', 'new'),
        }

    # ============ 规则 4: 优先级相同 → 用时序 ============
    if _is_newer(new_intent, existing):
        return {
            'strategy': STRATEGY_RECENCY,
            'action': 'accept_new',
            'loser': 'existing',
            'confidence': 0.65,
            'rationale': '优先级相同，新意图时间更新，按最新生效',
            'next_step': '人工确认',
            'loser_intent_id': with_intent_id,
        }
    return {
        'strategy': STRATEGY_RECENCY,
        'action': 'reject_new',
        'loser': 'new',
        'confidence': 0.65,
        'rationale': '优先级相同，现有意图时间更新，保留现有',
        'next_step': '人工确认',
        'loser_intent_id': new_intent.get('intent_id', 'new'),
    }


# ============================================================
# 批量消解
# ============================================================

def resolve_all(conflicts: List[Dict],
                new_intent: Dict,
                existing_intents: List[Dict]) -> List[Dict]:
    """对所有冲突生成消解建议."""
    return [resolve_conflict(c, new_intent, existing_intents)
            for c in conflicts]


def suggest_resolution(new_intent: Dict,
                       existing_intents: List[Dict]) -> Dict:
    """对一个新意图生成完整消解建议.

    自动调用 conflict_detector.detect_conflicts + resolve_all.

    返回: {
      'conflicts': [...],     # 检测到的冲突
      'resolutions': [...],   # 每个冲突对应的消解建议
      'recommendation': 'accept' | 'modify' | 'reject',
      'summary': {...},
    }
    """
    import conflict_detector

    topology = _current_topology()
    conflicts = conflict_detector.detect_conflicts(
        new_intent, existing_intents, topology)
    resolutions = resolve_all(conflicts, new_intent, existing_intents)

    # 综合判断：是否应该接受新意图
    if not resolutions:
        recommendation = 'accept'
    else:
        # 如果所有 resolution 都是 accept_new 或 modify_new → 接受（含修改）
        # 如果任一是 reject_new 且是安全类 → 拒绝
        has_reject = any(r['action'] == 'reject_new'
                         and r['confidence'] >= 0.9
                         for r in resolutions)
        has_modify = any(r['action'] == 'modify_new' for r in resolutions)
        if has_reject:
            recommendation = 'reject'
        elif has_modify:
            recommendation = 'modify'
        else:
            # accept_new 多 → accept
            accept_n = sum(1 for r in resolutions if r['action'] == 'accept_new')
            reject_n = sum(1 for r in resolutions if r['action'] == 'reject_new')
            recommendation = 'accept' if accept_n >= reject_n else 'reject'

    return {
        'conflicts': conflicts,
        'resolutions': resolutions,
        'recommendation': recommendation,
        'summary': summarize_resolutions(resolutions),
    }


def summarize_resolutions(resolutions: List[Dict]) -> Dict:
    """汇总建议统计."""
    by_strategy = {}
    by_action = {}
    avg_confidence = 0
    for r in resolutions:
        s = r.get('strategy', 'unknown')
        a = r.get('action', 'unknown')
        by_strategy[s] = by_strategy.get(s, 0) + 1
        by_action[a] = by_action.get(a, 0) + 1
        avg_confidence += r.get('confidence', 0)
    n = max(1, len(resolutions))
    return {
        'total': len(resolutions),
        'by_strategy': by_strategy,
        'by_action': by_action,
        'avg_confidence': round(avg_confidence / n, 2),
    }


def _current_topology() -> Dict:
    """从 state_file 读取当前拓扑."""
    import json
    import os
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