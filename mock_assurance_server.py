"""本地 Mock 保障模块（用于 integration_demo.py --mode mock 离线调试）。

最小实现：监听 8000 端口，提供 4 个端点（支持 /api/v1/assurance 前缀）：
  POST /register-request  → 创建意图记录（内存）
  POST /activate          → 标记为 ACTIVE
  POST /check             → 检查 ActualState，返回 status
  POST /verify            → 复测，返回 RECOVERED/FAILED
  GET  /record/{id}       → 查询意图当前状态
  GET  /docs              → 提示（不是真的 Swagger）

启动: python3 mock_assurance_server.py --port 8000
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
                ],
            })
        elif path == '/' or path == '/health':
            self._send_json(200, {'status': 'ok', 'mock': True,
                                  'intents_count': len(INTENTS_DB)})
        else:
            self._send_json(404, {'error': 'not found', 'path': self.path})

    # ---------- POST ----------
    def do_POST(self):
        body = self._read_body()
        path = strip_prefix(self.path)

        if path == '/register-request':
            return self._handle_register(body)
        elif path == '/activate':
            return self._handle_activate(body)
        elif path == '/check':
            return self._handle_check(body)
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

            self._send_json(200, {
                'intent_id': intent_id,
                'status': new_status,
                'violated': violated,
                'violations': violations,
                'check_count': rec['check_count'],
            })

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
    parser = argparse.ArgumentParser(description='Mock 保障模块（本地调试用）')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f'Mock 保障模块启动: http://{args.host}:{args.port}', flush=True)
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