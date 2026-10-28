"""assurance_bridge.py - intensure → assurance-agent 的可选推送通道。

设计原则（v1.0）:
  1. intensure 默认独立运行，不依赖 assurance-agent
  2. 配置 ASSURANCE_BASE 启用推送（不配则所有 push* 都是 no-op）
  3. 不阻塞 intensure 主流程：推送失败仅 warn，不抛异常
  4. 复用 actual_state_builder 的产出格式
  5. 自动处理 VM 内 HTTPS proxy 不稳定的情况（探测失败时自动降级到直连）

v3.18.0 新增（§6 契约版本管理，P3-3）:
  6. 推送前做一次契约版本协商：读对方 /api/health 的版本字段，
     大版本/次版本不一致 → 停止推送（防"对方偷偷升级，我方悄悄坏掉"），
     对方没暴露版本字段 → 只 warn 不阻断（保证现有联调链路不受影响）

使用:
  from assurance_bridge import AssuranceBridge

  bridge = AssuranceBridge()  # 从环境变量 ASSURANCE_BASE 读 URL
  bridge.push_check(intent_id='demo-001', actual_state=actual_dict)
  status = bridge.get_status('demo-001')
"""
import os
import json
import logging
import re
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta

import metrics  # noqa: E402  (v3.18.0 可观测性)

logger = logging.getLogger(__name__)


# 请求超时（秒）— cloudflared 较慢，给 8s
DEFAULT_TIMEOUT = 8

# 注意：不再硬编码 assurance-agent URL——演示前必须通过环境变量配置：
#   export ASSURANCE_BASE=https://xxx.trycloudflare.com
# 原因：A 同学 cloudflared 重启会换域名，硬编码会演示现场掉连。

# Proxy 探测超时（秒）
PROXY_PROBE_TIMEOUT = 3

# ============================================================
# §6 契约版本管理（v3.18.0 · P3-3）
# ============================================================

# 我方实现所针对的保障模块版本（契约版本）。
# 对方升级到 0.5.0 之类时，可用环境变量 ASSURANCE_EXPECTED_VERSION 临时对齐，
# 不必改代码。
DEFAULT_EXPECTED_VERSION = '0.4.0'

# 对方契约里 request 的 schema 版本（翻译模块确认稿 v0.1）
CONTRACT_SCHEMA_VERSION = '0.1.0'

# 版本检查结果的缓存时间（秒）——避免每次推送都多打一次 /api/health
VERSION_CHECK_TTL_S = 60

# 对方可能用来暴露版本号的字段名（按优先级）
VERSION_KEYS = ('version', 'api_version', 'agent_version', 'assurance_version', 'schema_version')

# 严格模式：对方没暴露版本字段时也拒绝推送（默认关闭，避免打断现有联调）
STRICT_VERSION_ENV = 'ASSURANCE_STRICT_VERSION'
EXPECTED_VERSION_ENV = 'ASSURANCE_EXPECTED_VERSION'


def _version_key(version: str) -> tuple:
    """把版本串变成 (major, minor) 元组；解析不出来返回 (-1, -1)。"""
    nums = re.findall(r'\d+', str(version or ''))
    if not nums:
        return (-1, -1)
    major = int(nums[0])
    minor = int(nums[1]) if len(nums) > 1 else 0
    return (major, minor)


def _extract_version(payload: Any) -> Optional[str]:
    """从 /api/health 返回体里找版本字段（找不到返回 None）。"""
    if not isinstance(payload, dict):
        return None
    for key in VERSION_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    # 有些实现放在嵌套的 service/version 对象里
    for nested_key in ('service', 'server', 'meta', 'info'):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            found = _extract_version(nested)
            if found:
                return found
    return None


