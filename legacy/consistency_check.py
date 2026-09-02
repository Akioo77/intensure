"""一致性检验: 期望状态(意图) vs 实际运行状态(/api/state + 历史).

意图保障智能体核心逻辑的示例实现:
  输入: 意图 JSON (期望状态, 见 STATE_MODEL.md 第 6 节)
  输入: 实际状态 (本系统 REST API)
  输出: {intent_id, status: compliant|violated|unknown, deviations[], evidence}

用法:
  python3 consistency_check.py intent_sample.json
  python3 consistency_check.py --all          # 检查全部示例意图
  python3 consistency_check.py intent.json 2 intent2.json
"""
import json
import sys
import time
import urllib.request

API = 'http://127.0.0.1:8080'

# 端口 state bit
OFPPS_LINK_DOWN = 1 << 0
OFPP_LOCAL = 4294967294


# ============ 数据获取 ============
def _get(path):
    with urllib.request.urlopen(API + path, timeout=4) as r:
        return json.loads(r.read())


def fetch_actual():
    """拉取实际状态: 最新快照 + 最近验证 + 最近连通性探测."""
    state = _get('/api/state')
    validation = _get('/api/history?key=validation&limit=5')
    conn = _get('/api/history?key=connectivity&limit=3')
    return {'state': state, 'validation': validation, 'connectivity': conn}


# ============ 工具 ============
def _resolve_hosts(intent, group_name):
    """把端点名解析为主机列表 (优先 hosts 字段, 其次 ip_cidr 匹配)."""
    eps = intent.get('semantics', {}).get('endpoints', [])
    for ep in eps:
        if ep.get('name') == group_name:
            if ep.get('hosts'):
                return set(ep['hosts'])
            cidrs = ep.get('ip_cidr', [])
            if cidrs:
                # 匹配实际状态里 IP 前缀 (简化: 取 /24 前 3 段)
                found = set()
                for mac, h in fetch_actual()['state']['hosts'].items():
                    for ip in h.get('ipv4', []):
                        if any(ip.startswith(cidr.rsplit('.', 1)[0] + '.')
                               for cidr in cidrs):
                            found.add('h' + ip.split('.')[-1])
                return found
    return set()


def _host_actual(host_name):
    """host 名 (h1) → 实际主机条目."""
    ip = '10.0.0.' + host_name[1:]
    state = fetch_actual()['state']
    for mac, h in state['hosts'].items():
        if ip in h.get('ipv4', []):
            return h
    return None


def _port_state(state, dpid, port_name):
    for p in state['switches'].get(dpid, {}).get('ports', []):
        if p.get('name') == port_name:
            return p
    return None


def _link_ports(link):
    """'s1-s2' → (dpid1, port1, dpid2, port2). 端口名约定: <sw>-eth3."""
    a, b = link.split('-')
    return (format(int(a[1:]), '016x'), f'{a}-eth3',
            format(int(b[1:]), '016x'), f'{b}-eth3')


# ============ 断言检查 ============
def check_connectivity(state, conn_hist, assertion, intent, devs):
    src, dst = assertion['src'], assertion['dst']
    allow = assertion.get('allow', True)
    src_hosts = _resolve_hosts(intent, src)
    dst_hosts = _resolve_hosts(intent, dst)
    if not src_hosts or not dst_hosts:
        devs.append({'type': 'connectivity', 'assertion': assertion,
                     'message': f'端点 {src}/{dst} 无法解析为主机',
                     'severity': 'warning'})
        return
    # 全部主机是否都在
    missing = [h for h in src_hosts | dst_hosts if _host_actual(h) is None]
    if missing:
        devs.append({'type': 'connectivity', 'assertion': assertion,
                     'message': f'主机缺失: {missing}', 'severity': 'critical'})
        return
    # 连通性: 用最近探测
    last = conn_hist[-1] if conn_hist else None
    if allow:
        if last is None:
            devs.append({'type': 'connectivity', 'assertion': assertion,
                         'message': '无连通性探测数据', 'severity': 'warning'})
        elif last.get('loss_pct', 0) > 0:
            devs.append({'type': 'connectivity', 'assertion': assertion,
                         'message': f'探测丢包 {last["loss_pct"]}%',
                         'actual': last['loss_pct'], 'severity': 'critical'})
    else:
        # 隔离近似判定: 两端主机如果在同一交换机 → 可能未隔离
        for sh in src_hosts:
            for dh in dst_hosts:
                sa, da = _host_actual(sh), _host_actual(dh)
                if sa and da and sa.get('dpid') == da.get('dpid'):
                    devs.append({'type': 'connectivity', 'assertion': assertion,
                                 'message': f'{sh}/{dh} 在同一交换机, 隔离存疑',
                                 'severity': 'warning'})


