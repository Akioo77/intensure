"""异常检测算法 - 从历史数据中检测异常模式。

4 种异常类型:
  1. latency_spike    - 时延突增（> 历史均值 + Nσ）
  2. loss_burst       - 丢包持续（N 个连续采样 > 阈值）
  3. traffic_anomaly  - 流量异常（与基线偏差 > X%）
  4. link_flapping    - 链路震荡（60s 内同链路 down/up > 3 次）

每条异常:
  {
    'type': 'latency_spike',
    'severity': 'high' | 'medium' | 'low',
    'subject': 's1-s2' | 'h1' | 'global',  # 异常对象
    'description': '人话',
    'evidence': { ...原始数据... },
    'detected_at': float,
    'recommendation': '建议动作',
  }

主入口: detect_all(state, history, connectivity) -> List[异常
]
"""
import math
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional

# 默认参数
DEFAULT_WINDOW_SIZE = 20      # 滑动窗口大小
DEFAULT_LATENCY_SIGMA = 2.0   # 时延异常 σ 阈值
DEFAULT_LOSS_THRESHOLD_PCT = 1.0  # 丢包阈值
DEFAULT_LOSS_DURATION_SAMPLES = 5  # 丢包持续采样数
DEFAULT_TRAFFIC_DEVIATION_PCT = 30.0  # 流量偏差阈值
DEFAULT_FLAPPING_WINDOW_S = 60  # 震荡检测窗口
DEFAULT_FLAPPING_THRESHOLD = 3  # 震荡阈值（次数）


# ============================================================
# 工具函数
# ============================================================

def _stats(values: List[float]) -> Dict[str, float]:
    """基础统计量."""
    if not values:
        return {'mean': 0, 'std': 0, 'min': 0, 'max': 0, 'n': 0}
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / max(1, n - 1)
    std = math.sqrt(var)
    return {
        'mean': mean, 'std': std,
        'min': min(values), 'max': max(values),
        'n': n,
    }


def _by_link(history: List[Dict], field: str = 'latency_ms') -> Dict[str, List]:
    """按链路分组提取数据.

    history items 形如:
      {
        'time': float,
        'flows': int, 'hosts': int, 'switches': int,
        'rx_bytes': int, 'tx_bytes': int,
        'latency_ms': float,  # 仅当 latency_probe 测得
        'loss_pct': float,
      }
    """
    out = defaultdict(list)
    for h in history:
        if field in h:
            # 目前所有数据合并到 'global'，未来可按 link 拆分
            out['global'].append(h[field])
    return dict(out)


# ============================================================
# 检测 1: 时延突增
# ============================================================

def detect_latency_spike(history: List[Dict],
                         sigma: float = DEFAULT_LATENCY_SIGMA,
                         window: int = DEFAULT_WINDOW_SIZE) -> List[Dict]:
    """时延突增检测.

    规则: latest_latency > mean(window) + sigma * std(window)
    """
    latencies = [h.get('latency_ms') for h in history if h.get('latency_ms') is not None]
    if len(latencies) < 5:
        return []  # 数据不足

    window_data = latencies[-window:]
    stats = _stats(window_data[:-1])  # 用历史窗口（不含最新）
    if stats['std'] == 0:
        # 时延一直稳定，突增 = 当前 > 均值 50%
        threshold = stats['mean'] * 1.5 if stats['mean'] > 0 else 100
    else:
        threshold = stats['mean'] + sigma * stats['std']

    latest = window_data[-1]
    if latest > threshold:
        ratio = latest / max(stats['mean'], 1)
        severity = 'high' if ratio > 3 else 'medium' if ratio > 2 else 'low'
        return [{
            'type': 'latency_spike',
            'severity': severity,
            'subject': 'global',
            'description': (
                f'时延突增: 当前 {latest:.1f}ms '
                f'(均值 {stats["mean"]:.1f}ms + {sigma}σ)'
            ),
            'evidence': {
                'current_ms': latest,
                'baseline_mean_ms': round(stats['mean'], 2),
                'baseline_std_ms': round(stats['std'], 2),
                'threshold_ms': round(threshold, 2),
                'ratio_to_mean': round(ratio, 2),
                'sample_count': stats['n'],
            },
            'detected_at': time.time(),
            'recommendation': '检查最近链路变更 / 注入的时延 / 流量突发',
        }]
    return []


