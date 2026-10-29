"""Flask REST 服务：暴露网络状态 + 验证结果。

端点:
  GET /api/state            完整状态
  GET /api/switches         交换机列表
  GET /api/hosts            主机列表
  GET /api/port_stats       端口统计
  GET /api/flows            流表
  GET /api/events           事件流
  GET /api/validate         跑状态验证（返回问题列表）
  GET /api/health           健康检查
"""
import json
import os
import time

import state_history

import fault_api  # noqa: E402  (v3.19.0 故障注入测试接口)

import metrics  # noqa: E402  (v3.18.0 可观测性)

from flask import Flask, Response, g, jsonify, request, send_from_directory
from flask_cors import CORS

# ---- 日志（P1-2: 统一用 logging, 不再用 print）----
import logging  # noqa: E402

from logging_setup import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)

# 共享状态: 优先用内存（与控制器同进程），否则读文件
CONTROLLER_STATE = None  # 由外部注入
STATE_FILE = '/tmp/network_state.json'


def load_state_from_file():
    """从文件读取最新状态."""
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_state():
    """优先取内存，否则取文件."""
    if CONTROLLER_STATE is not None:
        return CONTROLLER_STATE
    return load_state_from_file()


# 创建 Flask app
app = Flask(__name__)
CORS(app)


@app.route('/')
def index():
    """可视化监控台 (dashboard.html)."""
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)), 'dashboard.html')


@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok',
        'time': time.time(),
        'source': 'memory' if CONTROLLER_STATE is not None else 'file',
        'version': metrics.PROJECT_VERSION,
    })


# ============================================================
# 可观测性（v3.18.0 · P3-4）
# ============================================================

@app.route('/metrics')
def metrics_endpoint():
    """Prometheus 指标导出（文本格式，version 0.0.4）。

    没有装 prometheus_client 也能被 Prometheus 直接抓取；
    也可以直接 curl 看，或塞进 dashboard 的「系统指标」面板。
    """
    body = metrics.render()
    return Response(body, content_type='text/plain; version=0.0.4; charset=utf-8')


@app.before_request
def _metrics_before_request():
    """记下请求开始时间（after_request 要用）。"""
    g._metrics_t0 = time.perf_counter()


@app.after_request
def _metrics_after_request(response):
    """每个请求记一次计数 + 耗时。"""
    endpoint = request.endpoint or 'unknown'
    t0 = getattr(g, '_metrics_t0', None)
    elapsed = max(0.0, time.perf_counter() - t0) if t0 else 0.0
    metrics.HTTP_REQUESTS_TOTAL.inc({
        'method': request.method,
        'endpoint': endpoint,
        'status': str(response.status_code),
    })
    metrics.HTTP_DURATION.observe(elapsed, {'endpoint': endpoint})
    return response


@app.route('/api/state')
def api_state():
    return jsonify(get_state())


@app.route('/api/switches')
def api_switches():
    return jsonify(get_state().get('switches', {}))


@app.route('/api/hosts')
def api_hosts():
    return jsonify(get_state().get('hosts', {}))


@app.route('/api/port_stats')
def api_port_stats():
    return jsonify(get_state().get('port_stats', {}))


@app.route('/api/flows')
def api_flows():
    return jsonify(get_state().get('flows', {}))


@app.route('/api/links')
def api_links():
    """LLDP 发现的交换机间链路 (去重成对)."""
    state = get_state()
    links = state.get('links', {})
    pairs = []
    seen = set()
    for key, v in links.items():
        try:
            dpid, port = key.split(':')
            dpid, port = dpid, int(port)
            other = (v['peer_dpid'], v['peer_port'])
            pkey = frozenset([(dpid, port), other])
            if pkey in seen:
                continue
            seen.add(pkey)

            def port_name(d, pn):
                for pp in state['switches'].get(d, {}).get('ports', []):
                    if pp['port_no'] == pn:
                        return pp['name']
                return None

            pairs.append({
                'a': {'dpid': dpid, 'port_no': port, 'name': port_name(dpid, port)},
                'b': {'dpid': other[0], 'port_no': other[1],
                      'name': port_name(other[0], other[1])},
                'last_seen': v['last_seen'],
            })
        except Exception:
            continue
    return jsonify(pairs)


