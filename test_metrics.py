#!/usr/bin/env python3
"""test_metrics.py — 指标注册表与 /metrics 端点单元测试

覆盖场景：
  1. Counter 累加与渲染
  2. Gauge 赋值 / 自增自减
  3. Histogram 桶 / _sum / _count
  4. 标签转义（\\ " 换行）
  5. 同名指标复用（不会重复注册）
  6. 并发写不丢计数（线程安全）
  7. /metrics 端点返回 Prometheus 文本格式
  8. /api/health 带版本号

运行: python3 -m pytest test_metrics.py -v
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import metrics  # noqa: E402

try:
    import flask  # noqa: F401
except ImportError:
    flask = None

if flask is not None:
    import state_server
else:
    state_server = None


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'{name}  {detail}')


def test_counter():
    reg = metrics.Registry()
    c = reg.counter('demo_total', '演示计数器', ['kind'])
    c.inc({'kind': 'a'})
    c.inc({'kind': 'a'}, 2)
    c.inc({'kind': 'b'})
    text = reg.render()
    check('渲染出 counter 类型', '# TYPE demo_total counter' in text, text)
    check('累加正确', 'demo_total{kind="a"} 3' in text, text)
    check('get 读值正确', c.get({'kind': 'a'}) == 3, str(c.get({'kind': 'a'})))
    check('未定义的标签读 0', c.get({'kind': 'zzz'}) == 0)


def test_gauge():
    reg = metrics.Registry()
    g = reg.gauge('demo_gauge', '演示仪表', ['mode'])
    g.set(5, {'mode': 'x'})
    g.inc({'mode': 'x'})
    g.dec({'mode': 'x'}, 2)
    text = reg.render()
    check('gauge 类型正确', '# TYPE demo_gauge gauge' in text, text)
    check('gauge 值正确', 'demo_gauge{mode="x"} 4' in text, text)


def test_histogram():
    reg = metrics.Registry()
    h = reg.histogram('demo_seconds', '演示直方图', ['op'], buckets=(0.1, 0.5, 1.0))
    h.observe(0.05, {'op': 'x'})
    h.observe(0.4, {'op': 'x'})
    h.observe(2.0, {'op': 'x'})
    text = reg.render()
    check('histogram 类型正确', '# TYPE demo_seconds histogram' in text, text)
    check('第一个桶计数 = 1', 'demo_seconds_bucket{op="x",le="0.1"} 1' in text, text)
    check('第二个桶计数 = 2', 'demo_seconds_bucket{op="x",le="0.5"} 2' in text, text)
    check('+Inf 桶计数 = 3', 'demo_seconds_bucket{op="x",le="+Inf"} 3' in text, text)
    check('_count = 3', 'demo_seconds_count{op="x"} 3' in text, text)
    check('_sum 存在', 'demo_seconds_sum{op="x"} 2.45' in text, text)
    check('count() 一致', h.count({'op': 'x'}) == 3)


def test_label_escaping():
    reg = metrics.Registry()
    c = reg.counter('esc_total', '转义测试', ['v'])
    c.inc({'v': 'a"b\\c\nd'})
    text = reg.render()
    check('引号被转义', 'a\\"b\\\\c\\nd' in text, text)


def test_same_name_reused():
    reg = metrics.Registry()
    a = reg.counter('same_total', '同名复用', [])
    b = reg.counter('same_total', '同名复用', [])
    check('同名返回同一实例', a is b)
    a.inc()
    check('计数共享', b.get() == 1, str(b.get()))
    check('只注册一次', reg.names().count('same_total') == 1)


def test_thread_safety():
    reg = metrics.Registry()
    c = reg.counter('mt_total', '并发测试', [])

    def worker():
        for _ in range(500):
            c.inc()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check('并发计数不丢', c.get() == 2000, str(c.get()))


def test_render_empty_registry():
    reg = metrics.Registry()
    check('空注册表渲染为空串', reg.render() == '')


def test_project_version_and_build_info():
    check('版本号非空', bool(metrics.PROJECT_VERSION))
    text = metrics.render()
    check('build_info 存在', 'intensure_build_info' in text, text[:400])
    check('build_info 带版本', f'version="{metrics.PROJECT_VERSION}"' in text, text[:400])


def test_metrics_endpoint():
    if flask is None:
        return  # 没装 flask 就跳过（VM 里一定有）
    client = state_server.app.test_client()
    resp = client.get('/metrics')
    check('/metrics 返回 200', resp.status_code == 200, str(resp.status_code))
    body = resp.get_data(as_text=True)
    check('是 Prometheus 文本格式', '# TYPE' in body, body[:200])
    check('含请求计数指标', 'intensure_http_requests_total' in body, body[:400])


def test_health_reports_version():
    if flask is None:
        return
    client = state_server.app.test_client()
    resp = client.get('/api/health')
    check('/api/health 返回 200', resp.status_code == 200)
    data = resp.get_json()
    check('health 带版本号', data.get('version') == metrics.PROJECT_VERSION,
          str(data))


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