# ============================================================
# 检测 2: 丢包持续
# ============================================================

def detect_loss_burst(history: List[Dict],
                      threshold_pct: float = DEFAULT_LOSS_THRESHOLD_PCT,
                      duration: int = DEFAULT_LOSS_DURATION_SAMPLES) -> List[Dict]:
    """丢包持续检测.

    规则: 连续 N 个采样 loss_pct > 阈值
    """
    loss_seq = [h.get('loss_pct', 0) for h in history[-20:]]  # 看最近 20 个
    if len(loss_seq) < duration:
        return []

    # 检查最后 N 个是否都超过阈值
    recent = loss_seq[-duration:]
    if all(x > threshold_pct for x in recent):
        max_loss = max(recent)
        avg_loss = sum(recent) / len(recent)
        severity = 'high' if max_loss > 50 else 'medium' if max_loss > 10 else 'low'
        return [{
            'type': 'loss_burst',
            'severity': severity,
            'subject': 'global',
            'description': (
                f'丢包持续 {duration} 个采样: '
                f'平均 {avg_loss:.1f}%, 峰值 {max_loss:.1f}%'
            ),
            'evidence': {
                'threshold_pct': threshold_pct,
                'duration_samples': duration,
                'avg_loss_pct': round(avg_loss, 2),
                'max_loss_pct': max_loss,
                'recent_samples': recent,
            },
            'detected_at': time.time(),
            'recommendation': '触发自愈流程 / 检查链路 / 流量整形',
        }]
    return []


# ============================================================
# 检测 3: 流量异常
# ============================================================

def detect_traffic_anomaly(history: List[Dict],
                           deviation_pct: float = DEFAULT_TRAFFIC_DEVIATION_PCT,
                           window: int = DEFAULT_WINDOW_SIZE) -> List[Dict]:
    """流量异常检测.

    规则: |current_rx - mean(window)| / mean > deviation_pct
    适用: rx_bytes 和 tx_bytes
    """
    anomalies = []
    for metric in ['rx_bytes', 'tx_bytes']:
        values = [h.get(metric, 0) for h in history if h.get(metric) is not None]
        if len(values) < window:
            continue
        stats = _stats(values[:-1])
        latest = values[-1]
        if stats['mean'] <= 0:
            continue
        deviation = abs(latest - stats['mean']) / stats['mean'] * 100
        if deviation > deviation_pct:
            direction = '突增' if latest > stats['mean'] else '骤降'
            severity = 'high' if deviation > 100 else 'medium' if deviation > 50 else 'low'
            anomalies.append({
                'type': 'traffic_anomaly',
                'severity': severity,
                'subject': f'global.{metric}',
                'description': (
                    f'流量{direction}: 当前 {latest/1024:.1f}KB, '
                    f'基线均值 {stats["mean"]/1024:.1f}KB '
                    f'(偏差 {deviation:.0f}%)'
                ),
                'evidence': {
                    'metric': metric,
                    'current_bytes': latest,
                    'baseline_mean_bytes': round(stats['mean'], 2),
                    'baseline_std_bytes': round(stats['std'], 2),
                    'deviation_pct': round(deviation, 1),
                    'sample_count': stats['n'],
                },
                'detected_at': time.time(),
                'recommendation': (
                    '检查是否有大流量突发 / DDoS / 配置变更'
                ),
            })
    return anomalies


# ============================================================
# 检测 4: 链路震荡
# ============================================================

