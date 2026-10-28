"""metrics.py - 轻量级 Prometheus 指标注册表（仅用标准库）。

为什么自己写而不用 prometheus_client：
  1. CI 是离线/受限网络环境，多一个第三方依赖就多一次装不上的风险
  2. 本项目只需要 counter / gauge / histogram 三种类型 + Prometheus 文本格式
  3. 不引入依赖 = 不引入版本升级与安全维护负担

对外能力：
  - Counter / Gauge / Histogram 三种指标
  - 标签（labels）维度
  - Prometheus 文本 exposition 格式（version 0.0.4）
  - 线程安全（采集层与上报层是多线程的）

用法：
    from metrics import REGISTRY, counter, gauge, histogram, render

    counter('intensure_reports_total', '上报次数', ['endpoint']).inc({'endpoint': 'check'})
    gauge('intensure_assurance_contract', '契约版本状态', ['state']).set(1, {'state': 'ok'})
    hist = histogram('intensure_report_duration_seconds', '上报耗时', ['endpoint'])
    hist.observe(0.12, {'endpoint': 'check'})
    print(render())
"""
import threading
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# 与 CHANGELOG v3.18.0 对齐（build_info 指标用）
PROJECT_VERSION = '3.18.0'

# 直方图默认桶（秒）——覆盖 1ms ~ 30s，够上报/探测量级用
DEFAULT_BUCKETS: Tuple[float, ...] = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

LabelKey = Tuple[Tuple[str, str], ...]


def _normalize_labels(labels: Optional[Dict[str, str]]) -> LabelKey:
    """把 labels dict 变成排序后的元组（保证同一组标签只有一个 key）。"""
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape_label_value(value: str) -> str:
    """转义标签值里的 \\ " 和换行（Prometheus 文本格式要求）。"""
    return (
        str(value)
        .replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
    )


def _render_labels(labelnames: Sequence[str], labels: LabelKey) -> str:
    """渲染 {a="1",b="2"} 形式的标签串；没有标签时返回空串。"""
    if not labelnames:
        return ''
    values = dict(labels)
    parts = []
    for name in labelnames:
        parts.append(f'{name}="{_escape_label_value(values.get(name, ""))}"')
    return '{' + ','.join(parts) + '}'


