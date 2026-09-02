"""Flask REST 服务：暴露网络状态 + 验证结果 + 意图管理 + 自愈引擎。

端点:
  GET  /api/state            完整状态
  GET  /api/switches         交换机列表
  GET  /api/hosts            主机列表
  GET  /api/port_stats       端口统计
  GET  /api/flows            流表
  GET  /api/events           事件流
  GET  /api/validate         跑状态验证（返回问题列表）
  GET  /api/health           健康检查

  意图与冲突（Phase 3 Feature 1）：
  GET  /api/intents                所有意图
  POST /api/intents                添加意图（自动冲突检测）
  POST /api/intents/check          仅检测冲突（不添加）
  GET  /api/conflicts              当前活跃冲突
  POST /api/conflicts/resolve      提交消解决策
  POST /api/intents/<id>/revoke    撤销意图

  自愈（Phase 3 Feature 2）：
  GET  /api/healing/status         当前自愈状态汇总
  GET  /api/healing/history        自愈历史
  GET  /api/healing/cycle/<id>     单个 cycle 详情
  POST /api/healing/trigger        手动触发自愈（测试用）
  POST /api/healing/ack            上游确认推送
  GET  /api/healing/upstream       上游待 ack 队列
"""
import json
import os
import sys
import time

import state_history

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

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
    })


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
# Phase 3 Feature 1: 意图管理 + 冲突检测
# ============================================================

@app.route('/api/intents', methods=['GET'])
def api_intents_list():
    """列出所有意图. ?include_inactive=true 含已撤销."""
    import intent_registry
    intent_registry.load()  # 启动时加载
    include_inactive = request.args.get('include_inactive', 'false').lower() == 'true'
    intents = intent_registry.list_all(include_inactive=include_inactive)
    return jsonify({
        'intents': intents,
        'count': len(intents),
        'stats': intent_registry.stats(),
    })


@app.route('/api/intents', methods=['POST'])
def api_intents_add():
    """添加新意图. 自动冲突检测.

    body: 一个完整的意图 dict
    返回: {"intent_id": "...", "conflicts": [...], "status": "active|rejected"}
    """
    import intent_registry
    intent = request.get_json(force=True, silent=True) or {}
    if not intent.get('semantics', {}).get('type'):
        return jsonify({'status': 'error', 'error': 'semantics.type 必填'}), 400
    result = intent_registry.add(intent, check_conflicts=True)
    return jsonify(result)


@app.route('/api/intents/check', methods=['POST'])
def api_intents_check():
    """仅检测冲突，不添加（dry run）.

    body: 一个意图 dict
    返回: {"conflicts": [...], "summary": {...}}
    """
    import intent_registry, conflict_detector
    intent_registry.load()
    intent = request.get_json(force=True, silent=True) or {}
    topology = intent_registry._current_topology()
    existing = intent_registry.list_all()
    conflicts = conflict_detector.detect_conflicts(intent, existing, topology)
    return jsonify({
        'conflicts': conflicts,
        'summary': conflict_detector.summarize(conflicts),
        'has_conflict': len(conflicts) > 0,
    })


@app.route('/api/intents/bulk_load', methods=['POST'])
def api_intents_bulk_load():
    """批量加载意图列表. body: {"intents": [...]}

    返回: {"loaded": N, "results": [{intent_id, status, conflicts}]}
    """
    import intent_registry
    intent_registry.load()
    data = request.get_json(force=True, silent=True) or {}
    intents = data.get('intents', [])
    if not isinstance(intents, list):
        return jsonify({'status': 'error', 'error': 'intents 必须是列表'}), 400

    # 先清空所有现有意图
    intent_registry.clear()

    results = []
    for intent in intents:
        try:
            res = intent_registry.add(intent, check_conflicts=True)
            results.append({
                'intent_id': res['intent_id'],
                'status': res['status'],
                'conflict_count': len(res.get('conflicts', [])),
            })
        except Exception as e:
            results.append({
                'intent_id': intent.get('intent_id', '?'),
                'status': 'error',
                'error': str(e),
            })

    return jsonify({
        'status': 'ok',
        'loaded': len(results),
        'results': results,
        'summary': intent_registry.stats(),
    })


