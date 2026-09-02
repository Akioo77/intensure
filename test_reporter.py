"""单元测试：AssuranceReporter

覆盖场景：
  1. 200 OK 正常响应
  2. 404 / 400 不重试（4xx 立即返回）
  3. 409 状态机错不重试（单次返回）
  4. 500 / 网络错误指数退避重试
  5. 统计：成功/失败计数、最近延迟、状态码分布
  6. ISO 编码（中文 / Unicode）
  7. query_intent_status（GET /record/{id}）
  8. reset_stats 重置
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reporter import AssuranceReporter


PASS = '✅'
FAIL = '❌'
passed = 0
failed = 0


def check(name, condition, detail=''):
    global passed, failed
    if condition:
        print(f'  {PASS} {name}')
        passed += 1
    else:
        print(f'  {FAIL} {name}  {detail}')
        failed += 1


# ============ Mock HTTP Server ============

class MockResponseHandler(BaseHTTPRequestHandler):
    """可配置的 mock 响应。"""

    # 类变量，外部可修改
    next_status = 200
    next_body = {'status': 'ACTIVE'}
    fail_count = 0  # 剩余返回 5xx 的次数
    received_bodies = []

    def log_message(self, format, *args):
        pass

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def do_GET(self):
        if self.next_status == 200:
            self._send_json(200, {'intent_id': 'test', 'status': 'ACTIVE'})
        else:
            self._send_json(self.next_status, {'error': 'mock error'})

    def do_POST(self):
        body = self._read_body()
        MockResponseHandler.received_bodies.append(body)

        if MockResponseHandler.fail_count > 0:
            MockResponseHandler.fail_count -= 1
            self._send_json(500, {'error': 'mock 5xx'})
            return

        self._send_json(MockResponseHandler.next_status,
                        MockResponseHandler.next_body)

    def _send_json(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode('utf-8'))


def start_mock_server():
    """启动 mock server 并返回 (server, port, base_url)."""
    server = ThreadingHTTPServer(('127.0.0.1', 0), MockResponseHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f'http://127.0.0.1:{port}/api/v1/assurance'
    return server, port, base_url


def reset_mock():
    MockResponseHandler.next_status = 200
    MockResponseHandler.next_body = {'status': 'ACTIVE'}
    MockResponseHandler.fail_count = 0
    MockResponseHandler.received_bodies = []


# ============ 测试 ============

def test_200_ok():
    print('\n[1] 200 OK 正常路径')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        r = AssuranceReporter(base_url, timeout_s=2, retry=2)
        actual = {
            'intent_id': 'REQ-001-CI-001',
            'checked_at': '2026-09-03T10:05:00+08:00',
            'reachable': True,
            'expected_flow_present': True,
        }
        resp = r.check('REQ-001-CI-001', actual)
        check('status_code=200', resp['status_code'] == 200)
        check('body 透传', resp['body'].get('status') == 'ACTIVE')
        check('latency_ms > 0', resp['latency_ms'] >= 0)
        check('error=None', resp['error'] is None)
    finally:
        server.shutdown()


def test_4xx_no_retry():
    print('\n[2] 4xx 错误（除 409）不重试')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        MockResponseHandler.next_status = 404
        MockResponseHandler.next_body = {'error': 'not found'}

        r = AssuranceReporter(base_url, timeout_s=2, retry=3)
        resp = r.check('REQ-001-CI-001', {'reachable': True, 'expected_flow_present': True})
        check('404 返回', resp['status_code'] == 404)
        # 只调用一次（不重试）
        check('4xx 不重试', len(MockResponseHandler.received_bodies) == 1,
              f"received: {len(MockResponseHandler.received_bodies)}")
    finally:
        server.shutdown()


def test_409_no_retry():
    print('\n[3] 409 状态机错不重试')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        MockResponseHandler.next_status = 409
        MockResponseHandler.next_body = {'error': '必须先调用 activate()'}

        r = AssuranceReporter(base_url, timeout_s=2, retry=3)
        resp = r.check('REQ-001-CI-001', {'reachable': True, 'expected_flow_present': True})
        check('409 返回', resp['status_code'] == 409)
        check('409 不重试（一次）', len(MockResponseHandler.received_bodies) == 1)
    finally:
        server.shutdown()


def test_5xx_retry_then_success():
    print('\n[4] 5xx 重试（指数退避）')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        MockResponseHandler.fail_count = 2  # 前两次返回 500，第三次返回 200
        MockResponseHandler.next_body = {'status': 'ACTIVE'}

        r = AssuranceReporter(base_url, timeout_s=2, retry=3,
                              retry_backoff_base=0.05)  # 加速测试
        resp = r.check('REQ-001-CI-001', {'reachable': True, 'expected_flow_present': True})
        check('最终 200', resp['status_code'] == 200)
        check('调用 3 次（2 次失败 + 1 次成功）',
              len(MockResponseHandler.received_bodies) == 3,
              f"received: {len(MockResponseHandler.received_bodies)}")
    finally:
        server.shutdown()


def test_5xx_retry_exhausted():
    print('\n[5] 5xx 重试用尽')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        MockResponseHandler.fail_count = 999  # 一直 500

        r = AssuranceReporter(base_url, timeout_s=2, retry=2,
                              retry_backoff_base=0.05)
        resp = r.check('REQ-001-CI-001', {'reachable': True, 'expected_flow_present': True})
        check('最终 500', resp['status_code'] == 500)
        check('调用 3 次（1 初始 + 2 重试）',
              len(MockResponseHandler.received_bodies) == 3,
              f"received: {len(MockResponseHandler.received_bodies)}")
    finally:
        server.shutdown()


def test_stats():
    print('\n[6] 统计')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        r = AssuranceReporter(base_url, timeout_s=2, retry=1)
        # 3 次成功
        for _ in range(3):
            r.check('REQ-001-CI-001', {'reachable': True, 'expected_flow_present': True})
        # 1 次 404
        MockResponseHandler.next_status = 404
        r.check('REQ-001-CI-001', {'reachable': True, 'expected_flow_present': True})

        stats = r.get_stats()
        check('total_calls=4', stats['total_calls'] == 4, f"got: {stats['total_calls']}")
        check('success_calls=3', stats['success_calls'] == 3)
        check('failed_calls=1', stats['failed_calls'] == 1)
        check('recent_latency_ms 长度=4',
              len(stats['recent_latency_ms']) == 4)
        check('recent_status 长度=4',
              len(stats['recent_status']) == 4)
        check('last_status_code=404', stats['last_status_code'] == 404)
    finally:
        server.shutdown()


def test_unicode_payload():
    print('\n[7] Unicode / 中文 payload 编码')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        r = AssuranceReporter(base_url, timeout_s=2, retry=1)
        actual = {
            'intent_id': 'REQ-001-CI-001',
            'reachable': True,
            'expected_flow_present': True,
            'note': '测试中文 payload：财务部 → 报销系统',
        }
        resp = r.check('REQ-001-CI-001', actual)
        check('编码后正常传输', resp['status_code'] == 200)
        # 验证收到的 body 包含中文
        body = MockResponseHandler.received_bodies[0]
        check('中文 payload 正确接收',
              '财务部' in body.get('actual', {}).get('note', ''),
              f"got: {body}")
    finally:
        server.shutdown()


def test_query_intent_status():
    print('\n[8] query_intent_status（GET /record/{id}）')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        r = AssuranceReporter(base_url, timeout_s=2, retry=1)
        rec = r.query_intent_status('REQ-001-CI-001')
        check('GET 200 返回 dict', isinstance(rec, dict))
        check('包含 status 字段', 'status' in rec)
    finally:
        server.shutdown()


def test_reset_stats():
    print('\n[9] reset_stats 重置')
    reset_mock()
    server, port, base_url = start_mock_server()
    try:
        r = AssuranceReporter(base_url, timeout_s=2, retry=1)
        r.check('REQ-001-CI-001', {'reachable': True, 'expected_flow_present': True})
        r.reset_stats()
        stats = r.get_stats()
        check('total_calls=0', stats['total_calls'] == 0)
        check('success_calls=0', stats['success_calls'] == 0)
    finally:
        server.shutdown()


def main():
    print('=' * 60)
    print('AssuranceReporter 单元测试')
    print('=' * 60)
    test_200_ok()
    test_4xx_no_retry()
    test_409_no_retry()
    test_5xx_retry_then_success()
    test_5xx_retry_exhausted()
    test_stats()
    test_unicode_payload()
    test_query_intent_status()
    test_reset_stats()
    print('\n' + '=' * 60)
    print(f'测试结果: {passed} 通过 / {failed} 失败')
    print('=' * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()