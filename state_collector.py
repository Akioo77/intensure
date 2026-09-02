"""REST 客户端: 拉取 + 验证网络状态。

用法:
  from state_collector import StateCollector
  c = StateCollector(base_url='http://127.0.0.1:8080')
  state = c.get_state()
  report = c.validate(state)
"""
import json
import time
import urllib.request
import urllib.error
from typing import Optional


class StateCollector:
    """状态采集客户端.

    提供:
      - get_state()        完整状态
      - get_switches()     交换机
      - get_hosts()        主机
      - get_port_stats()   端口计数器
      - get_flows()        流表
      - get_events()       事件
      - validate()         跑验证
      - ping_hosts()       实际连通性 (可选, 需要 Mininet)
    """

    def __init__(self, base_url: str = 'http://127.0.0.1:8080', timeout: float = 5.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    # ---- API 端点 ----
    def get_state(self) -> dict:
        return self._get('/api/state')

    def get_switches(self) -> dict:
        return self._get('/api/switches')

    def get_hosts(self) -> dict:
        return self._get('/api/hosts')

    def get_port_stats(self) -> dict:
        return self._get('/api/port_stats')

    def get_flows(self) -> dict:
        return self._get('/api/flows')

    def get_events(self, since: float = 0, limit: int = 100) -> list:
        return self._get(f'/api/events?since={since}&limit={limit}')

    def validate(self, state: Optional[dict] = None,
                 baseline: Optional[dict] = None) -> dict:
        """跑验证 (调用 server 端的 validate_state).

        若传入 state, 也可本地验证 (不依赖 server endpoint).
        """
        # 服务端验证更权威
        try:
            return self._get('/api/validate')
        except (urllib.error.URLError, urllib.error.HTTPError):
            # fallback: 本地验证
            from state_model import validate_state
            if state is None:
                state = self.get_state()
            return validate_state(state, baseline)

    def ping_hosts(self, host_pairs: list = None) -> dict:
        """用 ping 测主机对连通性 (需要在 Mininet net 上下文里执行).

        实际 demo 不会调用这个 HTTP 端点, 而是用 Mininet 的 cmd.
        此方法保留供将来集成.
        """
        return {
            'note': 'Use Mininet net.ping() directly in demo.',
            'pairs': host_pairs,
        }

    def wait_for_switches(self, n: int, timeout: float = 30) -> bool:
        """等待至少 n 个交换机连接."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                state = self.get_state()
                if len(state.get('switches', {})) >= n:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def wait_for_hosts(self, n: int, timeout: float = 30) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                state = self.get_state()
                if len(state.get('hosts', {})) >= n:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False


# ============ CLI ============
if __name__ == '__main__':
    import sys

    base = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8080'
    cmd = sys.argv[2] if len(sys.argv) > 2 else 'state'

    c = StateCollector(base)
    try:
        if cmd == 'state':
            print(json.dumps(c.get_state(), indent=2, default=str))
        elif cmd == 'switches':
            print(json.dumps(c.get_switches(), indent=2, default=str))
        elif cmd == 'hosts':
            print(json.dumps(c.get_hosts(), indent=2, default=str))
        elif cmd == 'flows':
            print(json.dumps(c.get_flows(), indent=2, default=str))
        elif cmd == 'validate':
            print(json.dumps(c.validate(), indent=2, default=str))
        elif cmd == 'health':
            print(json.dumps(c._get('/api/health'), indent=2))
        else:
            print(f"unknown cmd: {cmd}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(2)