@app.route('/api/intents/matrix', methods=['GET'])
def api_intents_matrix():
    """意图间冲突矩阵: NxN. cell[i][j] = True if intent i conflicts with j.

    返回: {
      'intents': [list of intent_ids],
      'matrix': [[bool]],
      'conflict_count': int,
      'by_pair': [{a, b, conflict_type, severity}, ...]
    }
    """
    import intent_registry, conflict_detector
    intent_registry.load()
    active = intent_registry.list_all()
    topology = intent_registry._current_topology()

    intent_ids = [i['intent_id'] for i in active]
    n = len(intent_ids)
    id_to_idx = {iid: idx for idx, iid in enumerate(intent_ids)}

    # 计算冲突矩阵
    matrix = [[False] * n for _ in range(n)]
    by_pair = []
    seen_pairs = set()

    for idx, intent in enumerate(active):
        others = [i for j, i in enumerate(active) if j != idx]
        conflicts = conflict_detector.detect_conflicts(intent, others, topology)
        for c in conflicts:
            other_id = c['with_intent']
            if other_id in id_to_idx:
                other_idx = id_to_idx[other_id]
                matrix[idx][other_idx] = True
                matrix[other_idx][idx] = True
                pair_key = tuple(sorted([intent['intent_id'], other_id]))
                if pair_key not in seen_pairs:
                    seen_pairs.add(pair_key)
                    by_pair.append({
                        'a': intent['intent_id'],
                        'b': other_id,
                        'type': c['type'],
                        'severity': c['severity'],
                        'description': c['description'],
                    })

    return jsonify({
        'intents': intent_ids,
        'matrix': matrix,
        'conflict_count': len(by_pair),
        'by_pair': by_pair,
    })


@app.route('/api/intents/<intent_id>/revoke', methods=['POST'])
def api_intents_revoke(intent_id):
    """撤销意图."""
    import intent_registry
    intent_registry.load()
    data = request.get_json(force=True, silent=True) or {}
    reason = data.get('reason', 'revoked')
    return jsonify(intent_registry.remove(intent_id, reason=reason))


@app.route('/api/conflicts', methods=['GET'])
def api_conflicts_list():
    """列出所有活跃冲突（从当前意图中提取）."""
    import intent_registry, conflict_detector
    intent_registry.load()
    active = intent_registry.list_all()
    all_conflicts = []
    for intent in active:
        for c in intent.get('conflicts', []):
            all_conflicts.append({
                **c,
                'in_intent': intent['intent_id'],
            })
    return jsonify({
        'conflicts': all_conflicts,
        'summary': conflict_detector.summarize(all_conflicts),
    })


@app.route('/api/conflicts/suggest', methods=['POST'])
def api_conflicts_suggest():
    """对一个新意图生成自动消解建议.

    body: 意图 dict
    返回: {
      'conflicts': [...],
      'resolutions': [...],
      'recommendation': 'accept' | 'modify' | 'reject',
      'summary': {...},
      'has_conflict': bool
    }
    """
    import intent_registry, conflict_resolver
    intent_registry.load()
    intent = request.get_json(force=True, silent=True) or {}
    existing = intent_registry.list_all()
    result = conflict_resolver.suggest_resolution(intent, existing)
    return jsonify(result)


@app.route('/api/conflicts/resolve', methods=['POST'])
def api_conflicts_resolve():
    """提交冲突消解决策.

    body: {"strategy": "priority|recency|negotiate|reject",
           "intent_id": "intent-xxx",
           "action": "keep|revoke|modify",
           "modifications": {...} (可选)}
    """
    import intent_registry
    intent_registry.load()
    data = request.get_json(force=True, silent=True) or {}
    intent_id = data.get('intent_id')
    action = data.get('action', 'keep')
    strategy = data.get('strategy', 'priority')

    if not intent_id:
        return jsonify({'status': 'error', 'error': 'intent_id 必填'}), 400

    if action == 'revoke':
        return jsonify(intent_registry.remove(intent_id, reason='superseded'))

    if action == 'modify':
        # 修改意图（目前简化: 撤销旧的 + 添加新的）
        new_intent = data.get('modifications', {})
        new_intent['intent_id'] = new_intent.get('intent_id', intent_id)
        intent_registry.remove(intent_id, reason='superseded')
        return jsonify(intent_registry.add(new_intent, check_conflicts=True))

    return jsonify({
        'status': 'ok',
        'strategy': strategy,
        'action': action,
        'intent_id': intent_id,
    })


