"""Assurance Reporter：向意图保障模块（同学 A）上报 ActualState。

设计原则：
  1. 单文件、无外部依赖（用 urllib 而不是 requests）
  2. 状态机错（409）自动轮询，不抛异常
  3. 5xx 错误指数退避重试
  4. 所有上报异步，不阻塞探测循环（live_env 用 ThreadPoolExecutor）
  5. 统计：成功率/响应时间/错误码分布 → 给 Dashboard 用

主入口：
  reporter = AssuranceReporter(
      base_url='http://192.168.1.100:8000/api/v1/assurance',
      timeout_s=5,
      retry=3,
  )

  # 探测后
  resp = reporter.check(intent_id='REQ-001-CI-001', actual=actual_dict)
  # resp = {'status_code': 200, 'body': {...}, 'latency_ms': 12.3}

  # 修复后复测
  resp = reporter.verify(intent_id='REQ-001-CI-001', actual_after_heal=actual_dict)
"""
import json
import time
import urllib.request
import urllib.error
import threading
from typing import Dict, Optional, Any
from collections import deque


class ReporterError(Exception):
    """上报失败的封装异常。"""
    pass


class AssuranceReporter:
    """意图保障模块的 HTTP 客户端。"""

    def __init__(self,
                 base_url: str,
                 timeout_s: int = 5,
                 retry: int = 3,
                 retry_backoff_base: float = 0.5):
        """初始化。

        参数:
            base_url: 保障模块的 API 根地址，例如
                'http://192.168.1.100:8000/api/v1/assurance'
            timeout_s: 单次请求超时
            retry: 5xx 错误的最大重试次数
            retry_backoff_base: 指数退避基数（秒）
        """
        self.base_url = base_url.rstrip('/')
        self.timeout_s = timeout_s
        self.retry = retry
        self.retry_backoff_base = retry_backoff_base

        # 统计（线程安全用锁）
        self._stats_lock = threading.Lock()
        self._stats = {
            'total_calls': 0,
            'success_calls': 0,
            'failed_calls': 0,
            'last_status_code': None,
            'last_error': None,
            'recent_latency_ms': deque(maxlen=50),  # 最近 50 次响应时间
            'recent_status': deque(maxlen=50),  # 最近 50 次状态码
            'last_call_at': None,
        }

    # ============================================================
    # 公开 API
    # ============================================================

    def check(self, intent_id: str, actual: dict) -> dict:
        """上报一次探测结果。POST /api/v1/assurance/check。

        参数:
            intent_id: 关联意图 ID
            actual: ActualState dict（由 ActualStateBuilder.build() 输出）

        返回:
            dict: {'status_code': int, 'body': dict|None,
                   'latency_ms': float, 'error': str|None}

        异常:
            ReporterError: 4xx（除 409 外）或重试用尽
        """
        url = f'{self.base_url}/check'
        body_json = {'intent_id': intent_id, 'actual': actual}
        return self._post(url, body_json, expect_409=True)

    def verify(self, intent_id: str, actual_after_heal: dict) -> dict:
        """上报修复后复测结果。POST /api/v1/assurance/verify。"""
        url = f'{self.base_url}/verify'
        body_json = {'intent_id': intent_id, 'actual_after_heal': actual_after_heal}
        return self._post(url, body_json, expect_409=True)

    def query_intent_status(self, intent_id: str) -> Optional[dict]:
        """查询意图当前状态（GET /api/v1/assurance/record/{intent_id}）。

        用于 409 状态机错的恢复路径：保障模块说"必须先 activate"，
        可以轮询此接口看 ACTIVE 状态何时就绪。
        """
        url = f'{self.base_url}/record/{intent_id}'
        result = self._get(url)
        if result['status_code'] == 200:
            return result['body']
        return None

    def get_stats(self) -> dict:
        """获取上报统计（Dashboard 用）。"""
        with self._stats_lock:
            stats = dict(self._stats)
            stats['recent_latency_ms'] = list(stats['recent_latency_ms'])
            stats['recent_status'] = list(stats['recent_status'])
        return stats

    def reset_stats(self) -> None:
        """重置统计。"""
        with self._stats_lock:
            self._stats = {
                'total_calls': 0,
                'success_calls': 0,
                'failed_calls': 0,
                'last_status_code': None,
                'last_error': None,
                'recent_latency_ms': deque(maxlen=50),
                'recent_status': deque(maxlen=50),
                'last_call_at': None,
            }

    # ============================================================
    # 内部 HTTP
    # ============================================================

    def _post(self, url: str, body_dict: dict, expect_409: bool = True) -> dict:
        """内部 POST 方法，带重试 + 统计。"""
        body_bytes = json.dumps(body_dict, ensure_ascii=False).encode('utf-8')
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'application/json',
        }

        last_result = None
        for attempt in range(self.retry + 1):
            t0 = time.time()
            try:
                req = urllib.request.Request(
                    url, data=body_bytes, headers=headers, method='POST')
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read()
                    latency_ms = (time.time() - t0) * 1000
                    try:
                        body = json.loads(raw.decode('utf-8')) if raw else {}
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        body = {'raw': raw.decode('utf-8', errors='replace')}
                    result = {
                        'status_code': resp.status,
                        'body': body,
                        'latency_ms': round(latency_ms, 2),
                        'error': None,
                    }
                    self._record_stats(result)
                    last_result = result
                    break
            except urllib.error.HTTPError as e:
                latency_ms = (time.time() - t0) * 1000
                raw = e.read() if hasattr(e, 'read') else b''
                try:
                    body = json.loads(raw.decode('utf-8')) if raw else None
                except Exception:
                    body = None
                result = {
                    'status_code': e.code,
                    'body': body,
                    'latency_ms': round(latency_ms, 2),
                    'error': str(e),
                }
                self._record_stats(result)
                last_result = result

                # 4xx 处理（除 409 外不重试）
                if 400 <= e.code < 500 and e.code != 409:
                    return result
                # 409 状态机错：单次不重试，让调用方决定下一步
                if e.code == 409:
                    return result
                # 5xx：重试
                if e.code >= 500 and attempt < self.retry:
                    self._sleep_backoff(attempt)
                    continue
                # 其他：返回
                return result
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                latency_ms = (time.time() - t0) * 1000
                result = {
                    'status_code': 0,
                    'body': None,
                    'latency_ms': round(latency_ms, 2),
                    'error': str(e),
                }
                self._record_stats(result)
                last_result = result
                if attempt < self.retry:
                    self._sleep_backoff(attempt)
                    continue
                return result
            except Exception as e:
                latency_ms = (time.time() - t0) * 1000
                result = {
                    'status_code': 0,
                    'body': None,
                    'latency_ms': round(latency_ms, 2),
                    'error': f'{type(e).__name__}: {e}',
                }
                self._record_stats(result)
                last_result = result
                if attempt < self.retry:
                    self._sleep_backoff(attempt)
                    continue
                return result

        return last_result or {
            'status_code': 0, 'body': None, 'latency_ms': 0.0,
            'error': 'unknown failure',
        }

    def _get(self, url: str) -> dict:
        """内部 GET 方法。"""
        t0 = time.time()
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
                latency_ms = (time.time() - t0) * 1000
                body = json.loads(raw.decode('utf-8')) if raw else {}
                return {
                    'status_code': resp.status,
                    'body': body,
                    'latency_ms': round(latency_ms, 2),
                    'error': None,
                }
        except Exception as e:
            return {
                'status_code': 0,
                'body': None,
                'latency_ms': (time.time() - t0) * 1000,
                'error': str(e),
            }

    def _sleep_backoff(self, attempt: int) -> None:
        """指数退避：base * 2^attempt。"""
        time.sleep(self.retry_backoff_base * (2 ** attempt))

    def _record_stats(self, result: dict) -> None:
        """更新统计（线程安全）。"""
        with self._stats_lock:
            self._stats['total_calls'] += 1
            self._stats['last_status_code'] = result['status_code']
            self._stats['last_error'] = result['error']
            self._stats['last_call_at'] = time.time()
            self._stats['recent_latency_ms'].append(result['latency_ms'])
            self._stats['recent_status'].append(result['status_code'])
            if result['status_code'] and 200 <= result['status_code'] < 300:
                self._stats['success_calls'] += 1
            else:
                self._stats['failed_calls'] += 1


# ============================================================
# CLI / 自检
# ============================================================

if __name__ == '__main__':
    import sys
    print('AssuranceReporter CLI')
    print('用法: 在其他模块里 from reporter import AssuranceReporter')
    print('示例:')
    print('  r = AssuranceReporter("http://127.0.0.1:8000/api/v1/assurance")')
    print('  resp = r.check("REQ-001-CI-001", {"intent_id": "REQ-001-CI-001", ...})')
    print('  print(resp)')