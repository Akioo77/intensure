"""测试异常检测算法 - 8 个测试场景。

覆盖:
  1. 时延突增 (latency_spike) ✅
  2. 时延稳定（不应误报） ✅
  3. 丢包持续 (loss_burst) ✅
  4. 丢包偶发（不应误报） ✅
  5. 流量突增 (traffic_anomaly) ✅
  6. 流量骤降 (traffic_anomaly) ✅
  7. 链路震荡 (link_flapping) ✅
  8. 综合: 正常数据 (无异常)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anomaly_detector


PASS = '✅'
FAIL = '❌'


# ============================================================
# 测试用例
# ============================================================

def make_history(latencies=None, losses=None, rx=None, tx=None, n=20):
    """构造历史采样."""
    history = []
    for i in range(n):
        h = {
            'time': 1000 + i * 5,
            'flows': 18, 'hosts': 4, 'switches': 2,
            'rx_bytes': 0, 'tx_bytes': 0,
            'latency_ms': None, 'loss_pct': 0,
        }
        if latencies is not None and i < len(latencies):
            h['latency_ms'] = latencies[i]
        if losses is not None and i < len(losses):
            h['loss_pct'] = losses[i]
        if rx is not None and i < len(rx):
            h['rx_bytes'] = rx[i]
        if tx is not None and i < len(tx):
            h['tx_bytes'] = tx[i]
        returnhistory
    return history


def make_history(latencies=None, losses=None, rx=None, tx=None, n=20):
    history = []
    for i in range(n):
        h = {
            'time': 1000 + i * 5,
            'flows': 18, 'hosts': 4, 'switches': 2,
            'rx_bytes': 0, 'tx_bytes': 0,
            'latency_ms': None, 'loss_pct': 0,
        }
        if latencies and i < len(latencies):
            h['latency_ms'] = latencies[i]
        if losses and i < len(losses):
            h['loss_pct'] = losses[i]
        if rx and i < len(rx):
            h['rx_bytes'] = rx[i]
        if tx and i < len(tx):
            h['tx_bytes'] = tx[i]
        history.append(h)
    return history


def test_1_latency_spike():
    """时延突增: 基线 10ms, 最新 100ms."""
    # 前 19 个稳定在 10ms ± 2, 最后一个 100ms
    latencies = [10 + (i % 3) for i in range(19)] + [100]
    hist = make_history(latencies=latencies)
    anomalies = anomaly_detector.detect_latency_spike(hist)
    assert len(anomalies) == 1, f'got {len(anomalies)}'
    assert anomalies[0]['type'] == 'latency_spike'
    assert anomalies[0]['severity'] in ('high', 'medium')
    print(f'  ✅ severity={anomalies[0]["severity"]}, evidence={anomalies[0]["evidence"]}')


def test_2_latency_stable():
    """时延稳定: 不应误报."""
    latencies = [10 + (i % 3) for i in range(20)]  # 7~13 区间
    hist = make_history(latencies=latencies)
    anomalies = anomaly_detector.detect_latency_spike(hist)
    assert len(anomalies) == 0, f'got {len(anomalies)}'
    print('  ✅ 无误报')


def test_3_loss_burst():
    """丢包持续: 最近 5 个采样都 > 1%."""
    losses = [0] * 15 + [5.0] * 5  # 最后 5 个都是 5%
    hist = make_history(losses=losses)
    anomalies = anomaly_detector.detect_loss_burst(hist)
    assert len(anomalies) == 1, f'got {len(anomalies)}'
    assert anomalies[0]['type'] == 'loss_burst'
    print(f'  ✅ severity={anomalies[0]["severity"]}, max={anomalies[0]["evidence"]["max_loss_pct"]}%')


def test_4_loss_occasional():
    """丢包偶发: 只有 1-2 个采样 > 阈值, 不应误报."""
    losses = [0] * 18 + [5.0, 0]  # 最后 2 个: 5% 和 0
    hist = make_history(losses=losses)
    anomalies = anomaly_detector.detect_loss_burst(hist)
    assert len(anomalies) == 0, f'got {len(anomalies)}'
    print('  ✅ 无误报')


def test_5_traffic_spike():
    """流量突增: 基线 1000 bytes, 最新 5000 bytes (+400%)."""
    rx = [1000 + (i % 100) for i in range(19)] + [5000]
    hist = make_history(rx=rx)
    anomalies = anomaly_detector.detect_traffic_anomaly(hist)
    assert any(a['type'] == 'traffic_anomaly' for a in anomalies)
    rx_anom = [a for a in anomalies if 'rx_bytes' in a['subject']][0]
    assert rx_anom['evidence']['deviation_pct'] > 100
    print(f'  ✅ rx_bytes 偏差 {rx_anom["evidence"]["deviation_pct"]}%')


def test_6_traffic_drop():
    """流量骤降: 基线 1000 bytes, 最新 100 bytes (-90%)."""
    rx = [1000 + (i % 100) for i in range(19)] + [100]
    hist = make_history(rx=rx)
    anomalies = anomaly_detector.detect_traffic_anomaly(hist)
    assert any(a['type'] == 'traffic_anomaly' for a in anomalies)
    rx_anom = [a for a in anomalies if 'rx_bytes' in a['subject']][0]
    print(f'  ✅ rx_bytes 偏差 {rx_anom["evidence"]["deviation_pct"]}%')


def test_7_link_flapping():
    """链路震荡: 60s 内 down/up 切换 4 次."""
    now = 1000.0
    events = [
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': True, 'time': now - 50},
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': False, 'time': now - 45},
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': True, 'time': now - 40},
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': False, 'time': now - 35},
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': True, 'time': now - 30},
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': False, 'time': now - 25},
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': True, 'time': now - 10},
    ]
    anomalies = anomaly_detector.detect_link_flapping(events)
    assert len(anomalies) == 1, f'got {len(anomalies)}'
    assert anomalies[0]['type'] == 'link_flapping'
    assert anomalies[0]['severity'] == 'high'
    print(f'  ✅ transitions={anomalies[0]["evidence"]["transition_count"]}')


def test_8_no_anomaly_normal_data():
    """综合: 一切正常, 不应误报."""
    latencies = [10] * 20
    losses = [0] * 20
    rx = [1000] * 20
    events = []
    hist = make_history(latencies=latencies, losses=losses, rx=rx)

    anomalies = anomaly_detector.detect_all(
        state={}, history=hist, events=events)
    assert len(anomalies) == 0, f'got {len(anomalies)}: {[a["type"] for a in anomalies]}'
    print('  ✅ 一切正常，0 异常')


# ============================================================
# 运行
# ============================================================

TESTS = [
    ('1. 时延突增', test_1_latency_spike),
    ('2. 时延稳定（无误报）', test_2_latency_stable),
    ('3. 丢包持续', test_3_loss_burst),
    ('4. 丢包偶发（无误报）', test_4_loss_occasional),
    ('5. 流量突增', test_5_traffic_spike),
    ('6. 流量骤降', test_6_traffic_drop),
    ('7. 链路震荡', test_7_link_flapping),
    ('8. 综合正常数据', test_8_no_anomaly_normal_data),
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
            print(f'{FAIL} {name}: {e}')
            failed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f'{FAIL} {name}: {type(e).__name__}: {e}')
            failed += 1
    print('\n' + '=' * 70)
    print(f'通过: {passed}/{len(TESTS)}  失败: {failed}')
    return failed == 0


if __name__ == '__main__':
    ok = run_all()
    sys.exit(0 if ok else 1)