def detect_link_flapping(events: List[Dict],
                         window_s: float = DEFAULT_FLAPPING_WINDOW_S,
                         threshold: int = DEFAULT_FLAPPING_THRESHOLD) -> List[Dict]:
    """链路震荡检测.

    规则: 同一 dpid:port 在 window_s 秒内状态切换 > threshold 次
    期望 events 形如:
      [
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': true, 'time': 1.0},
        {'type': 'port_link_status', 'dpid': '0x1', 'port_no': 3, 'down': false, 'time': 1.5},
        ...
      ]
    """
    if not events:
        return []

    now = max(e.get('time', 0) for e in events)
    cutoff = now - window_s

    # 按 dpid:port 分组
    by_port: Dict[str, List[Dict]] = defaultdict(list)
    for e in events:
        if e.get('type') != 'port_link_status':
            continue
        if e.get('time', 0) < cutoff:
            continue
        key = f'{e.get("dpid", "?")}:{e.get("port_no", "?")}'
        by_port[key].append(e)

    anomalies = []
    for key, evts in by_port.items():
        if len(evts) < threshold + 1:  # 至少 N+1 次事件（>= N 次切换）
            continue
        evts.sort(key=lambda x: x.get('time', 0))
        # 数 down→up 或 up→down 切换
        transitions = sum(
            1 for i in range(1, len(evts))
            if evts[i].get('down') != evts[i-1].get('down')
        )
        if transitions >= threshold:
            # 找端口名
            port_name = evts[-1].get('name', key)
            anomalies.append({
                'type': 'link_flapping',
                'severity': 'high',
                'subject': key,
                'description': (
                    f'链路震荡: {port_name} 在 {window_s:.0f}s 内'
                    f'状态切换 {transitions} 次'
                ),
                'evidence': {
                    'port': port_name,
                    'window_s': window_s,
                    'transition_count': transitions,
                    'event_count': len(evts),
                    'last_state': 'down' if evts[-1].get('down') else 'up',
                },
                'detected_at': time.time(),
                'recommendation': (
                    '检查物理链路 / 网线 / 端口协商 /'
                    '上游交换机 STP 配置'
                ),
            })
    return anomalies


# ============================================================
# 主入口
# ============================================================

def detect_all(state: Dict = None,
               history: List[Dict] = None,
               events: List[Dict] = None,
               connectivity: List[Dict] = None) -> List[Dict]:
    """检测所有异常.

    参数:
      state: 当前网络状态
      history: 历史状态采样（汇总）
      events: 事件流
      connectivity: 连通性探测历史

    返回: 异常列表
    """
    history = history or []
    events = events or []
    connectivity = connectivity or []

    # 把 connectivity 也合并进 history 视野（latency/loss 字段）
    merged_hist = list(history)
    for c in connectivity[-20:]:
        if c.get('latency_ms') is not None or c.get('loss_pct') is not None:
            merged_hist.append({
                'time': c.get('time', 0),
                'latency_ms': c.get('latency_ms'),
                'loss_pct': c.get('loss_pct', 0),
            })

    all_anomalies = []
    all_anomalies.extend(detect_latency_spike(merged_hist))
    all_anomalies.extend(detect_loss_burst(merged_hist))
    all_anomalies.extend(detect_traffic_anomaly(merged_hist))
    all_anomalies.extend(detect_link_flapping(events))

    # 按严重度排序
    severity_order = {'high': 0, 'medium': 1, 'low': 2}
    all_anomalies.sort(key=lambda a: (severity_order.get(a['severity'], 9),
                                       -a['detected_at']))
    return all_anomalies


def summarize(anomalies: List[Dict]) -> Dict:
    """汇总异常统计."""
    by_type = {}
    by_severity = {'high': 0, 'medium': 0, 'low': 0}
    for a in anomalies:
        t = a['type']
        by_type[t] = by_type.get(t, 0) + 1
        s = a['severity']
        by_severity[s] = by_severity.get(s, 0) + 1
    return {
        'total': len(anomalies),
        'by_type': by_type,
        'by_severity': by_severity,
        'has_high': by_severity['high'] > 0,
    }