class ContractState:
    """契约版本协商结果。

    state 取值：
      ok          —— 对方版本 == 期望版本
      compatible  —— 次版本内差异（如 0.4.1 vs 0.4.0），允许推送
      unknown     —— 对方没暴露版本字段（默认按兼容处理，仅 warn）
      mismatch    —— 大/次版本不一致，阻断推送
      unreachable —— 对方连不上（不阻断，让真正的请求去报错）
      disabled    —— 未配置 ASSURANCE_BASE
    """

    __slots__ = ('state', 'expected', 'actual', 'message', 'checked_at')

    def __init__(self, state: str, expected: str, actual: Optional[str] = None,
                 message: str = '', checked_at: float = 0.0):
        self.state = state
        self.expected = expected
        self.actual = actual
        self.message = message
        self.checked_at = checked_at

    @property
    def blocking(self) -> bool:
        """是否阻断推送。"""
        return self.state == 'mismatch'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'state': self.state,
            'expected': self.expected,
            'actual': self.actual,
            'message': self.message,
            'checked_at': self.checked_at,
            'blocking': self.blocking,
        }

    def __repr__(self) -> str:
        return (f'ContractState(state={self.state!r}, expected={self.expected!r}, '
                f'actual={self.actual!r})')


class AssuranceBridge:
    """intensure → assurance-agent 推送通道。

    用法:
        bridge = AssuranceBridge()  # 启用（如果 ASSURANCE_BASE 环境变量存在）
        bridge = AssuranceBridge(enabled=False)  # 禁用（no-op）
        bridge.push_check('demo-001', actual_state)
    """

    def __init__(self,
                 base_url: Optional[str] = None,
                 enabled: Optional[bool] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 expected_version: Optional[str] = None,
                 strict_version: Optional[bool] = None):
        """初始化。

        参数:
            base_url: assurance-agent 的 base URL（默认读环境变量）
            enabled: 是否启用（None 时默认：base_url 非空则启用）
            timeout: HTTP 请求超时（秒）
            expected_version: 期望的对方契约版本（默认读 ASSURANCE_EXPECTED_VERSION）
            strict_version: 严格模式（对方没版本号也拒绝上报）
        """
        # 优先级: 显式参数 > ASSURANCE_BASE > ASSURANCE_BASE_FALLBACK > 空(no-op)
        # ASSURANCE_BASE_FALLBACK 用于 A 同学 cloudflared 重启换域名时的备用地址
        self.base_url = (
            base_url
            or os.environ.get("ASSURANCE_BASE")
            or os.environ.get("ASSURANCE_BASE_FALLBACK")
            or ""
        ).rstrip("/")
        self.timeout = timeout

        if enabled is None:
            enabled = bool(self.base_url)
        self.enabled = enabled

        if not self.base_url:
            logger.warning(
                "AssuranceBridge 未配置 ASSURANCE_BASE 环境变量，将作为 no-op 运行。"
                "演示/联调前必须 export ASSURANCE_BASE=https://xxx.trycloudflare.com"
            )

        # 创建带 proxy bypass 的 requests session
        self.session = self._setup_session()

        # ---- §6 契约版本管理（v3.18.0）----
        self.expected_version = (expected_version or
                                 os.environ.get(EXPECTED_VERSION_ENV) or
                                 DEFAULT_EXPECTED_VERSION).strip()
        if strict_version is None:
            strict_version = os.environ.get(STRICT_VERSION_ENV, '').strip().lower() in (
                '1', 'true', 'yes', 'on')
        self.strict_version = bool(strict_version)
        self._contract = ContractState(
            'disabled' if not self.enabled else 'unknown',
            self.expected_version, None, '尚未检查')
        self._contract_checked_at = 0.0
        self._contract_warned = False
        self._publish_contract_metric()

        if self.enabled:
            logger.info(f"[AssuranceBridge] 启用，目标: {self.base_url}")
            logger.info(
                f"[AssuranceBridge] 期望契约版本: {self.expected_version}"
                f"（契约 schema {CONTRACT_SCHEMA_VERSION}）"
                + ("，严格模式" if self.strict_version else "，宽松模式")
            )
        else:
            logger.info("[AssuranceBridge] 禁用（未配置 ASSURANCE_BASE）")

    # ============================================================
    # 公开 API
    # ============================================================

    def health(self) -> bool:
        """检查 assurance-agent 是否可达。"""
        if not self.enabled:
            return False
        try:
            r = self.session.get(f"{self.base_url}/api/health", timeout=self.timeout)
            return r.status_code == 200 and '"ok"' in r.text
        except Exception as e:
            logger.warning(f"[AssuranceBridge] 健康检查失败: {e}")
            return False

    # ============================================================
    # §6 契约版本管理（v3.18.0 · P3-3）
    # ============================================================

    @property
    def contract_state(self) -> ContractState:
        """当前契约版本协商结果（不触发检查）。"""
        return self._contract

    @property
    def push_allowed(self) -> bool:
        """当前是否允许推送（版本冲突时会变成 False）。"""
        if not self.enabled:
            return False
        if self._contract.state == 'mismatch':
            return False
        if self.strict_version and self._contract.state == 'unknown':
            return False
        return True

    def check_contract(self, force: bool = False) -> ContractState:
        """跟对方协商契约版本（带 TTL 缓存）。

        规则：
          对方版本 == 期望版本        → ok
          同 major.minor（补丁级差异）→ compatible
          对方没暴露版本字段          → unknown（默认只 warn，不阻断）
          major.minor 不一致          → mismatch（阻断推送）
          对方连不上                  → unreachable（不阻断）
        """
        if not self.enabled:
            self._contract = ContractState('disabled', self.expected_version)
            self._publish_contract_metric()
            return self._contract

        now = datetime.now().timestamp()
        if (not force and self._contract_checked_at and
                now - self._contract_checked_at < VERSION_CHECK_TTL_S):
            return self._contract

        actual = None
        state = 'unknown'
        message = ''
        try:
            r = self.session.get(f"{self.base_url}/api/health", timeout=self.timeout)
            if r.status_code == 200:
                try:
                    payload = r.json()
                except (ValueError, json.JSONDecodeError):
                    payload = None
                actual = _extract_version(payload)
                if actual is None:
                    state = 'unknown'
                    message = '对方 /api/health 未暴露版本字段，按兼容模式继续'
                else:
                    exp_key = _version_key(self.expected_version)
                    act_key = _version_key(actual)
                    if actual.strip() == self.expected_version:
                        state = 'ok'
                        message = f'契约版本一致（{actual}）'
                    elif exp_key == act_key:
                        state = 'compatible'
                        message = (f'契约版本补丁级差异（期望 {self.expected_version}，'
                                   f'实际 {actual}）')
                    else:
                        state = 'mismatch'
                        message = (f'契约版本不一致（期望 {self.expected_version}，'
                                   f'实际 {actual}）—— 已停止推送')
            else:
                state = 'unreachable'
                message = f'健康检查返回 HTTP {r.status_code}'
        except Exception as e:
            state = 'unreachable'
            message = f'健康检查异常: {e.__class__.__name__}'

        self._contract = ContractState(state, self.expected_version, actual,
                                       message, now)
        self._contract_checked_at = now
        self._publish_contract_metric()

        if state == 'mismatch':
            logger.error(f"[AssuranceBridge] {message}")
        elif state == 'unknown' and not self._contract_warned:
            logger.warning(f"[AssuranceBridge] {message}；如需强制要求对方带版本号，"
                           f"请设 {STRICT_VERSION_ENV}=1")
            self._contract_warned = True
        elif state in ('ok', 'compatible'):
            logger.info(f"[AssuranceBridge] {message}")
        elif state == 'unreachable':
            logger.warning(f"[AssuranceBridge] {message}")

        return self._contract

    def _publish_contract_metric(self) -> None:
        """把契约状态写进指标（当前状态=1，历史状态清 0）。"""
        try:
            for state in ('ok', 'compatible', 'unknown', 'mismatch',
                          'unreachable', 'disabled'):
                metrics.CONTRACT_STATE.set(
                    1 if state == self._contract.state else 0,
                    {
                        'state': state,
                        'expected': self.expected_version,
                        'actual': self._contract.actual or 'unknown',
                    })
        except Exception:  # 指标失败绝不能影响主流程
            pass

    def _guard_contract(self) -> bool:
        """推送前的版本守卫：不通过就返回 False（本模块继续跑，只是不上报）。"""
        if not self.enabled:
            return False
        state = self.check_contract()
        if state.state == 'mismatch':
            logger.error(
                f"[AssuranceBridge] 契约版本不兼容，放弃本次上报"
                f"（期望 {state.expected}，实际 {state.actual}）。"
                f"如确认兼容，可设 {EXPECTED_VERSION_ENV}={state.actual} 后重试。"
            )
            metrics.REPORTS_TOTAL.inc({'endpoint': 'contract', 'result': 'mismatch'})
            return False
        if self.strict_version and state.state == 'unknown':
            logger.error(
                f"[AssuranceBridge] 严格模式下对方未暴露版本号，放弃上报"
                f"（{STRICT_VERSION_ENV}=1）"
            )
            metrics.REPORTS_TOTAL.inc({'endpoint': 'contract', 'result': 'unknown'})
            return False
        return True

    def register_intent(self,
                        intent_id: str,
                        source_cidr: str,
                        destination_cidr: str,
                        protocol: str = "tcp",
                        destination_port: int = 443,
                        deployment_id: Optional[str] = None,
                        devices: Optional[List[str]] = None,
                        max_latency_ms: Optional[float] = None,
                        max_packet_loss: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """register + activate 一个意图到 assurance-agent。

        返回:
            RecordView dict（成功时）或 None（失败时）
        """
        if not self.enabled:
            logger.debug("[AssuranceBridge] 禁用，跳过 register")
            return None
        if not self._guard_contract():
            return None

        try:
            # Step 1: register
            payload = {
                "intent": {
                    "intent_id": intent_id,
                    "intent_type": "ACCESS_CONTROL",
                    "action": "ALLOW",
                    "source": {"cidr": source_cidr},
                    "destination": {"cidr": destination_cidr},
                    "protocol": protocol,
                    "destination_port": destination_port,
                },
                "deployment_id": deployment_id or f"deploy-{intent_id}",
                "deployment_status": "deployed",
                "devices": devices or [],
                "policy_refs": [],
            }
            r = self.session.post(
                f"{self.base_url}/api/v1/assurance/register",
                json=payload,
                timeout=self.timeout,
            )
            if r.status_code >= 400:
                logger.warning(f"[AssuranceBridge] register 失败 ({r.status_code}): {r.text[:200]}")
                # 不 return，继续尝试 activate（A 同学的 register 可能幂等）

            # Step 2: activate（生成 expected_state）
            act_payload = {"intent_id": intent_id}
            if max_latency_ms is not None:
                act_payload["max_latency_ms"] = max_latency_ms
            if max_packet_loss is not None:
                act_payload["max_packet_loss"] = max_packet_loss

            r2 = self.session.post(
                f"{self.base_url}/api/v1/assurance/activate",
                json=act_payload,
                timeout=self.timeout,
            )
            if r2.status_code == 200:
                logger.info(f"[AssuranceBridge] register+activate 成功: {intent_id}")
                return r2.json()
            else:
                logger.warning(
                    f"[AssuranceBridge] activate 失败 ({r2.status_code}): {r2.text[:200]}"
                )
                return None

        except Exception as e:
            logger.warning(f"[AssuranceBridge] register_intent 异常: {e}")
            return None

    def push_check(self,
                   intent_id: str,
                   actual_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """推送 ActualState 做一致性检查。

        参数:
            intent_id: 意图 ID
            actual_state: ActualState dict（来自 actual_state_builder.build()）

        返回:
            RecordView dict（成功时）或 None（失败时/禁用时）
        """
        if not self.enabled:
            logger.debug("[AssuranceBridge] 禁用，跳过 push_check")
            return None
        if not self._guard_contract():
            return None

        try:
            payload = {
                "intent_id": intent_id,
                "actual": actual_state,
            }
            r = self.session.post(
                f"{self.base_url}/api/v1/assurance/check",
                json=payload,
                timeout=self.timeout,
            )
            if r.status_code == 200:
                data = r.json()
                status = data.get("status", "?")
                violations = len(data.get("violations", []))
                logger.info(
                    f"[AssuranceBridge] push_check: {intent_id} -> {status}, "
                    f"violations={violations}"
                )
                return data
            else:
                logger.warning(
                    f"[AssuranceBridge] push_check 失败 ({r.status_code}): {r.text[:200]}"
                )
                return None
        except Exception as e:
            logger.warning(f"[AssuranceBridge] push_check 异常: {e}")
            return None

    def get_status(self, intent_id: str) -> Optional[Dict[str, Any]]:
        """拉取 intent 的当前状态（用于 dashboard 显示）。

        返回:
            RecordView dict（成功时）或 None
        """
        if not self.enabled:
            return None
        if not self._guard_contract():
            return None
        try:
            r = self.session.get(
                f"{self.base_url}/api/v1/assurance/record/{intent_id}",
                timeout=self.timeout,
            )
            if r.status_code == 200:
                return r.json()
            return None
        except Exception as e:
            logger.warning(f"[AssuranceBridge] get_status 异常: {e}")
            return None

    def trigger_diagnose(self, intent_id: str) -> Optional[Dict[str, Any]]:
        """触发根因诊断。"""
        return self._simple_post(f"/api/v1/assurance/diagnose?intent_id={intent_id}")

    def trigger_heal(self, intent_id: str) -> Optional[Dict[str, Any]]:
        """触发自愈评估。"""
        return self._simple_post(f"/api/v1/assurance/heal?intent_id={intent_id}")

    def verify_recovery(self,
                        intent_id: str,
                        actual_after_heal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """验证恢复状态。"""
        if not self.enabled:
            return None
        if not self._guard_contract():
            return None
        try:
            r = self.session.post(
                f"{self.base_url}/api/v1/assurance/verify",
                json={"intent_id": intent_id, "actual_after_heal": actual_after_heal},
                timeout=self.timeout,
            )
            if r.status_code == 200:
                return r.json()
            return None
        except Exception as e:
            logger.warning(f"[AssuranceBridge] verify_recovery 异常: {e}")
            return None

    # ============================================================
    # 内部工具
    # ============================================================

    def _simple_post(self, path: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        if not self._guard_contract():
            return None
        try:
            r = self.session.post(
                f"{self.base_url}{path}",
                timeout=self.timeout,
            )
            if r.status_code == 200:
                return r.json()
            return None
        except Exception as e:
            logger.warning(f"[AssuranceBridge] POST {path} 异常: {e}")
            return None

    def _setup_session(self) -> requests.Session:
        """创建带 proxy bypass 的 requests session。

        处理 VM 内 Clash 代理不稳定的情况：如果系统设置了 HTTPS_PROXY 但它
        连不上，则改用直连，避免 ProxyError。
        """
        session = requests.Session()

        # 读环境变量里的 proxy 设置
        proxies_from_env = {
            "http": os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"),
            "https": os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
        }
        # 过滤 None
        proxies_from_env = {k: v for k, v in proxies_from_env.items() if v}

        if not proxies_from_env or not self.base_url:
            # 无代理 或 无目标 → 直连
            session.trust_env = False
            return session

        # 探测代理是否可用
        try:
            r = session.get(
                f"{self.base_url}/api/health",
                timeout=PROXY_PROBE_TIMEOUT,
                proxies=proxies_from_env,
            )
            if r.status_code == 200:
                # 代理可用，用代理
                session.proxies.update(proxies_from_env)
                logger.debug(f"[AssuranceBridge] 使用代理: {proxies_from_env}")
            else:
                # 代理不通（status 不是 200），改直连
                logger.info(f"[AssuranceBridge] 代理不可用 (HTTP {r.status_code})，改用直连")
                session.trust_env = False
        except Exception as e:
            logger.info(f"[AssuranceBridge] 代理不可用 ({e.__class__.__name__})，改用直连")
            session.trust_env = False

        return session


# ============================================================
# 单例（可选）
# ============================================================
_default_bridge: Optional[AssuranceBridge] = None


def get_bridge() -> AssuranceBridge:
    """获取默认单例。"""
    global _default_bridge
    if _default_bridge is None:
        _default_bridge = AssuranceBridge()
    return _default_bridge


# ============================================================
# CLI 入口（调试用）
# ============================================================
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    bridge = AssuranceBridge()

    print("=== AssuranceBridge ===")
    print(f"  base_url: {bridge.base_url}")
    print(f"  enabled:  {bridge.enabled}")
    print()

    if not bridge.enabled:
        print("未启用（设 ASSURANCE_BASE 环境变量启用）")
        print()
        print("示例:")
        print("  ASSURANCE_BASE=https://xxx.trycloudflare.com python3 assurance_bridge.py")
        sys.exit(0)

    print(f"健康检查: {'✓' if bridge.health() else '✗'}")

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "contract":
            state = bridge.check_contract(force=True)
            print("契约版本协商:")
            print(f"  期望版本: {state.expected}")
            print(f"  对方版本: {state.actual or '（未暴露）'}")
            print(f"  状态    : {state.state}")
            print(f"  说明    : {state.message}")
            print(f"  可上报  : {'是' if bridge.push_allowed else '否'}")
        elif cmd == "register" and len(sys.argv) >= 5:
            intent_id, src, dst = sys.argv[2], sys.argv[3], sys.argv[4]
            result = bridge.register_intent(intent_id, src, dst)
            print(
                "register+activate: "
                f"{json.dumps(result, indent=2, ensure_ascii=False)[:300] if result else '失败'}"
            )
        elif cmd == "status" and len(sys.argv) >= 3:
            intent_id = sys.argv[2]
            result = bridge.get_status(intent_id)
            print(
                "status: "
                f"{json.dumps(result, indent=2, ensure_ascii=False)[:500] if result else '失败'}"
            )
        else:
            print("用法:")
            print("  python3 assurance_bridge.py                              # 健康检查")
            print("  python3 assurance_bridge.py contract                    # 契约版本协商")
            print("  python3 assurance_bridge.py register <intent_id> <src> <dst>")
            print("  python3 assurance_bridge.py status <intent_id>")
    else:
        # 默认跑一个端到端冒烟测试
        print()
        print("=== 端到端冒烟测试 ===")
        TEST_INTENT = f"smoke-{int(datetime.now().timestamp())}"
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()

        # 1. register
        print(f"1) register+activate {TEST_INTENT}...")
        r = bridge.register_intent(
            TEST_INTENT,
            "192.168.10.0/24", "10.0.50.10/32",
            max_latency_ms=50, max_packet_loss=0.01
        )
        print(f"   结果: {'✓' if r else '✗'}")

        # 2. push_check 健康
        print("2) push_check (healthy)...")
        healthy_actual = {
            "intent_id": TEST_INTENT,
            "checked_at": now,
            "reachable": True,
            "latency_ms": 12.0,
            "packet_loss": 0.0,
            "expected_flow_present": True,
            "down_links": [],
            "down_ports": [],
            "conflicting_flows": []
        }
        r = bridge.push_check(TEST_INTENT, healthy_actual)
        print(
            f"   结果: status={r.get('status') if r else 'N/A'}, "
            f"violations={len(r.get('violations', [])) if r else 0}"
        )

        # 3. push_check 故障
        print("3) push_check (fault)...")
        fault_actual = dict(healthy_actual)
        fault_actual["reachable"] = False
        fault_actual["expected_flow_present"] = False
        fault_actual["down_links"] = ["s1-eth2"]
        fault_actual["packet_loss"] = 1.0
        r = bridge.push_check(TEST_INTENT, fault_actual)
        print(
            f"   结果: status={r.get('status') if r else 'N/A'}, "
            f"violations={len(r.get('violations', [])) if r else 0}"
        )

        # 4. diagnose
        print("4) diagnose...")
        r = bridge.trigger_diagnose(TEST_INTENT)
        if r and r.get("diagnosis"):
            print(
                "   根因: "
                f"{[rc.get('cause_type') for rc in r['diagnosis'].get('root_causes', [])]}"
            )
            print(f"   置信度: {r['diagnosis'].get('confidence')}")
        else:
            print(f"   结果: {'失败' if not r else '无 diagnosis'}")

        # 5. heal
        print("5) heal...")
        r = bridge.trigger_heal(TEST_INTENT)
        if r:
            print(f"   verdict: {r.get('verdict')}")
            print(f"   blocked_by: {r.get('blocked_by')}")
        else:
            print("   结果: 失败")

        print()
        print("✅ 冒烟测试完成")