class Counter:
    """单调递增计数器。"""

    def __init__(self, name: str, documentation: str, labelnames: Sequence[str] = ()):
        self.name = name
        self.documentation = documentation
        self.labelnames = tuple(labelnames)
        self._values: Dict[LabelKey, float] = {}
        self._lock = threading.Lock()

    def inc(self, labels: Optional[Dict[str, str]] = None, value: float = 1.0) -> None:
        """计数 +value（默认 +1）。"""
        key = _normalize_labels(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        """读当前值（主要用于测试/自检）。"""
        key = _normalize_labels(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> List[str]:
        with self._lock:
            items = sorted(self._values.items())
        lines = [f'# HELP {self.name} {self.documentation}',
                 f'# TYPE {self.name} counter']
        for key, value in items:
            lines.append(f'{self.name}{_render_labels(self.labelnames, key)} {value:g}')
        return lines


class Gauge:
    """可增可减的瞬时值。"""

    def __init__(self, name: str, documentation: str, labelnames: Sequence[str] = ()):
        self.name = name
        self.documentation = documentation
        self.labelnames = tuple(labelnames)
        self._values: Dict[LabelKey, float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = _normalize_labels(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, labels: Optional[Dict[str, str]] = None, value: float = 1.0) -> None:
        key = _normalize_labels(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def dec(self, labels: Optional[Dict[str, str]] = None, value: float = 1.0) -> None:
        self.inc(labels, -value)

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        key = _normalize_labels(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> List[str]:
        with self._lock:
            items = sorted(self._values.items())
        lines = [f'# HELP {self.name} {self.documentation}',
                 f'# TYPE {self.name} gauge']
        for key, value in items:
            lines.append(f'{self.name}{_render_labels(self.labelnames, key)} {value:g}')
        return lines


class Histogram:
    """固定桶直方图（带 _sum / _count）。"""

    def __init__(self,
                 name: str,
                 documentation: str,
                 labelnames: Sequence[str] = (),
                 buckets: Iterable[float] = DEFAULT_BUCKETS):
        self.name = name
        self.documentation = documentation
        self.labelnames = tuple(labelnames)
        self.buckets = tuple(sorted(float(b) for b in buckets))
        self._counts: Dict[LabelKey, List[int]] = {}
        self._sums: Dict[LabelKey, float] = {}
        self._totals: Dict[LabelKey, int] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """记录一次观测值（秒）。"""
        key = _normalize_labels(labels)
        value = float(value)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for i, upper in enumerate(self.buckets):
                if value <= upper:
                    counts[i] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._totals[key] = self._totals.get(key, 0) + 1

    def count(self, labels: Optional[Dict[str, str]] = None) -> int:
        """观测总次数（含超出最大桶的样本）。"""
        key = _normalize_labels(labels)
        with self._lock:
            return self._totals.get(key, 0)

    def sum(self, labels: Optional[Dict[str, str]] = None) -> float:
        """观测值总和。"""
        key = _normalize_labels(labels)
        with self._lock:
            return self._sums.get(key, 0.0)

    def render(self) -> List[str]:
        with self._lock:
            keys = sorted(self._counts.keys())
            snapshot = {k: (list(self._counts[k]), self._sums.get(k, 0.0),
                            self._totals.get(k, 0)) for k in keys}

        lines = [f'# HELP {self.name} {self.documentation}',
                 f'# TYPE {self.name} histogram']
        bucket_labelnames = self.labelnames + ('le',)
        for key, (counts, total, observed) in snapshot.items():
            for upper, count in zip(self.buckets, counts):
                bucket_labels = dict(key)
                bucket_labels['le'] = f'{upper:g}'
                label_str = _render_labels(bucket_labelnames, bucket_labels)
                lines.append(f'{self.name}_bucket{label_str} {count}')
            # +Inf 桶 = 全部观测数（含超出最大桶的样本）
            inf_labels = dict(key)
            inf_labels['le'] = '+Inf'
            label_str = _render_labels(bucket_labelnames, inf_labels)
            lines.append(f'{self.name}_bucket{label_str} {observed}')
            lines.append(f'{self.name}_sum{_render_labels(self.labelnames, key)} {total:g}')
            lines.append(f'{self.name}_count{_render_labels(self.labelnames, key)} {observed}')
        return lines


class Registry:
    """指标注册表：按名字存放指标，渲染成 Prometheus 文本格式。"""

    def __init__(self):
        self._metrics: Dict[str, object] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> Counter:
        """取（没有就建）一个 counter。同名重复调用返回同一个实例。"""
        return self._get_or_create(name, lambda: Counter(name, documentation, labelnames))

    def gauge(self, name: str, documentation: str, labelnames: Sequence[str] = ()) -> Gauge:
        """取（没有就建）一个 gauge。"""
        return self._get_or_create(name, lambda: Gauge(name, documentation, labelnames))

    def histogram(self,
                  name: str,
                  documentation: str,
                  labelnames: Sequence[str] = (),
                  buckets: Iterable[float] = DEFAULT_BUCKETS) -> Histogram:
        """取（没有就建）一个 histogram。"""
        return self._get_or_create(
            name, lambda: Histogram(name, documentation, labelnames, buckets))

    def _get_or_create(self, name: str, factory):
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = factory()
                self._metrics[name] = metric
            return metric

    def names(self) -> List[str]:
        """已注册的指标名（测试用）。"""
        with self._lock:
            return sorted(self._metrics.keys())

    def reset(self) -> None:
        """清空注册表（测试用；生产不要调）。"""
        with self._lock:
            self._metrics.clear()

    def render(self) -> str:
        """渲染成 Prometheus exposition 文本（以换行结尾）。"""
        with self._lock:
            metrics = [self._metrics[name] for name in sorted(self._metrics.keys())]
        lines: List[str] = []
        for metric in metrics:
            lines.extend(metric.render())
        if not lines:
            return ''
        return '\n'.join(lines) + '\n'


# 全局默认注册表
REGISTRY = Registry()


def counter(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Counter:
    """全局注册表取 counter。"""
    return REGISTRY.counter(name, documentation, labelnames)


def gauge(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Gauge:
    """全局注册表取 gauge。"""
    return REGISTRY.gauge(name, documentation, labelnames)


def histogram(name: str,
              documentation: str,
              labelnames: Sequence[str] = (),
              buckets: Iterable[float] = DEFAULT_BUCKETS) -> Histogram:
    """全局注册表取 histogram。"""
    return REGISTRY.histogram(name, documentation, labelnames, buckets)


def render() -> str:
    """渲染全局注册表。"""
    return REGISTRY.render()


# ---- 常用指标（集中定义，避免各处拼错名字）------

# 上报层（reporter.py / assurance_bridge.py）用
REPORTS_TOTAL = counter(
    'intensure_reports_total', '上报请求总次数（按端点与结果分类）', ['endpoint', 'result'])
REPORT_FAILURES_TOTAL = counter(
    'intensure_report_failures_total', '上报失败总次数（按端点与失败原因分类）',
    ['endpoint', 'reason'])
REPORT_DURATION = histogram(
    'intensure_report_duration_seconds', '上报耗时（秒）', ['endpoint'])

# 自营 REST 层（state_server.py）用
HTTP_REQUESTS_TOTAL = counter(
    'intensure_http_requests_total', '自营 REST 接口请求总数', ['method', 'endpoint', 'status'])
HTTP_DURATION = histogram(
    'intensure_http_request_duration_seconds', '自营 REST 接口耗时（秒）', ['endpoint'])

# 契约版本（P3-3）用
CONTRACT_STATE = gauge(
    'intensure_assurance_contract', '与保障模块的契约版本状态（1=当前状态）',
    ['state', 'expected', 'actual'])

# 构建信息
BUILD_INFO = gauge(
    'intensure_build_info', 'intensure 构建信息', ['version'])
BUILD_INFO.set(1, {'version': PROJECT_VERSION})


def snapshot() -> Dict[str, object]:
    """给 dashboard / 自检用的 JSON 友好快照（不是 Prometheus 格式）。"""
    data = {
        'project_version': PROJECT_VERSION,
        'generated_at': time.time(),
        'reports_total': {},
        'report_failures_total': {},
        'http_requests_total': {},
    }
    for key, value in REPORTS_TOTAL._values.items():  # noqa: SLF001 - 同包内读取
        data['reports_total']['/'.join(f'{k}={v}' for k, v in key)] = value
    for key, value in REPORT_FAILURES_TOTAL._values.items():  # noqa: SLF001
        data['report_failures_total']['/'.join(f'{k}={v}' for k, v in key)] = value
    for key, value in HTTP_REQUESTS_TOTAL._values.items():  # noqa: SLF001
        data['http_requests_total']['/'.join(f'{k}={v}' for k, v in key)] = value
    return data


if __name__ == '__main__':
    REPORTS_TOTAL.inc({'endpoint': 'check', 'result': 'ok'})
    REPORT_DURATION.observe(0.012, {'endpoint': 'check'})
    HTTP_REQUESTS_TOTAL.inc({'method': 'GET', 'endpoint': 'health', 'status': '200'})
    print(render())