# ============================================================
# Phase 3 Feature 2: 自愈引擎
# ============================================================

@app.route('/api/healing/status', methods=['GET'])
def api_healing_status():
    """当前自愈状态汇总."""
    from healing_engine import get_engine
    eng = get_engine()
    return jsonify(eng.status())


@app.route('/api/healing/history', methods=['GET'])
def api_healing_history():
    """自愈历史. ?limit=N."""
    from healing_engine import get_engine
    eng = get_engine()
    limit = request.args.get('limit', default=20, type=int)
    return jsonify({
        'history': eng.history(limit),
        'count': len(eng.history(limit)),
    })


@app.route('/api/healing/cycle/<cycle_id>', methods=['GET'])
def api_healing_cycle(cycle_id):
    """单个 cycle 详情."""
    from healing_engine import get_engine
    eng = get_engine()
    cycle = eng.get_cycle(cycle_id)
    if not cycle:
        return jsonify({'status': 'not_found', 'cycle_id': cycle_id}), 404
    return jsonify(cycle)


@app.route('/api/healing/trigger', methods=['POST'])
def api_healing_trigger():
    """手动触发自愈（测试用）.

    body: {"intent_id": "intent-xxx", "deviation": {...}}
    """
    from healing_engine import get_engine
    import intent_registry
    eng = get_engine()
    data = request.get_json(force=True, silent=True) or {}
    intent_id = data.get('intent_id')
    deviation = data.get('deviation')
    if not intent_id or not deviation:
        return jsonify({'status': 'error', 'error': 'intent_id 和 deviation 必填'}), 400
    intent_registry.load()
    intent = intent_registry.get(intent_id)
    if not intent:
        return jsonify({'status': 'error', 'error': f'意图 {intent_id} 不存在'}), 404
    cycle_id = eng.trigger_manual(intent, deviation)
    return jsonify({
        'status': 'ok',
        'cycle_id': cycle_id,
        'message': f'已手动触发自愈 cycle {cycle_id}',
    })


@app.route('/api/healing/ack', methods=['POST'])
def api_healing_ack():
    """上游确认收到推送.

    body: {"cycle_id": "heal-xxx"}
    """
    from healing_engine import get_engine
    eng = get_engine()
    data = request.get_json(force=True, silent=True) or {}
    cycle_id = data.get('cycle_id')
    if not cycle_id:
        return jsonify({'status': 'error', 'error': 'cycle_id 必填'}), 400
    return jsonify(eng.ack(cycle_id))


@app.route('/api/healing/upstream', methods=['GET'])
def api_healing_upstream():
    """上游待 ack 的自愈意图（让同学 B/C 能看到推送）."""
    from healing_engine import get_engine
    eng = get_engine()
    return jsonify({
        'pending': eng.upstream_buffer_snapshot(),
        'count': len(eng.upstream_buffer_snapshot()),
    })


# ============================================================
# Phase 3 Feature 5: 异常检测
# ============================================================

@app.route('/api/anomalies', methods=['GET'])
def api_anomalies():
    """检测异常. 汇总 4 类: latency_spike / loss_burst / traffic_anomaly / link_flapping."""
    import anomaly_detector
    state = get_state()
    summary_hist = state_history.get('summary', 100)
    conn_hist = state_history.get('connectivity', 50)
    events = state.get('events', [])[-200:]

    anomalies = anomaly_detector.detect_all(
        state=state,
        history=summary_hist,
        events=events,
        connectivity=conn_hist,
    )
    return jsonify({
        'anomalies': anomalies,
        'summary': anomaly_detector.summarize(anomalies),
    })


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

    print(f"🌐 State server listening on http://{args.host}:{args.port}")
    print(f"   Reading from: {STATE_FILE}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
