"""本地 Mock 保障模块（离线调试 + **联调前的自检工具**）。

启动: python3 mock_assurance_server.py --port 8000
      python3 mock_assurance_server.py --version 0.9.0   # 模拟版本不兼容，验证降级

⚠️ 2026-11-07 补齐：以前这个 mock 只实现了 5 个端点，且注册接口是
「注册意图树」形态（`/register-request` + 嵌套 body），跟演示脚本用的
「注册单个意图」形态对不上 → **脚本对着 mock 根本跑不通**，只能当离线演示。
现在它与 `demo_scripts/01~08` 完全对齐，可以当**联调前的回归工具**用：

    python3 mock_assurance_server.py --port 8000 &
    export ASSURANCE_BASE=http://127.0.0.1:8000
    cd demo_scripts && for s in 01_register_intent 02_collect_state \
        03_check_baseline 04_inject_fault 05_check_violated \
        06_diagnose 07_heal 08_verify_recovered; do bash $s.sh; done

提供端点（前缀 /api/v1/assurance）:
  POST /register            → 注册单个意图（body: {"intent":{...},...}）
  POST /register-request    → 注册意图树（body: {"request":{"child_intents":[...]}}）
  POST /activate            → 标记为 ACTIVE
  POST /check               → 一致性检查，返回 status + violations
  POST /diagnose?intent_id= → 根因诊断（status=DIAGNOSING + root_causes）
  POST /heal?intent_id=     → 自愈决策（verdict=APPROVED/BLOCKED + guard_checks）
  POST /verify              → 复测，返回 RECOVERED/FAILED
  GET  /record/{id}         → 查询意图当前状态
  GET  /docs                → 提示（不是真的 Swagger）
  GET  /api/health          → 健康检查（带 version 字段，契约版本协商用）
"""
import argparse
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# 内存数据库
INTENTS_DB = {}
INTENTS_LOCK = threading.Lock()

# 路径前缀（让 mock 端点跟真实保障模块一致）
PATH_PREFIXES = ['/api/v1/assurance', '/api/v1', '/api']

# 模拟的对方版本（2026-11-10 对齐：对方真实服务版本是 0.4.1）
MOCK_VERSION = '0.4.1'


def strip_prefix(path: str) -> str:
    """去掉 /api/v1/assurance 等前缀。"""
    for prefix in PATH_PREFIXES:
        if path.startswith(prefix + '/'):
            return path[len(prefix):]
    return path