@app.route('/api/events')
def api_events():
    """支持 ?since=timestamp&limit=N 参数."""
    state = get_state()
    events = state.get('events', [])
    since = request.args.get('since', default=0, type=float)
    limit = request.args.get('limit', default=100, type=int)
    filtered = [e for e in events if e.get('time', 0) > since]
    return jsonify(filtered[-limit:])


@app.route('/api/validate')
def api_validate():
    """跑状态验证（在查询时即时跑）."""
    from state_model import validate_state
    state = get_state()
    report = validate_state(state)
    return jsonify(report)


@app.route('/api/history')
def api_history():
    """历史状态. ?key=summary|port_stats|validation|connectivity&limit=N"""
    key = request.args.get('key')
    limit = request.args.get('limit', default=100, type=int)
    return jsonify(state_history.get(key, limit))


@app.route('/api/probe', methods=['POST'])
def api_probe():
    """连通性探测结果上报 (由 live_env 等周期调用).

    body: {"reachable": [...], "unreachable": [...], "loss_pct": 0.0}
    """
    data = request.get_json(force=True, silent=True) or {}
    state_history.add_connectivity({
        'time': time.time(),
        'reachable': data.get('reachable', []),
        'unreachable': data.get('unreachable', []),
        'loss_pct': data.get('loss_pct', 0),
        'latency_ms': data.get('latency_ms'),
    })
    state_history.dump()
    return jsonify({'status': 'ok', 'recorded': True})


# ============================================================
# 故障注入（测试接口，与生产模块解耦）
# ============================================================

@app.route('/api/v1/test/inject-fault', methods=['POST'])
def api_inject_fault():
    """注入故障（测试接口）。

    body: {"fault_type": "link_down", "target": "s1-eth1",
           "params": {...}, "mode": "test"}

    fault_type 支持 9 种 + clear：
      link_down / link_up / delay / loss / rate_limit / burst /
      reorder / vlan_mismatch / flow_race / clear
    别名（演示脚本用的业务语义名）：
      high_latency→delay, packet_loss→loss, flow_missing→flow_race

    target 两种写法：
      - 具体接口名（'s1-eth1'）
      - 交换机名（'s1'）→ 该交换机所有端口；params.port 可指定单个端口

    ⚠️ 边界：本接口只负责「把故障打进去」。
       一致性判定 / 根因诊断 / 写 assurance.* 表由 assurance_mvp.py 负责。
    """
    data = request.get_json(force=True, silent=True) or {}
    fault_type = data.get('fault_type')
    target = data.get('target', '')
    params = data.get('params') or {}
    mode = data.get('mode', 'test')

    try:
        result = fault_api.FAULT_API.inject(fault_type, target, params, mode)
    except fault_api.FaultInjectionError as e:
        logger.warning(f'fault injection failed: {e}')
        metrics.FAULT_INJECTIONS_TOTAL.inc({
            'fault_type': str(fault_type), 'result': 'error'})
        return jsonify({'status': 'error', 'error': str(e),
                        'fault_type': fault_type, 'target': target}), 400
    except Exception as e:  # noqa: BLE001 — 不能让一个测试接口打挂整个服务
        logger.exception(f'fault injection crashed: {e}')
        metrics.FAULT_INJECTIONS_TOTAL.inc({
            'fault_type': str(fault_type), 'result': 'crash'})
        return jsonify({'status': 'error', 'error': str(e)}), 500

    metrics.FAULT_INJECTIONS_TOTAL.inc({
        'fault_type': str(result.get('fault_type', fault_type)), 'result': 'ok'})
    return jsonify({'status': 'ok', 'mode': mode, **result})


