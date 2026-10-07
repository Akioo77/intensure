"""assurance_bridge.py - intensure → assurance-agent 的可选推送通道。

设计原则（v1.0）:
  1. intensure 默认独立运行，不依赖 assurance-agent
  2. 配置 ASSURANCE_BASE 启用推送（不配则所有 push* 都是 no-op）
  3. 不阻塞 intensure 主流程：推送失败仅 warn，不抛异常
  4. 复用 actual_state_builder 的产出格式
  5. 自动处理 VM 内 HTTPS proxy 不稳定的情况（探测失败时自动降级到直连）

使用:
  from assurance_bridge import AssuranceBridge

  bridge = AssuranceBridge()  # 从环境变量 ASSURANCE_BASE 读 URL
  bridge.push_check(intent_id='demo-001', actual_state=actual_dict)
  status = bridge.get_status('demo-001')
"""
import os
import json
import logging
import requests
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


# 默认目标（A 同学 cloudflared，2026-09-12 起的新地址）
DEFAULT_BASE = "https://westminster-accounts-permitted-pursuant.trycloudflare.com"

# 请求超时（秒）— cloudflared 较慢，给 8s
DEFAULT_TIMEOUT = 8

# Proxy 探测超时（秒）
PROXY_PROBE_TIMEOUT = 3


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
                 timeout: float = DEFAULT_TIMEOUT):
        """初始化。

        参数:
            base_url: assurance-agent 的 base URL（默认读环境变量）
            enabled: 是否启用（None 时默认：base_url 非空则启用）
            timeout: HTTP 请求超时（秒）
        """
        self.base_url = (base_url or os.environ.get("ASSURANCE_BASE") or "").rstrip("/")
        self.timeout = timeout

        if enabled is None:
            enabled = bool(self.base_url)
        self.enabled = enabled

        # 创建带 proxy bypass 的 requests session
        self.session = self._setup_session()

        if self.enabled:
            logger.info(f"[AssuranceBridge] 启用，目标: {self.base_url}")
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
                logger.warning(f"[AssuranceBridge] activate 失败 ({r2.status_code}): {r2.text[:200]}")
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
                logger.info(f"[AssuranceBridge] push_check: {intent_id} -> {status}, violations={violations}")
                return data
            else:
                logger.warning(f"[AssuranceBridge] push_check 失败 ({r.status_code}): {r.text[:200]}")
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

    print(f"=== AssuranceBridge ===")
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
        if cmd == "register" and len(sys.argv) >= 5:
            intent_id, src, dst = sys.argv[2], sys.argv[3], sys.argv[4]
            result = bridge.register_intent(intent_id, src, dst)
            print(f"register+activate: {json.dumps(result, indent=2, ensure_ascii=False)[:300] if result else '失败'}")
        elif cmd == "status" and len(sys.argv) >= 3:
            intent_id = sys.argv[2]
            result = bridge.get_status(intent_id)
            print(f"status: {json.dumps(result, indent=2, ensure_ascii=False)[:500] if result else '失败'}")
        else:
            print("用法:")
            print("  python3 assurance_bridge.py                              # 健康检查")
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
        print(f"2) push_check (healthy)...")
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
        print(f"   结果: status={r.get('status') if r else 'N/A'}, violations={len(r.get('violations', [])) if r else 0}")

        # 3. push_check 故障
        print(f"3) push_check (fault)...")
        fault_actual = dict(healthy_actual)
        fault_actual["reachable"] = False
        fault_actual["expected_flow_present"] = False
        fault_actual["down_links"] = ["s1-eth2"]
        fault_actual["packet_loss"] = 1.0
        r = bridge.push_check(TEST_INTENT, fault_actual)
        print(f"   结果: status={r.get('status') if r else 'N/A'}, violations={len(r.get('violations', [])) if r else 0}")

        # 4. diagnose
        print(f"4) diagnose...")
        r = bridge.trigger_diagnose(TEST_INTENT)
        if r and r.get("diagnosis"):
            print(f"   根因: {[rc.get('cause_type') for rc in r['diagnosis'].get('root_causes', [])]}")
            print(f"   置信度: {r['diagnosis'].get('confidence')}")
        else:
            print(f"   结果: {'失败' if not r else '无 diagnosis'}")

        # 5. heal
        print(f"5) heal...")
        r = bridge.trigger_heal(TEST_INTENT)
        if r:
            print(f"   verdict: {r.get('verdict')}")
            print(f"   blocked_by: {r.get('blocked_by')}")
        else:
            print(f"   结果: 失败")

        print()
        print("✅ 冒烟测试完成")
