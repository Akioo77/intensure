"""intensure DB Writer · 选择性写库 + 心跳策略

职责:
- 提供写 intensure.* 表的封装函数（不要在其他文件裸写 SQL）
- 实现保留策略（30/7 天 + 选择性写 + 心跳）
- 连接池（懒加载、自动重连）

被调方:
- controller_app.py (_stats_loop → write_network_state + write_state_history)
- controller_app.py (_log_event → write_port_event / write_link_event)
- live_env.py (main loop → write_probe_result 选择性)

不依赖 os_ken / eventlet（独立模块，便于测试）。
"""
import os
import time
import threading
from typing import Optional, Dict, Any, Tuple

try:
    import psycopg2
    from psycopg2.extras import Json
except ImportError:
    psycopg2 = None
    Json = None


# ============ 配置 ============

# DSN: 优先用环境变量，否则用开发默认值
DEFAULT_DSN = (
    'postgresql://intensure_user:intensure_dev_pwd@127.0.0.1:5432/intensure_dev'
)
DSN = os.environ.get('INTENSURE_DB_DSN', DEFAULT_DSN)

# 保障模块 DSN
DEFAULT_DSN_ASSURANCE = (
    'postgresql://assurance_user:assurance_dev_pwd@127.0.0.1:5432/intensure_dev'
)
DSN_ASSURANCE = os.environ.get('ASSURANCE_DB_DSN', DEFAULT_DSN_ASSURANCE)

# 选择性写库阈值
STATE_HISTORY_MIN_INTERVAL = 30    # 秒；state_history 写库最小间隔
PROBE_LOSS_THRESHOLD = 0.01        # 丢包>1% 视为异常
PROBE_LATENCY_THRESHOLD_MS = 200   # 延迟>200ms 视为异常
PROBE_HEARTBEAT_INTERVAL = 60      # 一致性检查心跳间隔（秒，给保障模块用）

# 调试开关
DEBUG = os.environ.get('INTENSURE_DB_DEBUG', '0') == '1'


# ============ 单例 ============

_instance = None
_assurance_instance = None
_lock = threading.Lock()


def get_writer():
    """获取全局单例（intensure_user）。"""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = DBWriter()
    return _instance


def get_assurance_writer():
    """获取全局单例（assurance_user）。"""
    global _assurance_instance
    if _assurance_instance is None:
        with _lock:
            if _assurance_instance is None:
                _assurance_instance = DBWriter(dsn=DSN_ASSURANCE)
    return _assurance_instance


