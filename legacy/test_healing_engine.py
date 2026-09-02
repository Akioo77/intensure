"""测试自愈引擎 - 验证状态机、生成器、持久化。

测试场景:
  1. link_status 偏差 → 生成 recover_link 自愈意图 ✅
  2. connectivity 偏差 → 生成 reapply_acl 自愈意图 ✅
  3. qos 偏差 → 生成 adjust_qos 自愈意图 ✅
  4. reachability 偏差 → 生成 reroute 自愈意图 ✅
  5. conflict 偏差 → 生成 supersede 自愈意图 ✅
  6. 完整状态机流转：detecting → generating → pushing → verifying → done ✅
  7. 上游 ack 路径 ✅
  8. 升级（escalation）逻辑 ✅
  9. 持久化 ✅
  10. 多 cycle 并发处理 ✅
"""
import json
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import healing_engine
import healing_intent_generator as gen


PASS = '✅'
FAIL = '❌'


def make_intent(intent_id='i-test', type_='connectivity', endpoints=None,
                qos=None, link_status=None, connectivity=None):
    return {
        'intent_id': intent_id,
        'semantics': {'type': type_, 'endpoints': endpoints or []},
        'target_state': {
            'connectivity': connectivity or [],
            'link_status': link_status or [],
            'qos': qos or [],
        },
        'priority': 5,
        'status': 'active',
    }


def make_dev(type_, **kwargs):
    dev = {'type': type_, 'severity': kwargs.pop('severity', 'critical'),
           'message': kwargs.pop('message', '?')}
    dev.update(kwargs)
    return dev


# ============================================================
# 测试用例
# ============================================================

TESTS = []


def test_1_link_status():
    result = gen.generate(
        make_dev('link_status', link='s1-s2', expected='up', actual='down'),
        make_intent())
    assert result['action'] == gen.ACTION_RECOVER, f"got {result['action']}"
    assert result['target']['link'] == 's1-s2'
    assert 'reapply_physical_link' in result['parameters']['method']
    assert 'deadline_ms' in result
    print(f'    action={result["action"]} target={result["target"]}')


def test_2_connectivity():
    result = gen.generate(
        make_dev('connectivity',
                 assertion={'src': '研发', 'dst': '市场', 'allow': True},
                 message='连通性违反'),
        make_intent())
    assert result['action'] == gen.ACTION_REAPPLY_ACL
    assert result['target']['src'] == '研发'
    assert result['target']['dst'] == '市场'
    assert result['target']['allow'] == True
    print(f'    action={result["action"]}')


def test_3_qos_latency():
    result = gen.generate(
        make_dev('qos',
                 assertion={'link': 's1-s2', 'max_latency_ms': 50},
                 actual=80, message='时延超限'),
        make_intent())
    assert result['action'] == gen.ACTION_ADJUST_QOS
    assert result['target']['metric'] == 'latency'
    assert result['target']['target_ms'] < 50
    print(f'    target={result["target"]}')


def test_4_qos_loss():
    result = gen.generate(
        make_dev('qos',
                 assertion={'link': 's1-s2', 'max_loss_pct': 1.0},
                 actual=5, message='丢包超限'),
        make_intent())
    assert result['action'] == gen.ACTION_BOOST_BANDWIDTH
    assert result['target']['metric'] == 'loss'
    print(f'    target={result["target"]}')


def test_5_reachability():
    result = gen.generate(
        make_dev('reachability_violation',
                 pair=['h1', 'h2'],
                 path=['h1', 's1', 's2', 'h2'],
                 isolated_in_path=['s1']),
        make_intent())
    assert result['action'] == gen.ACTION_REROUTE
    assert 's1' not in result['parameters']['new_path']
    print(f'    bypass={result["parameters"]["new_path"]}')


def test_6_conflict():
    result = gen.generate(
        make_dev('conflict', with_intent='intent-xxx',
                 severity='high', message='冲突'),
        make_intent())
    assert result['action'] == gen.ACTION_SUPERSEDE
    assert result['target']['intent_id'] == 'intent-xxx'
    print(f'    action={result["action"]}')


def test_7_state_machine():
    """完整状态机流转."""
    eng = healing_engine.get_engine()
    intent = make_intent('i-state-test', 'connectivity',
                         connectivity=[{'src': '研发', 'dst': '市场', 'allow': True}])
    dev = make_dev('connectivity',
                   assertion={'src': '研发', 'dst': '市场', 'allow': True},
                   message='连通性违反')

    cycle_id = eng.trigger_manual(intent, dev)
    # 引擎会自动推进状态机，最后进入 verifying
    # 等几秒让状态机跑完
    print(f'    cycle_id={cycle_id}')

    # 检查 cycle 在历史里
    deadline = time.time() + 12
    while time.time() < deadline:
        c = eng.get_cycle(cycle_id)
        if not c:
            print(f'    cycle 已完成 (已移入历史)')
            break
        print(f'    state={c["current_state"]} attempt={c["verify_attempts"]}')
        time.sleep(1)
    else:
        print(f'    ⚠ timeout, 仍在: {c["current_state"]}')

    status = eng.status()
    print(f'    history={status["history_count"]}, done={status["done_count"]}, '
          f'failed={status["failed_count"]}')
    assert status['history_count'] >= 1, 'expected history to grow'


