"""单元测试：跨模块契约版本管理（v3.18.0 · P3-3）

覆盖场景：
  1. 对方版本 == 期望版本        → ok，允许上报
  2. 补丁级差异（0.4.1 vs 0.4.0）→ compatible，允许上报
  3. 次版本不一致（0.9.0）       → mismatch，阻断上报（服务器收不到 POST）
  4. 对方没暴露版本字段          → unknown，默认兼容模式，允许上报
  5. 严格模式 + 对方没版本字段    → 阻断上报
  6. 对方连不上                  → unreachable，不因版本阻断（让真实请求报错）
  7. 版本号从嵌套字段里取
  8. 版本检查结果有 TTL 缓存（不每次推送都打 /api/health）

运行: python3 -m pytest test_contract_version.py -v
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assurance_bridge import (  # noqa: E402
    AssuranceBridge,
    ContractState,
    _extract_version,
    _version_key,
)


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'{name}  {detail}')


# ============ Mock 保障模块 ============

class MockAssuranceHandler(BaseHTTPRequestHandler):
    """可配置的 mock 保障模块：/api/health 的返回体可调。"""

    health_body = {'status': 'ok', 'version': '0.4.0'}
    health_status = 200
    posted_paths = []
    health_hits = 0

    def log_message(self, format, *args):
        pass

    def _send_json(self, status_code, body):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode('utf-8'))

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def do_GET(self):
        if self.path.endswith('/api/health') or self.path.endswith('/health'):
            MockAssuranceHandler.health_hits += 1
            self._send_json(MockAssuranceHandler.health_status,
                            MockAssuranceHandler.health_body)
        else:
            self._send_json(404, {'error': 'not found'})

    def do_POST(self):
        body = self._read_body()
        MockAssuranceHandler.posted_paths.append(self.path)
        self._send_json(200, {'status': 'ACTIVE', 'violations': [], 'echo': body})


def start_mock():
    """启动 mock 保障模块，返回 (server, base_url)。"""
    server = ThreadingHTTPServer(('127.0.0.1', 0), MockAssuranceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f'http://127.0.0.1:{server.server_address[1]}'


def reset_mock(health_body=None, health_status=200):
    MockAssuranceHandler.health_body = (health_body if health_body is not None
                                        else {'status': 'ok', 'version': '0.4.0'})
    MockAssuranceHandler.health_status = health_status
    MockAssuranceHandler.posted_paths = []
    MockAssuranceHandler.health_hits = 0


ACTUAL = {
    'intent_id': 'REQ-001-CI-001',
    'checked_at': '2026-10-29T10:00:00+08:00',
    'reachable': True,
    'expected_flow_present': True,
}


# ============ 纯函数 ============

def test_version_key():
    check('0.4.0 → (0,4)', _version_key('0.4.0') == (0, 4))
    check('0.4 → (0,4)', _version_key('0.4') == (0, 4))
    check('v1.2.3 → (1,2)', _version_key('v1.2.3') == (1, 2))
    check('空串 → (-1,-1)', _version_key('') == (-1, -1))
    check('非数字 → (-1,-1)', _version_key('abc') == (-1, -1))


def test_extract_version():
    check('直接字段', _extract_version({'version': '0.4.0'}) == '0.4.0')
    check('api_version 字段', _extract_version({'api_version': '0.5.0'}) == '0.5.0')
    check('schema_version 字段', _extract_version({'schema_version': '0.1.0'}) == '0.1.0')
    check('嵌套字段', _extract_version({'service': {'version': '0.4.1'}}) == '0.4.1')
    check('数字版本', _extract_version({'version': 1}) == '1')
    check('没有版本字段', _extract_version({'status': 'ok'}) is None)
    check('非 dict', _extract_version('not-a-dict') is None)


# ============ 协商逻辑 ============

def test_version_match_allows_push():
    reset_mock({'status': 'ok', 'version': '0.4.0'})
    server, base = start_mock()
    try:
        bridge = AssuranceBridge(base_url=base, expected_version='0.4.0', timeout=2)
        state = bridge.check_contract(force=True)
        check('状态 = ok', state.state == 'ok', f'实际 {state.state}')
        check('对方版本被识别', state.actual == '0.4.0', f'实际 {state.actual}')
        check('允许上报', bridge.push_allowed is True)
        result = bridge.push_check('REQ-001-CI-001', ACTUAL)
        check('push_check 有返回', result is not None)
        check('服务器收到 POST', len(MockAssuranceHandler.posted_paths) == 1,
              f'paths={MockAssuranceHandler.posted_paths}')
    finally:
        server.shutdown()


def test_patch_difference_is_compatible():
    reset_mock({'status': 'ok', 'version': '0.4.1'})
    server, base = start_mock()
    try:
        bridge = AssuranceBridge(base_url=base, expected_version='0.4.0', timeout=2)
        state = bridge.check_contract(force=True)
        check('状态 = compatible', state.state == 'compatible', f'实际 {state.state}')
        check('允许上报', bridge.push_allowed is True)
        bridge.push_check('REQ-001-CI-001', ACTUAL)
        check('服务器收到 POST', len(MockAssuranceHandler.posted_paths) == 1)
    finally:
        server.shutdown()


def test_version_mismatch_blocks_push():
    reset_mock({'status': 'ok', 'version': '0.9.0'})
    server, base = start_mock()
    try:
        bridge = AssuranceBridge(base_url=base, expected_version='0.4.0', timeout=2)
        state = bridge.check_contract(force=True)
        check('状态 = mismatch', state.state == 'mismatch', f'实际 {state.state}')
        check('blocking 为 True', state.blocking is True)
        check('不允许上报', bridge.push_allowed is False)
        result = bridge.push_check('REQ-001-CI-001', ACTUAL)
        check('push_check 返回 None', result is None)
        check('服务器没收到 POST', len(MockAssuranceHandler.posted_paths) == 0,
              f'paths={MockAssuranceHandler.posted_paths}')
    finally:
        server.shutdown()


def test_missing_version_is_compatible_by_default():
    reset_mock({'status': 'ok'})  # 对方不暴露版本字段（= 当前真实伙伴的情况）
    server, base = start_mock()
    try:
        bridge = AssuranceBridge(base_url=base, expected_version='0.4.0', timeout=2)
        state = bridge.check_contract(force=True)
        check('状态 = unknown', state.state == 'unknown', f'实际 {state.state}')
        check('默认不阻断', state.blocking is False)
        check('允许上报（兼容模式）', bridge.push_allowed is True)
        bridge.push_check('REQ-001-CI-001', ACTUAL)
        check('服务器收到 POST', len(MockAssuranceHandler.posted_paths) == 1)
    finally:
        server.shutdown()


def test_strict_mode_blocks_missing_version():
    reset_mock({'status': 'ok'})
    server, base = start_mock()
    try:
        bridge = AssuranceBridge(base_url=base, expected_version='0.4.0',
                                 strict_version=True, timeout=2)
        state = bridge.check_contract(force=True)
        check('状态 = unknown', state.state == 'unknown', f'实际 {state.state}')
        check('严格模式不允许上报', bridge.push_allowed is False)
        bridge.push_check('REQ-001-CI-001', ACTUAL)
        check('服务器没收到 POST', len(MockAssuranceHandler.posted_paths) == 0)
    finally:
        server.shutdown()


def test_unreachable_does_not_block():
    reset_mock({'status': 'ok', 'version': '0.4.0'})
    server, base = start_mock()
    server.shutdown()  # 直接把对方关掉 = 不可达
    try:
        bridge = AssuranceBridge(base_url=base, expected_version='0.4.0', timeout=1)
        state = bridge.check_contract(force=True)
        check('状态 = unreachable', state.state == 'unreachable', f'实际 {state.state}')
        check('不因版本阻断', bridge.push_allowed is True)
        check('blocking 为 False', state.blocking is False)
    finally:
        try:
            server.server_close()
        except Exception:
            pass


def test_disabled_bridge_is_noop():
    bridge = AssuranceBridge(base_url='', enabled=False)
    state = bridge.check_contract(force=True)
    check('状态 = disabled', state.state == 'disabled', f'实际 {state.state}')
    check('不允许上报', bridge.push_allowed is False)
    check('push_check 返回 None', bridge.push_check('x', ACTUAL) is None)


def test_version_check_is_cached():
    reset_mock({'status': 'ok', 'version': '0.4.0'})
    server, base = start_mock()
    try:
        bridge = AssuranceBridge(base_url=base, expected_version='0.4.0', timeout=2)
        bridge.check_contract(force=True)
        hits_after_first = MockAssuranceHandler.health_hits
        for _ in range(5):
            bridge.check_contract()
        check('TTL 内不再打 /api/health',
              MockAssuranceHandler.health_hits == hits_after_first,
              f'hits={MockAssuranceHandler.health_hits}')
    finally:
        server.shutdown()


def test_contract_state_to_dict():
    state = ContractState('ok', '0.4.0', '0.4.0', '一致')
    data = state.to_dict()
    check('to_dict 含 state', data['state'] == 'ok')
    check('to_dict 含 expected', data['expected'] == '0.4.0')
    check('to_dict 含 actual', data['actual'] == '0.4.0')
    check('to_dict 含 blocking', data['blocking'] is False)


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