class DBWriter:
    """DB 写入器 · 懒加载连接、自动重连。"""

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or DSN
        self._conn = None
        self._conn_lock = threading.Lock()

        # 选择性写库状态
        self._last_state_history_at = 0.0
        self._last_state_summary = None
        self._last_probe_state: Dict[str, dict] = {}  # dst_host -> {reachable, latency, loss}
        self._last_consistency_heartbeat: Dict[str, float] = {}  # intent_id -> epoch

        # 统计
        self.stats = {
            'network_state_writes': 0,
            'state_history_writes': 0,
            'state_history_skipped': 0,
            'port_event_writes': 0,
            'link_event_writes': 0,
            'probe_writes': 0,
            'probe_skipped': 0,
            'consistency_writes': 0,
            'consistency_skipped': 0,
            'errors': 0,
        }

    # ---------- 连接管理 ----------

    def _get_conn(self):
        """懒加载 + 自动重连。"""
        if psycopg2 is None:
            raise RuntimeError('psycopg2 not installed')
        with self._conn_lock:
            try:
                if self._conn is None or self._conn.closed:
                    self._conn = psycopg2.connect(self.dsn)
                    self._conn.autocommit = True
                else:
                    # 检查连接是否还活着
                    try:
                        with self._conn.cursor() as cur:
                            cur.execute('SELECT 1')
                    except Exception:
                        self._conn.close()
                        self._conn = psycopg2.connect(self.dsn)
                        self._conn.autocommit = True
            except Exception as e:
                self.stats['errors'] += 1
                if DEBUG:
                    print(f'[DBWriter] connection error: {e}', flush=True)
                raise
            return self._conn

    def close(self):
        """关闭连接（测试用）。"""
        with self._conn_lock:
            if self._conn and not self._conn.closed:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---------- 读（查询） ----------

    def query(self, sql, params=None):
        """只读查询，返回 list of dict。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] query error: {e}', flush=True)
            return []

    def query_one(self, sql, params=None):
        """只读查询，返回单行 dict 或 None。"""
        rows = self.query(sql, params or ())
        return rows[0] if rows else None

    # ---------- network_state (单行 upsert, 每次都写) ----------

    def write_network_state(self, payload: dict, version: Optional[int] = None):
        """单行 upsert；保障模块每 5s 读这个。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # 不指定 version，让 DB 自动 +1（每次采集 +1）
                cur.execute("""
                    INSERT INTO intensure.network_state (state_key, payload, version)
                    VALUES ('current', %s::jsonb, COALESCE(%s, 1))
                    ON CONFLICT (state_key) DO UPDATE SET
                        payload = EXCLUDED.payload,
                        version = intensure.network_state.version + 1,
                        captured_at = now()
                """, (Json(payload), version))
            self.stats['network_state_writes'] += 1
            return True
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_network_state error: {e}', flush=True)
            return False

    # ---------- state_history (选择性写：30s 节流 + 变化触发) ----------

    def write_state_history_if_needed(self, payload: dict, summary: dict,
                                       reason: str = 'scheduled',
                                       force: bool = False) -> bool:
        """选择性写库：
        - summary 变化 → 写（reason='on-change'）
        - 距上次 > 30s → 写（reason='scheduled'）
        - 否则跳过
        """
        now = time.time()
        summary_changed = (self._last_state_summary != summary)
        time_passed = (now - self._last_state_history_at) > STATE_HISTORY_MIN_INTERVAL

        if not force and not summary_changed and not time_passed:
            self.stats['state_history_skipped'] += 1
            return False

        actual_reason = 'on-change' if summary_changed else reason

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intensure.state_history (payload, summary, capture_reason)
                    VALUES (%s::jsonb, %s::jsonb, %s)
                """, (Json(payload), Json(summary), actual_reason))
            self._last_state_history_at = now
            self._last_state_summary = summary
            self.stats['state_history_writes'] += 1
            return True
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_state_history error: {e}', flush=True)
            return False

    # ---------- port_events / link_events (变化时才调进来) ----------

    def write_port_event(self, dpid: str, port_no: int, port_name: str,
                         event_type: str, speed_mbps: Optional[int] = None,
                         metadata: Optional[dict] = None):
        """写端口事件（调用方负责判断是否变化）。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intensure.port_events
                        (dpid, port_no, port_name, event_type, speed_mbps, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """, (dpid, port_no, port_name, event_type, speed_mbps,
                      Json(metadata or {})))
            self.stats['port_event_writes'] += 1
            return True
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_port_event error: {e}', flush=True)
            return False

    def write_link_event(self, src_dpid: str, src_port: int,
                         dst_dpid: str, dst_port: int, event_type: str,
                         metadata: Optional[dict] = None):
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intensure.link_events
                        (src_dpid, src_port, dst_dpid, dst_port, event_type, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                """, (src_dpid, src_port, dst_dpid, dst_port, event_type,
                      Json(metadata or {})))
            self.stats['link_event_writes'] += 1
            return True
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_link_event error: {e}', flush=True)
            return False

    # ---------- probe_results (选择性：异常才写) ----------

    def should_write_probe(self, dst_host: str, reachable: bool,
                           loss: float, latency_ms: Optional[float]) -> Tuple[bool, Optional[str]]:
        """判定探针结果是否异常（决定要不要写库）。

        Returns:
            (is_anomaly, anomaly_type)
            - is_anomaly=True: 必须写库
            - anomaly_type: 'first'/'reachability_change'/'high_loss'/'high_latency'
        """
        prev = self._last_probe_state.get(dst_host)
        is_anomaly = False
        anomaly_type = None

        if prev is None:
            is_anomaly = True
            anomaly_type = 'first'
        elif reachable != prev.get('reachable'):
            is_anomaly = True
            anomaly_type = 'reachability_change'
        elif loss > PROBE_LOSS_THRESHOLD:
            is_anomaly = True
            anomaly_type = 'high_loss'
        elif latency_ms is not None and latency_ms > PROBE_LATENCY_THRESHOLD_MS:
            is_anomaly = True
            anomaly_type = 'high_latency'

        # 总是更新状态（即使不写库）
        self._last_probe_state[dst_host] = {
            'reachable': reachable,
            'latency': latency_ms,
            'loss': loss,
        }
        return is_anomaly, anomaly_type

    def write_probe_result(self, src_host: str, dst_host: str,
                           reachable: bool, loss_pct: float,
                           latency_ms: Optional[float] = None,
                           probe_method: str = 'ping',
                           is_anomaly: bool = False,
                           anomaly_type: Optional[str] = None) -> bool:
        """写探针结果（推荐先用 should_write_probe 判定）。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intensure.probe_results
                        (src_host, dst_host, reachable, latency_ms, packet_loss,
                         probe_method, is_anomaly, anomaly_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (src_host, dst_host, reachable, latency_ms, loss_pct,
                      probe_method, is_anomaly, anomaly_type))
            self.stats['probe_writes'] += 1
            return True
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_probe_result error: {e}', flush=True)
            return False

    def write_probe_with_decision(self, src_host: str, dst_host: str,
                                  reachable: bool, loss_pct: float,
                                  latency_ms: Optional[float] = None,
                                  probe_method: str = 'ping') -> bool:
        """一站式：判定 + 写（如异常则写）。返回是否写了。"""
        is_anomaly, anomaly_type = self.should_write_probe(dst_host, reachable, loss_pct, latency_ms)
        if is_anomaly:
            return self.write_probe_result(
                src_host, dst_host, reachable, loss_pct, latency_ms,
                probe_method, is_anomaly=True, anomaly_type=anomaly_type
            )
        self.stats['probe_skipped'] += 1
        return False

    # ---------- consistency_checks (选择性：变化时 + 心跳) ----------

    def should_write_consistency(self, intent_id: str, result: str,
                                 is_diagnosing: bool = False) -> Tuple[bool, bool]:
        """判定一致性检查是否要写库。

        Returns:
            (should_write, is_heartbeat)
            - should_write=True: 状态变化 / 心跳 / diagnosing 阶段都写
            - is_heartbeat=True: 心跳；False: 实际状态变化
        """
        prev_state = getattr(self, '_consistency_result_state', {})
        prev_result = prev_state.get(intent_id)
        now = time.time()
        last_hb = self._last_consistency_heartbeat.get(intent_id, 0)

        is_heartbeat = False

        if is_diagnosing:
            # 诊断阶段：每次都写
            is_heartbeat = False
            should_write = True
        elif prev_result is None or result != prev_result:
            # 状态变化
            is_heartbeat = False
            should_write = True
        elif (now - last_hb) > PROBE_HEARTBEAT_INTERVAL:
            # 心跳
            is_heartbeat = True
            should_write = True
        else:
            should_write = False

        if should_write:
            self._last_consistency_heartbeat[intent_id] = now
            prev_state[intent_id] = result
            self._consistency_result_state = prev_state

        return should_write, is_heartbeat

    def write_consistency_check(self, intent_id: str, check_type: str,
                                 expected_state: dict, actual_state: dict,
                                 result: str, violation_types: list,
                                 evidence: dict, is_heartbeat: bool = False,
                                 duration_ms: Optional[int] = None) -> bool:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO assurance.consistency_checks
                        (intent_id, check_type, expected_state, actual_state,
                         result, violation_types, evidence, is_heartbeat, duration_ms)
                    VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s::text[], %s::jsonb, %s, %s)
                """, (intent_id, check_type, Json(expected_state), Json(actual_state),
                      result, violation_types or [], Json(evidence), is_heartbeat, duration_ms))
            self.stats['consistency_writes'] += 1
            return True
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_consistency_check error: {e}', flush=True)
            return False

    # ---------- 诊断 / 自愈（直接写） ----------

    def write_diagnosis(self, intent_id: str, root_causes: list,
                        confidence: float, affected_devices: list,
                        evidence: dict,
                        consistency_check_id: Optional[int] = None,
                        checked_at_ref: Optional[str] = None) -> Optional[str]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO assurance.diagnoses
                        (intent_id, root_causes, confidence, affected_devices, evidence,
                         consistency_check_id, checked_at_ref)
                    VALUES (%s, %s::jsonb, %s, %s::text[], %s::jsonb, %s, %s)
                    RETURNING id
                """, (intent_id, Json(root_causes), confidence, affected_devices,
                      Json(evidence), consistency_check_id, checked_at_ref))
                return str(cur.fetchone()[0])
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_diagnosis error: {e}', flush=True)
            return None

    def write_healing_intent(self, intent_id: str, diagnosis_id: Optional[str],
                             strategy: str, primitives: list,
                             risk: str, guard_checks: dict,
                             verdict: str, blocked_by: list,
                             parent_intent_id: Optional[str] = None,
                             ttl_seconds: int = 120) -> Optional[str]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO assurance.healing_intents
                        (intent_id, diagnosis_id, parent_intent_id, strategy, primitives,
                         risk, guard_checks, verdict, blocked_by, ttl_seconds)
                    VALUES (%s, %s::uuid, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s::text[], %s)
                    RETURNING id
                """, (intent_id, diagnosis_id, parent_intent_id, strategy,
                      Json(primitives), risk, Json(guard_checks), verdict,
                      blocked_by or [], ttl_seconds))
                return str(cur.fetchone()[0])
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_healing_intent error: {e}', flush=True)
            return None

    # ---------- 审计 ----------

    def write_audit(self, module_name: str, actor: str, action: str,
                    target: Optional[str] = None, severity: str = 'INFO',
                    payload: Optional[dict] = None,
                    correlation_id: Optional[str] = None) -> bool:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO shared.audit_log
                        (module_name, actor, action, target, severity, payload, correlation_id)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::uuid)
                """, (module_name, actor, action, target, severity,
                      Json(payload or {}), correlation_id))
            return True
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_audit error: {e}', flush=True)
            return False

    # ---------- 心跳 ----------

    def write_heartbeat(self, status: str = 'healthy',
                        version: Optional[str] = None,
                        metadata: Optional[dict] = None) -> bool:
        """写自己的心跳到 shared.module_health（RLS 限定只能写自己的行）。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO shared.module_health (module_name, status, version, metadata)
                    VALUES ('intensure', %s, %s, %s::jsonb)
                    ON CONFLICT (module_name) DO UPDATE SET
                        last_heartbeat = now(),
                        status = EXCLUDED.status,
                        version = EXCLUDED.version,
                        metadata = EXCLUDED.metadata
                """, (status, version, Json(metadata or {})))
            return True
        except Exception as e:
            self.stats['errors'] += 1
            if DEBUG:
                print(f'[DBWriter] write_heartbeat error: {e}', flush=True)
            return False

    def get_stats(self) -> dict:
        """返回累计统计（用于监控）。"""
        return dict(self.stats)


# ============ 便捷函数（直接调用） ============

def write_network_state(payload: dict, version: Optional[int] = None):
    return get_writer().write_network_state(payload, version)


def write_state_history_if_needed(payload: dict, summary: dict, reason: str = 'scheduled'):
    return get_writer().write_state_history_if_needed(payload, summary, reason)


def write_port_event(dpid: str, port_no: int, port_name: str,
                     event_type: str, speed_mbps: Optional[int] = None):
    return get_writer().write_port_event(dpid, port_no, port_name, event_type, speed_mbps)


def write_link_event(src_dpid: str, src_port: int, dst_dpid: str, dst_port: int, event_type: str):
    return get_writer().write_link_event(src_dpid, src_port, dst_dpid, dst_port, event_type)


def write_probe_with_decision(src_host: str, dst_host: str, reachable: bool,
                               loss_pct: float, latency_ms: Optional[float] = None):
    return get_writer().write_probe_with_decision(src_host, dst_host, reachable, loss_pct, latency_ms)


def write_audit(action: str, target: Optional[str] = None, severity: str = 'INFO',
                payload: Optional[dict] = None, correlation_id: Optional[str] = None):
    return get_writer().write_audit('intensure', 'system', action, target, severity,
                                    payload, correlation_id)


def write_heartbeat(status: str = 'healthy', version: Optional[str] = None):
    return get_writer().write_heartbeat(status, version)


if __name__ == '__main__':
    # CLI 自测（快速验证连接）
    w = get_writer()
    print('--- DBWriter self-test ---')
    ok = w.write_network_state({'test': True, 'ts': time.time()})
    print(f'write_network_state: {ok}')
    ok = w.write_audit('intensure', 'system', 'test_action', severity='INFO', payload={'test': True})
    print(f'write_audit: {ok}')
    ok = w.write_heartbeat(status='healthy', version='3.5')
    print(f'write_heartbeat: {ok}')
    print('stats:', w.get_stats())

    # 保障模块读测试
    print()
    print('--- Assurance writer read test ---')
    aw = get_assurance_writer()
    ns = aw.query_one('SELECT version, captured_at FROM intensure.network_state LIMIT 1')
    print(f'network_state read: {ns}')
    policies = aw.query('SELECT intent_id, intent_type FROM implementation.policies WHERE status=%s LIMIT 3', ('active',))
    print(f'active policies: {len(policies)}')