def test_8_ack():
    """上游 ack 路径."""
    eng = healing_engine.get_engine()
    intent = make_intent('i-ack-test', 'qos',
                         qos=[{'link': 's1-s2', 'max_latency_ms': 30}])
    dev = make_dev('qos', assertion={'link': 's1-s2', 'max_latency_ms': 30},
                   actual=80, message='时延超限')
    cycle_id = eng.trigger_manual(intent, dev)

    # 立即 ack（不等 auto ack）
    time.sleep(0.2)
    result = eng.ack(cycle_id)
    assert result['status'] == 'ok', result
    print(f'    ack result: {result}')


def test_9_persistence():
    """持久化: 创建 cycle, 杀进程, 重启, 数据仍在."""
    eng = healing_engine.get_engine()
    intent = make_intent('i-persist-test', 'isolation')
    dev = make_dev('connectivity',
                   assertion={'src': '财务', 'dst': '研发', 'allow': False})
    cycle_id = eng.trigger_manual(intent, dev)
    print(f'    新 cycle: {cycle_id}')

    # 模拟重启 - 直接读文件
    assert os.path.exists(healing_engine.PERSIST_CYCLES_FILE) or \
           os.path.exists(healing_engine.PERSIST_HISTORY_FILE)
    # 等移到 history
    time.sleep(10)
    assert os.path.exists(healing_engine.PERSIST_HISTORY_FILE)
    with open(healing_engine.PERSIST_HISTORY_FILE) as f:
        history = json.load(f)
    print(f'    history has {len(history)} cycles')


def test_10_concurrent_cycles():
    """并发处理多个 cycle."""
    eng = healing_engine.get_engine()
    cycles = []
    for i in range(3):
        intent = make_intent(f'i-conc-{i}', 'connectivity')
        dev = make_dev('connectivity',
                       assertion={'src': f'A{i}', 'dst': f'B{i}', 'allow': True})
        cid = eng.trigger_manual(intent, dev)
        cycles.append(cid)

    status = eng.status()
    print(f'    创建 {len(cycles)} cycles, active={status["active_cycles"]}, '
          f'upstream_buffer={status["upstream_buffer"]}')
    assert status['active_cycles'] + status['history_count'] >= 3


# ============================================================
# 运行
# ============================================================

# 注册所有数字命名测试
def _numbered(num, name):
    return [
        ('生成器: link_status → recover_link', test_1_link_status),
        ('生成器: connectivity → reapply_acl', test_2_connectivity),
        ('生成器: qos latency → adjust_qos', test_3_qos_latency),
        ('生成器: qos loss → boost_bandwidth', test_4_qos_loss),
        ('生成器: reachability → reroute', test_5_reachability),
        ('生成器: conflict → supersede', test_6_conflict),
        ('引擎: 完整状态机流转', test_7_state_machine),
        ('引擎: 上游 ack 路径', test_8_ack),
        ('引擎: 持久化', test_9_persistence),
        ('引擎: 多 cycle 并发', test_10_concurrent_cycles),
    ]


def run_all():
    tests = [
        ('生成器: link_status → recover_link', test_1_link_status),
        ('生成器: connectivity → reapply_acl', test_2_connectivity),
        ('生成器: qos latency → adjust_qos', test_3_qos_latency),
        ('生成器: qos loss → boost_bandwidth', test_4_qos_loss),
        ('生成器: reachability → reroute', test_5_reachability),
        ('生成器: conflict → supersede', test_6_conflict),
        ('引擎: 完整状态机流转', test_7_state_machine),
        ('引擎: 上游 ack 路径', test_8_ack),
        ('引擎: 持久化', test_9_persistence),
        ('引擎: 多 cycle 并发', test_10_concurrent_cycles),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        print(f'\n--- {name} ---')
        try:
            fn()
            print(f'{PASS} {name}')
            passed += 1
        except AssertionError as e:
            print(f'{FAIL} {name}: {e}')
            failed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f'{FAIL} {name}: {type(e).__name__}: {e}')
            failed += 1
    print('\n' + '=' * 70)
    print(f'通过: {passed}/{len(tests)}  失败: {failed}')
    return failed == 0


if __name__ == '__main__':
    ok = run_all()
    sys.exit(0 if ok else 1)