class MockHandler(BaseHTTPRequestHandler):
    """Mock HTTP 处理器。"""

    def log_message(self, format, *args):
        """安静模式：不打印每条请求。"""
        pass

    def _send_json(self, status_code: int, body: dict):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(body, ensure_ascii=False).encode('utf-8'))

    def _read_body(self) -> dict:
        length = int(self.headers.get('Content-Length', 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        path = strip_prefix(self.path)

        if path.startswith('/record/'):
            intent_id = path[len('/record/'):]
            with INTENTS_LOCK:
                rec = INTENTS_DB.get(intent_id)
            if rec:
                self._send_json(200, rec)
            else:
                self._send_json(404, {'error': f'Intent {intent_id!r} not found'})
        elif path == '/docs' or path == '/docs/':
            self._send_json(200, {
                'message': 'Mock 保障模块（integration_demo.py --mode mock 用）',
                'endpoints': [
                    'POST /api/v1/assurance/register-request',
                    'POST /api/v1/assurance/activate',
                    'POST /api/v1/assurance/check',
                    'POST /api/v1/assurance/verify',
                    'GET  /api/v1/assurance/record/{intent_id}',
                    'GET  /api/health',
                ],
            })
        elif path == '/' or path == '/health':
            self._send_json(200, {'status': 'ok', 'mock': True,
                                  'version': MOCK_VERSION,
                                  'intents_count': len(INTENTS_DB)})
        else:
            self._send_json(404, {'error': 'not found', 'path': self.path})

    # ---------- POST ----------
    def do_POST(self):
        body = self._read_body()
        # ⚠️ 路径里可能带 query string（`/diagnose?intent_id=xxx`）——
        #    strip_prefix 之前要把 query 摘出来，否则 diagnose/heal 拿不到 intent_id
        raw_path, _, raw_query = self.path.partition('?')
        path = strip_prefix(raw_path)
        if raw_query:
            from urllib.parse import parse_qs
            for key, values in parse_qs(raw_query).items():
                body.setdefault(key, values[0] if values else '')

        if path == '/register-request':
            return self._handle_register(body)
        elif path == '/register':
            # 单意图注册（demo_scripts/01_register_intent.sh 用的形态）：
            # body = {"intent": {...}, "deployment_id": ..., "devices": [...]}
            return self._handle_register_intent(body)
        elif path == '/activate':
            return self._handle_activate(body)
        elif path == '/check':
            return self._handle_check(body)
        elif path == '/diagnose':
            return self._handle_diagnose(body)
        elif path == '/heal':
            return self._handle_heal(body)
        elif path == '/verify':
            return self._handle_verify(body)
        else:
            self._send_json(404, {'error': 'not found', 'path': self.path})

    # ---------- 业务逻辑 ----------
    def _handle_register(self, body: dict):
        request = body.get('request', {})
        deployment_id = body.get('deployment_id', 'unknown')

        registered = []
        for child in request.get('child_intents', []):
            cid = child.get('child_intent_id')
            if not cid:
                continue
            with INTENTS_LOCK:
                INTENTS_DB[cid] = {
                    'intent_id': cid,
                    'status': 'REGISTERED',
                    'deployment_id': deployment_id,
                    'request_id': request.get('request_id'),
                    'child_intent': child,
                    'check_count': 0,
                    'last_check_status': None,
                    'last_check_at': None,
                    'recovered_at': None,
                    'created_at': time.time(),
                }
            registered.append(cid)

        self._send_json(200, {
            'registered': registered,
            'count': len(registered),
        })

    def _handle_register_intent(self, body: dict):
        """注册**单个意图**（demo_scripts/01_register_intent.sh 的形态）。

        与 `/register-request` 的区别：
          · `/register-request` —— 注册一棵**意图树**（翻译模块提交用，
            body 里是 request.child_intents[]）
          · `/register`         —— 注册**一个意图**（联调脚本用，
            body 里是 intent 对象）
        对方 0.4.0 到底给的是哪个形态，联调前必须确认（见
        docs/PARTNER_COORDINATION.md §2.4）。
        """
        intent = body.get('intent') or {}
        intent_id = intent.get('intent_id')
        if not intent_id:
            self._send_json(400, {'error': 'intent.intent_id required'})
            return
        with INTENTS_LOCK:
            INTENTS_DB[intent_id] = {
                'intent_id': intent_id,
                'status': 'REGISTERED',
                'deployment_id': body.get('deployment_id', 'unknown'),
                'intent': intent,
                'check_count': 0,
                'last_check_status': None,
                'last_violations': [],
                'last_check_at': None,
                'recovered_at': None,
                'created_at': time.time(),
            }
        self._send_json(200, {
            'intent_id': intent_id,
            'status': 'REGISTERED',
            'registered': [intent_id],
            'count': 1,
        })

    def _handle_activate(self, body: dict):
        intent_id = body.get('intent_id')
        if not intent_id:
            self._send_json(400, {'error': 'intent_id required'})
            return
        with INTENTS_LOCK:
            rec = INTENTS_DB.get(intent_id)
            if not rec:
                self._send_json(404, {'error': f'Intent {intent_id!r} not found'})
                return
            if rec['status'] == 'REGISTERED':
                rec['status'] = 'ACTIVE'
                rec['activated_at'] = time.time()
                self._send_json(200, {'intent_id': intent_id, 'status': 'ACTIVE'})
            elif rec['status'] == 'ACTIVE':
                self._send_json(200, {'intent_id': intent_id, 'status': 'ACTIVE',
                                      'note': 'already active'})
            else:
                self._send_json(409, {
                    'error': f'Intent {intent_id!r} in state {rec["status"]!r}, '
                             f'cannot activate',
                    'current_status': rec['status'],
                })

    def _handle_check(self, body: dict):
        intent_id = body.get('intent_id')
        actual = body.get('actual', {})

        with INTENTS_LOCK:
            rec = INTENTS_DB.get(intent_id)
            if not rec:
                self._send_json(404, {'error': f'Intent {intent_id!r} not found'})
                return
            # 只在 REGISTERED 状态拒绝（必须先 activate）
            # ACTIVE / VIOLATED / RECOVERED 都接受 check（真实保障模块的设计）
            if rec['status'] in ('REGISTERED',):
                self._send_json(409, {
                    'error': f'必须先调用 activate()（当前 status={rec["status"]!r}）',
                    'current_status': rec['status'],
                })
                return

            # 一致性检查（mock 版）
            rec['check_count'] += 1
            rec['last_check_at'] = time.time()

            reachable = actual.get('reachable', False)
            expected_flow_present = actual.get('expected_flow_present', True)
            down_links = actual.get('down_links', [])

            violations = []
            if not reachable:
                violations.append({
                    'type': 'REACHABILITY_FAILURE',
                    'severity': 'high',
                    'message': '源到目的不可达',
                })
            if not expected_flow_present:
                violations.append({
                    'type': 'POLICY_DRIFT',
                    'severity': 'high',
                    'message': '对应流表条目不存在',
                })
            if down_links:
                violations.append({
                    'type': 'LINK_DOWN',
                    'severity': 'high',
                    'message': f'链路 down: {down_links}',
                    'down_links': down_links,
                })

            violated = len(violations) > 0
            new_status = 'VIOLATED' if violated else 'ACTIVE'
            rec['status'] = new_status
            rec['last_check_status'] = new_status
            rec['last_violations'] = violations

            self._send_json(200, {
                'intent_id': intent_id,
                'status': new_status,
                'violated': violated,
                'violations': violations,
                'check_count': rec['check_count'],
            })

    def _handle_diagnose(self, body: dict):
        """根因诊断（demo_scripts/06_diagnose.sh 期望 status=DIAGNOSING）。"""
        intent_id = body.get('intent_id')
        with INTENTS_LOCK:
            rec = INTENTS_DB.get(intent_id)
            if not rec:
                self._send_json(404, {'error': f'Intent {intent_id!r} not found'})
                return
            violations = rec.get('last_violations') or []
            causes = []
            for v in violations:
                if v.get('type') == 'REACHABILITY_FAILURE':
                    causes.append({'type': 'port_failure', 'device': 's1',
                                   'confidence': 0.85,
                                   'description': '链路不可达，疑似端口或对端故障'})
                elif v.get('type') == 'POLICY_DRIFT':
                    causes.append({'type': 'policy_drift', 'device': 's1',
                                   'confidence': 0.8,
                                   'description': '期望流表条目缺失'})
                elif v.get('type') == 'LINK_DOWN':
                    causes.append({'type': 'port_failure', 'device': 's1',
                                   'confidence': 0.9,
                                   'description': f"链路 down: {v.get('down_links')}"})
            if not causes:
                causes.append({'type': 'no_fault_detected', 'device': None,
                               'confidence': 0.3,
                               'description': '最近一次检查没有违例'})
            rec['status'] = 'DIAGNOSING'
            self._send_json(200, {
                'status': 'DIAGNOSING',
                'intent_id': intent_id,
                'root_causes': causes,
                'confidence': max([c['confidence'] for c in causes]),
                'candidates': [c['type'] for c in causes],
                'strategy': '先恢复链路，再重下发流表',
            })

    def _handle_heal(self, body: dict):
        """自愈决策。

        ⚠️ 口径对齐（2026-11-10，对方明确）：
        自愈结论字段是 **APPROVED / BLOCKED**，不是 ALLOW。
        （注意：别跟意图里的 `action: "ALLOW"` 搞混 —— 那是意图的动作类型，
          跟自愈结论是两个字段。）

        之前这里返回 ALLOW，是我方 mock 跟对方真实接口对不上：
        演示脚本对真接口跑时，判定会落到「未知决策」分支。
        """
        intent_id = body.get('intent_id')
        with INTENTS_LOCK:
            rec = INTENTS_DB.get(intent_id)
            if not rec:
                self._send_json(404, {'error': f'Intent {intent_id!r} not found'})
                return
            # 闸门：最近一次检查有违例才允许自愈（模拟对方的 guard 规则）
            violated = bool(rec.get('last_violations'))
            verdict = 'APPROVED' if violated else 'BLOCKED'
            guard_checks = [
                {'id': 'G1', 'name': '有未恢复的违例', 'passed': violated},
                {'id': 'G2', 'name': '目标设备在采集侧可见', 'passed': True},
                {'id': 'G3', 'name': '修复动作在允许的编排内', 'passed': True},
                {'id': 'G4', 'name': '没有正在进行的同目标自愈', 'passed': True},
            ]
            payload = {
                'status': 'HEALING' if violated else 'ACTIVE',
                'intent_id': intent_id,
                'verdict': verdict,
                'guard_checks': guard_checks,
                'healing_intent': {
                    'intent_id': f'heal-{intent_id}',
                    'goal': '恢复链路可达性',
                    'primitives': ['link_up', 'reinstall_flow'],
                } if violated else None,
            }
            if not violated:
                payload['blocked_by'] = ['G1: 没有需要修复的违例']
            rec['status'] = payload['status']
            self._send_json(200, payload)

    def _handle_verify(self, body: dict):
        intent_id = body.get('intent_id')
        actual_after_heal = body.get('actual_after_heal', {})

        with INTENTS_LOCK:
            rec = INTENTS_DB.get(intent_id)
            if not rec:
                self._send_json(404, {'error': f'Intent {intent_id!r} not found'})
                return

            # ⚠️ 真实保障模块 verify 端点不限状态（只验证 actual_after_heal）
            # 不检查 rec['status']，允许在 ACTIVE / VIOLATED 下都能 verify

            reachable = actual_after_heal.get('reachable', False)
            expected_flow_present = actual_after_heal.get('expected_flow_present', True)
            down_links = actual_after_heal.get('down_links', [])

            recovered = reachable and expected_flow_present and not down_links
            new_status = 'RECOVERED' if recovered else 'FAILED'
            rec['status'] = new_status
            if recovered:
                rec['recovered_at'] = time.time()
            else:
                rec['failed_at'] = time.time()
            rec['verify_count'] = rec.get('verify_count', 0) + 1

            self._send_json(200, {
                'intent_id': intent_id,
                'status': new_status,
                'recovered': recovered,
                'verify_count': rec['verify_count'],
            })

    # ---------- CORS preflight ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


def main():
    global MOCK_VERSION

    parser = argparse.ArgumentParser(description='Mock 保障模块（本地调试用）')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--version', default=MOCK_VERSION,
                        help='模拟的对方版本号（用于验证契约版本协商）')
    args = parser.parse_args()

    MOCK_VERSION = args.version

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f'Mock 保障模块启动: http://{args.host}:{args.port}', flush=True)
    print(f'  - 模拟对方版本: {MOCK_VERSION}', flush=True)
    print('  - POST /api/v1/assurance/register-request  注册意图', flush=True)
    print('  - POST /api/v1/assurance/activate          激活意图', flush=True)
    print('  - POST /api/v1/assurance/check             上报 ActualState', flush=True)
    print('  - POST /api/v1/assurance/verify            复测上报', flush=True)
    print('  - GET  /api/v1/assurance/record/{id}       查询意图', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n收到 Ctrl+C，关闭 mock 服务...', flush=True)
        server.shutdown()


if __name__ == '__main__':
    main()
