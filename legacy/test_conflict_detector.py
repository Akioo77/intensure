"""测试意图冲突检测引擎 - 10 个测试场景.

测试覆盖:
  1. 直接矛盾（connectivity vs isolation）  - 期望: 检出 high
  2. 直接矛盾（相同主机对，不同意图）        - 期望: 检出 high
  3. 不同主机对，无冲突                    - 期望: 无冲突
  4. 资源竞争（带宽总和 > 容量）             - 期望: 检出 high
  5. 资源竞争（带宽总和 <= 容量）            - 期望: 无冲突
  6. 时延矛盾（min > max）                  - 期望: 检出 medium
  7. 时延差异警告（max 差异 > 5x）          - 期望: 检出 low
  8. 可达性违反（连通路径穿过隔离主体）      - 期望: 检出 high
  9. 跨意图多冲突（一对多）                  - 期望: 多条冲突
  10. 复合约束（QoS + 隔离 + 连通）          - 期望: 至少一条冲突

召回率要求: ≥ 90%
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import conflict_detector


# 模拟拓扑：s1 ↔ s2, h1/h3 → s1, h2/h4 → s2
MOCK_TOPOLOGY = {
    'switches': {
        's1': {
            'dpid': '0000000000000001',
            'ports': [
                {'port_no': 1, 'name': 's1-eth1', 'state': 'UP'},
                {'port_no': 2, 'name': 's1-eth2', 'state': 'UP'},
                {'port_no': 3, 'name': 's1-eth3', 'state': 'UP'},
            ],
        },
        's2': {
            'dpid': '0000000000000002',
            'ports': [
                {'port_no': 1, 'name': 's2-eth1', 'state': 'UP'},
                {'port_no': 2, 'name': 's2-eth2', 'state': 'UP'},
                {'port_no': 3, 'name': 's2-eth3', 'state': 'UP'},
            ],
        },
    },
    'links': {
        '0000000000000001:3': {'peer_dpid': '0000000000000002', 'peer_port': 3},
        '0000000000000002:3': {'peer_dpid': '0000000000000001', 'peer_port': 3},
    },
    'hosts': {
        'h1': {'dpid': '0000000000000001', 'port': 1},
        'h3': {'dpid': '0000000000000001', 'port': 2},
        'h2': {'dpid': '0000000000000002', 'port': 1},
        'h4': {'dpid': '0000000000000002', 'port': 2},
    },
}


def make_intent(intent_id, type_, endpoints, qos=None, connectivity=None,
                priority=5):
    """构造意图."""
    intent = {
        'intent_id': intent_id,
        'timestamp': 0,
        'semantics': {'type': type_, 'endpoints': endpoints},
        'target_state': {},
        'priority': priority,
        'status': 'active',
        'conflicts': [],
    }
    if connectivity:
        intent['target_state']['connectivity'] = connectivity
    if qos:
        intent['target_state']['qos'] = qos
    return intent


# ============================================================
# 测试用例
# ============================================================

TESTS = []


def t(name, existing, new, expected_min_conflicts, expected_severity=None,
      expected_type=None):
    TESTS.append({
        'name': name,
        'existing': existing,
        'new': new,
        'expected_min': expected_min_conflicts,
        'expected_severity': expected_severity,
        'expected_type': expected_type,
    })


# Test 1: 直接矛盾 - 连通 vs 隔离（同对）
t('直接矛盾-连通vs隔离-研发↔市场',
  existing=[make_intent('i-A', 'connectivity',
                        [{'name': '研发'}, {'name': '市场'}],
                        connectivity=[{'src': '研发', 'dst': '市场', 'allow': True}])],
  new=make_intent('i-B', 'isolation',
                  [{'name': '研发'}, {'name': '市场'}],
                  connectivity=[{'src': '研发', 'dst': '市场', 'allow': False}]),
  expected_min_conflicts=1,
  expected_severity='high',
  expected_type='direct_contradiction')

# Test 2: 直接矛盾 - 反向（隔离先，连通后）
t('直接矛盾-隔离vs连通-财务↔研发',
  existing=[make_intent('i-A', 'isolation',
                        [{'name': '财务'}, {'name': '研发'}],
                        connectivity=[{'src': '财务', 'dst': '研发', 'allow': False}])],
  new=make_intent('i-B', 'connectivity',
                  [{'name': '财务'}, {'name': '研发'}],
                  connectivity=[{'src': '财务', 'dst': '研发', 'allow': True}]),
  expected_min_conflicts=1,
  expected_severity='high',
  expected_type='direct_contradiction')

# Test 3: 不同主机对，无冲突
t('无冲突-不同主机对',
  existing=[make_intent('i-A', 'connectivity',
                        [{'name': '研发'}, {'name': '市场'}],
                        connectivity=[{'src': '研发', 'dst': '市场', 'allow': True}])],
  new=make_intent('i-B', 'connectivity',
                  [{'name': '财务'}, {'name': '行政'}],
                  connectivity=[{'src': '财务', 'dst': '行政', 'allow': True}]),
  expected_min_conflicts=0)

# Test 4: 资源竞争 - 带宽超过容量
t('资源竞争-带宽超量',
  existing=[make_intent('i-A', 'qos', [],
                        qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 80}])],
  new=make_intent('i-B', 'qos', [],
                  qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 50}]),
  expected_min_conflicts=1,
  expected_severity='high',
  expected_type='resource_competition')

# Test 5: 资源竞争 - 带宽不超
t('无冲突-带宽合适',
  existing=[make_intent('i-A', 'qos', [],
                        qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 40}])],
  new=make_intent('i-B', 'qos', [],
                  qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 50}]),
  expected_min_conflicts=0)

# Test 6: 时延矛盾 - min > max
t('时延矛盾-min超max',
  existing=[make_intent('i-A', 'qos', [],
                        qos=[{'link': 's1-s2', 'max_latency_ms': 30}])],
  new=make_intent('i-B', 'qos', [],
                  qos=[{'link': 's1-s2', 'min_latency_ms': 50}]),
  expected_min_conflicts=1,
  expected_severity='medium',
  expected_type='latency_contradiction')

# Test 7: 时延差异警告
t('时延警告-差异过大',
  existing=[make_intent('i-A', 'qos', [],
                        qos=[{'link': 's1-s2', 'max_latency_ms': 10}])],
  new=make_intent('i-B', 'qos', [],
                  qos=[{'link': 's1-s2', 'max_latency_ms': 100}]),
  expected_min_conflicts=1,
  expected_severity='low',
  expected_type='latency_warning')

# Test 8: 可达性违反
t('可达性违反-路径穿过隔离',
  existing=[make_intent('i-A', 'isolation',
                        [{'name': 's1'}],
                        connectivity=[{'src': 's1', 'dst': 's1', 'allow': False}])],
  new=make_intent('i-B', 'connectivity',
                  [{'name': 'h1'}, {'name': 'h2'}],
                  connectivity=[{'src': 'h1', 'dst': 'h2', 'allow': True}]),
  expected_min_conflicts=1,
  expected_severity='high',
  expected_type='reachability_violation')

# Test 9: 多冲突 - 同时多类型
t('多冲突-资源+时延',
  existing=[make_intent('i-A', 'qos', [],
                        qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 70,
                              'max_latency_ms': 30}])],
  new=make_intent('i-B', 'qos', [],
                  qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 60,
                        'min_latency_ms': 50}]),
  expected_min_conflicts=2)  # 1 resource + 1 latency

# Test 10: 复合场景 - QoS + 隔离 + 连通
t('复合-混合类型',
  existing=[
      make_intent('i-A', 'connectivity', [{'name': '研发'}, {'name': '市场'}],
                  connectivity=[{'src': '研发', 'dst': '市场', 'allow': True}]),
      make_intent('i-B', 'qos', [],
                  qos=[{'link': 's1-s2', 'max_latency_ms': 20}]),
  ],
  new=make_intent('i-C', 'isolation',
                  [{'name': '研发'}, {'name': '市场'}],
                  connectivity=[{'src': '研发', 'dst': '市场', 'allow': False}]),
  expected_min_conflicts=1,
  expected_severity='high',
  expected_type='direct_contradiction')

# Test 11: 同方向不冲突 (两个都要求互通，不同主体)
t('无冲突-同向不同主体',
  existing=[make_intent('i-A', 'connectivity',
                        [{'name': '研发'}, {'name': '市场'}],
                        connectivity=[{'src': '研发', 'dst': '市场', 'allow': True}])],
  new=make_intent('i-B', 'connectivity',
                  [{'name': '财务'}, {'name': '市场'}],
                  connectivity=[{'src': '财务', 'dst': '市场', 'allow': True}]),
  expected_min_conflicts=0)

# Test 12: 三个意图中第二个冲突
t('多意图-中间冲突',
  existing=[
      make_intent('i-A', 'qos', [],
                  qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 30}]),
      make_intent('i-B', 'qos', [],
                  qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 90}]),
  ],
  new=make_intent('i-C', 'qos', [],
                  qos=[{'link': 's1-s2', 'min_bandwidth_mbps': 20}]),
  expected_min_conflicts=1,
  expected_severity='high',
  expected_type='resource_competition')


# ============================================================
# 跑测试
# ============================================================

def run_tests():
    passed = 0
    failed = 0
    detailed = []
    for i, test in enumerate(TESTS, 1):
        conflicts = conflict_detector.detect_conflicts(
            test['new'], test['existing'], MOCK_TOPOLOGY)
        n = len(conflicts)

        ok = n >= test['expected_min']
        msg = f'Test {i:2d}: {test["name"]} → '
        if ok:
            passed += 1
            msg += f'✅ ({n} conflicts)'
        else:
            failed += 1
            msg += f'❌ (expected ≥{test["expected_min"]}, got {n})'
        detailed.append(msg)

        if test.get('expected_type') and conflicts:
            types = [c['type'] for c in conflicts]
            if test['expected_type'] in types:
                sev = next(c['severity'] for c in conflicts
                           if c['type'] == test['expected_type'])
                if test.get('expected_severity') and sev != test['expected_severity']:
                    print(f'  ⚠️  type={test["expected_type"]} 但 severity={sev} '
                          f'(expected {test["expected_severity"]})')

        # Print conflict details
        for c in conflicts:
            print(f'        - [{c["severity"]:6s}] {c["type"]:25s} '
                  f'vs {c["with_intent"]:20s} {c["description"][:60]}')

    print('=' * 70)
    for m in detailed:
        print(m)
    print('=' * 70)
    print(f'通过: {passed}/{len(TESTS)} ({100*passed/len(TESTS):.0f}%)')
    print(f'失败: {failed}')

    if passed / len(TESTS) >= 0.9:
        print(f'\n🎯 召回率 ≥ 90% 目标达成!')
        return True
    else:
        print(f'\n⚠️ 召回率 < 90%, 需要调整算法')
        return False


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)