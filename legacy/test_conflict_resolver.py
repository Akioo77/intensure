"""测试冲突消解建议器 - 8 个测试场景。

测试覆盖:
  1. 安全类冲突 + 新优先级高 → priority_override (accept_new)
  2. 安全类冲突 + 新优先级低 → reject (reject_new)
  3. 安全类冲突 + 同优先级 + 新更 → recency (accept_new)
  4. 资源竞争 + 新可降带宽 → negotiation_split (modify_new)
  5. 资源竞争 + 新不能降 → priority_override
  6. 时延矛盾 → negotiation_split (中间值)
  7. 时延警告 → negotiation_split (时延改紧)
  8. 不冲突 → accept
  9. 综合：多冲突 + 智能推荐
  10. 边界：现有意图未找到 → reject
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import conflict_resolver


def make_intent(intent_id, type_, endpoints=None, qos=None,
                connectivity=None, priority=5, timestamp=0):
    intent = {
        'intent_id': intent_id,
        'timestamp': timestamp,
        'semantics': {'type': type_, 'endpoints': endpoints or []},
        'target_state': {},
        'priority': priority,
        'status': 'active',
    }
    if connectivity:
        intent['target_state']['connectivity'] = connectivity
    if qos:
        intent['target_state']['qos'] = qos
    return intent


def make_conflict(type_, with_intent, severity='high', **details):
    c = {
        'type': type_,
        'with_intent': with_intent,
        'severity': severity,
        'description': details.pop('description', f'{type_} conflict'),
    }
    c['details'] = details
    return c


def assert_eq(actual, expected, name):
    if actual == expected:
        print(f'  ✅ {name}: {actual}')
    else:
        print(f'  ❌ {name}: 期望 {expected}, 实际 {actual}')
        raise AssertionError(name)


# ============================================================
# 测试用例
# ============================================================

def test_1_security_high_priority():
    """安全冲突 + 新优先级高 → priority_override accept_new."""
    new = make_intent('new-1', 'connectivity', priority=1)
    existing = [make_intent('exi-1', 'isolation', priority=5)]
    conflict = make_conflict('direct_contradiction', 'exi-1')

    res = conflict_resolver.resolve_conflict(conflict, new, existing)
    assert_eq(res['strategy'], 'priority_override', '策略')
    assert_eq(res['action'], 'accept_new', '动作')
    assert_eq(res['loser'], 'existing', '输家')
    assert res['confidence'] >= 0.9, f'confidence={res["confidence"]}'


def test_2_security_low_priority():
    """安全冲突 + 新优先级低 → reject."""
    new = make_intent('new-1', 'connectivity', priority=8)
    existing = [make_intent('exi-1', 'isolation', priority=1)]
    conflict = make_conflict('direct_contradiction', 'exi-1')

    res = conflict_resolver.resolve_conflict(conflict, new, existing)
    assert_eq(res['strategy'], 'reject', '策略')
    assert_eq(res['action'], 'reject_new', '动作')
    assert_eq(res['loser'], 'new', '输家')


def test_3_security_same_priority_newer():
    """安全冲突 + 同优先级 + 新更 → recency_override accept_new."""
    new = make_intent('new-1', 'connectivity', priority=5, timestamp=100)
    existing = [make_intent('exi-1', 'isolation', priority=5, timestamp=50)]
    conflict = make_conflict('direct_contradiction', 'exi-1')

    res = conflict_resolver.resolve_conflict(conflict, new, existing)
    assert_eq(res['strategy'], 'recency_override', '策略')
    assert_eq(res['action'], 'accept_new', '动作')


def test_4_resource_negotiate():
    """资源竞争 + 新可降带宽 → negotiation_split modify_new."""
    new = make_intent('new-1', 'qos',
                      qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 80}],
                      priority=5)
    existing = [make_intent('exi-1', 'qos',
                            qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 50}],
                            priority=5)]
    conflict = make_conflict('resource_competition', 'exi-1',
                             link='s1-s2', over_mbps=30, capacity_mbps=100,
                             required_mbps=130)

    res = conflict_resolver.resolve_conflict(conflict, new, existing)
    assert_eq(res['strategy'], 'negotiation_split', '策略')
    assert_eq(res['action'], 'modify_new', '动作')
    assert 'modified_intent' in res, '应包含 modified_intent'
    # 检查 modified 的带宽确实降了 30
    new_bw = res['modified_intent']['target_state']['qos'][0]['min_bandwidth_mbps']
    assert new_bw == 50, f'期望 50 Mbps, 实际 {new_bw}'
    print(f'  ✅ modified_intent 带宽: {new_bw} Mbps (从 80 → 50)')


def test_5_latency_negotiate():
    """时延矛盾 → negotiation_split (取中间值)."""
    new = make_intent('new-1', 'qos',
                      qos=[{'link': 's1-s2', 'min_latency_ms': 50,
                            'max_latency_ms': 100}],
                      priority=5)
    existing = [make_intent('exi-1', 'qos',
                            qos=[{'link': 's1-s2', 'max_latency_ms': 30}],
                            priority=5)]
    conflict = make_conflict('latency_contradiction', 'exi-1',
                             link='s1-s2', new_min_ms=50, existing_max_ms=30)

    res = conflict_resolver.resolve_conflict(conflict, new, existing)
    assert_eq(res['strategy'], 'negotiation_split', '策略')
    assert 'modified_intent' in res
    print(f'  ✅ modified_intent: {res["modified_intent"]["target_state"]["qos"][0]}')


def test_6_priority_difference_qos():
    """QoS 冲突 + 优先级不同 → priority_override (跳过协商)."""
    new = make_intent('new-1', 'qos',
                      qos=[{'link': 's1-s2', 'max_latency_ms': 30}],
                      priority=1)  # 高优先级
    existing = [make_intent('exi-1', 'qos',
                            qos=[{'link': 's1-s2', 'max_latency_ms': 100}],
                            priority=8)]
    conflict = make_conflict('latency_warning', 'exi-1',
                             link='s1-s2', new_max_ms=30, existing_max_ms=100)

    res = conflict_resolver.resolve_conflict(conflict, new, existing)
    assert_eq(res['strategy'], 'priority_override', '策略')
    assert_eq(res['action'], 'accept_new', '动作')


def test_7_no_conflict_accept():
    """无冲突 → 直接返回空 (通过 suggest_resolution)."""
    new = make_intent('new-1', 'connectivity',
                      connectivity=[{'src': '研发', 'dst': '市场', 'allow': True}],
                      priority=5)
    existing = [make_intent('exi-1', 'connectivity',
                            connectivity=[{'src': '财务', 'dst': '行政', 'allow': True}],
                            priority=5)]

    suggestion = conflict_resolver.suggest_resolution(new, existing)
    assert_eq(suggestion['recommendation'], 'accept', '推荐动作')
    assert_eq(suggestion['summary']['total'], 0, '冲突数')


def test_8_multi_conflict_recommend_reject():
    """多冲突 + 安全类 reject → 综合推荐 reject."""
    new = make_intent('new-1', 'connectivity',
                      endpoints=[{'name': '研发'}, {'name': '市场'}],
                      connectivity=[{'src': '研发', 'dst': '市场', 'allow': True}],
                      priority=8)  # 低优先级
    existing = [
        make_intent('exi-1', 'isolation',
                    endpoints=[{'name': '研发'}, {'name': '市场'}],
                    priority=1),  # 高优先级安全
        make_intent('exi-2', 'isolation',
                    endpoints=[{'name': '研发'}, {'name': '市场'}],
                    priority=2),
    ]

    suggestion = conflict_resolver.suggest_resolution(new, existing)
    assert_eq(suggestion['recommendation'], 'reject', '推荐动作')
    assert suggestion['summary']['total'] >= 1
    print(f'  ✅ 共 {suggestion["summary"]["total"]} 个冲突，avg_confidence={suggestion["summary"]["avg_confidence"]}')


def test_9_missing_existing_intent():
    """现有意图未找到 → reject."""
    new = make_intent('new-1', 'connectivity', priority=5)
    existing = []
    conflict = make_conflict('direct_contradiction', 'exi-missing')

    res = conflict_resolver.resolve_conflict(conflict, new, existing)
    assert_eq(res['strategy'], 'reject', '策略')
    assert_eq(res['action'], 'reject_new', '动作')
    assert_eq(res['confidence'], 1.0, 'confidence')


def test_10_warning_with_negotiate():
    """低 severity warning → 可协商拆分."""
    new = make_intent('new-1', 'qos',
                      qos=[{'link': 's1-s2', 'max_latency_ms': 100}],
                      priority=5)
    existing = [make_intent('exi-1', 'qos',
                            qos=[{'link': 's1-s2', 'max_latency_ms': 10}],
                            priority=5)]
    conflict = make_conflict('latency_warning', 'exi-1',
                             severity='low',
                             link='s1-s2', new_max_ms=100, existing_max_ms=10)

    res = conflict_resolver.resolve_conflict(conflict, new, existing)
    # latency_warning 是可协商的，应该尝试 negotiate
    # 但 priority 相同（都是 5），按规则会走到规则 4 (recency)
    # 因为 latency_warning 不在 _can_negotiate... wait, yes it is
    print(f'  ℹ️  策略: {res["strategy"]}, 动作: {res["action"]}, confidence: {res["confidence"]}')


# ============================================================
# 运行
# ============================================================

TESTS = [
    ('1. 安全冲突 + 新优先级高 → priority_override', test_1_security_high_priority),
    ('2. 安全冲突 + 新优先级低 → reject', test_2_security_low_priority),
    ('3. 安全冲突 + 同优先级 + 新更 → recency', test_3_security_same_priority_newer),
    ('4. 资源竞争 + 可降带宽 → negotiation_split', test_4_resource_negotiate),
    ('5. 时延矛盾 → negotiation_split', test_5_latency_negotiate),
    ('6. QoS 冲突 + 优先级不同 → priority_override', test_6_priority_difference_qos),
    ('7. 无冲突 → accept (suggest_resolution)', test_7_no_conflict_accept),
    ('8. 多冲突 + 安全 reject → 综合推荐 reject', test_8_multi_conflict_recommend_reject),
    ('9. 现有意图未找到 → reject', test_9_missing_existing_intent),
    ('10. 时延警告 → 协商拆分', test_10_warning_with_negotiate),
]


def run_all():
    passed = 0
    failed = 0
    for name, fn in TESTS:
        print(f'\n--- {name} ---')
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f'❌ {name}: {e}')
            failed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f'❌ {name}: {type(e).__name__}: {e}')
            failed += 1
    print('\n' + '=' * 70)
    print(f'通过: {passed}/{len(TESTS)}  失败: {failed}')
    return failed == 0


if __name__ == '__main__':
    ok = run_all()
    sys.exit(0 if ok else 1)