@app.route('/api/v1/test/faults', methods=['GET', 'DELETE'])
def api_list_faults():
    """当前活跃故障列表（GET）/ 清理故障（DELETE）。

    DELETE 可带 ?target=s1 只清该交换机；不带则全网清理。
    """
    if request.method == 'DELETE':
        try:
            result = fault_api.FAULT_API.clear_all(request.args.get('target', ''))
        except fault_api.FaultInjectionError as e:
            return jsonify({'status': 'error', 'error': str(e)}), 400
        return jsonify({'status': 'ok', **result})
    return jsonify({'status': 'ok',
                    'active_faults': fault_api.FAULT_API.active_faults()})


# ============================================================
# OVS 详细日志（独立模块接入，不影响现有采集/上报链路）
# ============================================================

@app.route('/api/ovs/log')
def api_ovs_log():
    """OVS 详细日志查询（链路状态归因用）.

    Query:
      since: 起始时间戳（秒，float），默认 0
      until: 结束时间戳（秒），默认 now
      module: 模块过滤（ofproto/netdev/dpi/bridge 等）
      level: 级别过滤（DBG/INFO/WARN/ERR）
      keyword: 关键词过滤（msg 含）
      limit: 最多条数，默认 500

    返回:
      {
        "stats": {...OVSLogger 状态...},
        "logs": [{ts, ts_str, seq, module, level, msg}, ...]
      }
    """
    try:
        from ovs_logger import get_instance
        logger = get_instance()
    except ImportError as e:
        return jsonify({
            'error': 'ovs_logger 模块未找到',
            'detail': str(e),
        }), 500

    since = request.args.get('since', default=0, type=float)
    until = request.args.get('until', default=None, type=float)
    module = request.args.get('module', default=None, type=str)
    level = request.args.get('level', default=None, type=str)
    keyword = request.args.get('keyword', default=None, type=str)
    limit = request.args.get('limit', default=500, type=int)

    logs = logger.get_logs(
        since=since, until=until,
        module=module, level=level,
        keyword=keyword, limit=limit,
    )
    stats = logger.get_stats()

    return jsonify({
        'stats': stats,
        'count': len(logs),
        'logs': logs,
    })


@app.route('/api/ovs/log/correlate', methods=['POST'])
def api_ovs_log_correlate():
    """把 link events 跟 OVS 日志做关联（归因用）.

    Body:
      {
        "link_events": [{type, dpid, port_no, time, ...}, ...],
        "time_window": 5.0  // 默认 ±5 秒
      }

    返回:
      {
        "results": [
          {"link_event": {...}, "ovs_logs": [...], "time_window_s": 5.0},
          ...
        ]
      }
    """
    try:
        from ovs_logger import get_instance
        logger = get_instance()
    except ImportError as e:
        return jsonify({
            'error': 'ovs_logger 模块未找到',
            'detail': str(e),
        }), 500

    data = request.get_json(force=True, silent=True) or {}
    link_events = data.get('link_events', [])
    time_window = float(data.get('time_window', 5.0))

    if not isinstance(link_events, list):
        return jsonify({'error': 'link_events 必须是 list'}), 400

    results = logger.correlate_with_link_events(
        link_events=link_events,
        time_window=time_window,
    )
    return jsonify({'results': results})


@app.route('/api/ovs/log/stats')
def api_ovs_log_stats():
    """OVSLogger 采集器状态（给 dashboard 显示用）."""
    try:
        from ovs_logger import get_instance
        logger = get_instance()
    except ImportError as e:
        return jsonify({
            'error': 'ovs_logger 模块未找到',
            'detail': str(e),
        }), 500

    return jsonify(logger.get_stats())


def main():
    """主入口: 当单独运行此文件时."""
    global STATE_FILE
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', default=8080, type=int)
    parser.add_argument('--state-file', default=STATE_FILE)
    args = parser.parse_args()

    STATE_FILE = args.state_file
    # 独立运行时从文件恢复历史
    state_history.load()

    logger.info(f"🌐 State server listening on http://{args.host}:{args.port}")
    logger.info(f"   Reading from: {STATE_FILE}")
    configure_logging()   # P1-2: 统一日志配置
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