def check_link_status(state, assertion, devs):
    link = assertion['link']
    want_up = assertion.get('state', 'up') == 'up'
    d1, p1, d2, p2 = _link_ports(link)
    pa = _port_state(state, d1, p1)
    pb = _port_state(state, d2, p2)
    if pa is None or pb is None:
        devs.append({'type': 'link_status', 'link': link,
                     'message': f'端口 {p1}/{p2} 未发现', 'severity': 'warning'})
        return
    up = (pa['state'] & OFPPS_LINK_DOWN == 0) and (pb['state'] & OFPPS_LINK_DOWN == 0)
    actual = 'up' if up else 'down'
    if up != want_up:
        devs.append({'type': 'link_status', 'link': link,
                     'expected': assertion.get('state'), 'actual': actual,
                     'message': f'{link} 期望 {assertion.get("state")}, 实际 {actual}',
                     'severity': 'critical'})


def check_qos(state, conn_hist, assertion, devs):
    last = conn_hist[-1] if conn_hist else None
    if assertion.get('max_loss_pct') is not None and last:
        if last.get('loss_pct', 0) > assertion['max_loss_pct']:
            devs.append({'type': 'qos', 'assertion': assertion,
                         'message': f'丢包 {last["loss_pct"]}% 超限 {assertion["max_loss_pct"]}%',
                         'severity': 'critical'})
    if assertion.get('max_latency_ms') is not None:
        lat = last.get('latency_ms') if last else None
        if lat is None:
            devs.append({'type': 'qos', 'assertion': assertion,
                         'message': '无时延测量数据(需主动探测)', 'severity': 'info'})
        elif lat > assertion['max_latency_ms']:
            devs.append({'type': 'qos', 'assertion': assertion,
                         'message': f'时延 {lat}ms 超限 {assertion["max_latency_ms"]}ms',
                         'actual': lat, 'severity': 'critical'})


def check_intent(intent):
    """对单个意图做一致性检验."""
    actual = fetch_actual()
    state = actual['state']
    conn_hist = actual['connectivity']
    devs = []

    ts = intent.get('target_state', {})
    for a in ts.get('connectivity', []):
        check_connectivity(state, conn_hist, a, intent, devs)
    for a in ts.get('link_status', []):
        check_link_status(state, a, devs)
    for a in ts.get('qos', []):
        check_qos(state, conn_hist, a, devs)

    errors = [d for d in devs if d['severity'] == 'critical']
    warnings = [d for d in devs if d['severity'] == 'warning']
    status = ('violated' if errors else
              'compliant' if not warnings else 'unknown')

    return {
        'intent_id': intent.get('intent_id', '?'),
        'user_statement': intent.get('user_statement', ''),
        'status': status,
        'checked_at': time.time(),
        'deviation_count': len(devs),
        'critical': len(errors),
        'warning': len(warnings),
        'deviations': devs,
        'evidence': {
            'switches': len(state['switches']),
            'hosts': len(state['hosts']),
            'flows': sum(len(v) for v in state['flows'].values()),
            'ports_down': sum(1 for sw in state['switches'].values()
                              for p in sw['ports']
                              if p['port_no'] != OFPP_LOCAL and p['state'] & OFPPS_LINK_DOWN),
            'last_probe': conn_hist[-1] if conn_hist else None,
        },
    }


def main():
    args = sys.argv[1:]
    if not args or '--all' in args:
        files = ['intent_sample.json']
    else:
        files = args
    results = []
    for f in files:
        with open(f) as fp:
            data = json.load(fp)
        intents = data if isinstance(data, list) else [data]
        for it in intents:
            results.append(check_intent(it))
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
