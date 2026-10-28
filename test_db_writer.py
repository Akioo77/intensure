#!/usr/bin/env python3
"""test_db_writer.py — db_writer.py 单元测试

覆盖 12 个 write_* 方法 + 判定/查询方法 + 连接管理：
  1. 每个 write_*: 成功路径 + 失败路径（不崩、返回 False/None、errors 计数 +1）
  2. 选择性写库判定：should_write_probe / should_write_consistency 的全部边界
  3. state_history 节流：30s 节流 + 变化触发 + force
  4. 连接管理：懒加载 / 复用 / 探活失败重连
  5. 真实 DB 集成（DB 不可用则跳过）：network_state upsert 幂等 + 实际落库

运行: python3 -m pytest test_db_writer.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db_writer

FAILURES = []


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        FAILURES.append(name)
        raise AssertionError(f"{name} {detail}")


# ============ Fake DB ============

class FakeCursor:
    """模拟 psycopg2 cursor。"""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.fail:
            raise RuntimeError("mock execute failure")

    def fetchone(self):
        return self.conn.fetchone_result

    def fetchall(self):
        return self.conn.fetchall_result

    @property
    def description(self):
        return self.conn.description


class FakeConn:
    """模拟 psycopg2 connection。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.closed = False
        self.autocommit = False
        self.executed = []
        self.fetchone_result = ("fake-uuid-1234",)
        self.fetchall_result = [("a", 1), ("b", 2)]
        self.description = [("col_a",), ("col_b",)]

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakeWriter(db_writer.DBWriter):
    """注入 FakeConn 的 DBWriter。

    注意: 类名刻意不叫 Test* —— pytest 会把 Test* 当测试类收集。
    """

    def __init__(self, fail=False):
        super().__init__(dsn="postgresql://fake/fake")
        self.fake = FakeConn(fail=fail)

    def _get_conn(self):
        return self.fake


def _sqls(w):
    return [s for s, _ in w.fake.executed]


# ============ 1. 连接管理 ============

def test_connection_lazy_load_and_reuse():
    """_get_conn 懒加载 + 复用同一连接。"""
    calls = []

    class FakePsycopg2:
        # _get_conn 现在会带 connect_timeout, 并引用 OperationalError/InterfaceError
        OperationalError = _FakeOperationalError
        InterfaceError = _FakeInterfaceError

        @staticmethod
        def connect(dsn, connect_timeout=None):
            calls.append(dsn)
            c = FakeConn()
            c.closed = False
            return c

    old = db_writer.psycopg2
    db_writer.psycopg2 = FakePsycopg2
    try:
        w = db_writer.DBWriter(dsn="postgresql://x/y")
        c1 = w._get_conn()
        c2 = w._get_conn()
        check("首次连接会调 psycopg2.connect", len(calls) == 1, f"calls={calls}")
        check("第二次复用同一连接（不重连）", c1 is c2, "不是同一对象")
    finally:
        db_writer.psycopg2 = old


def test_connection_reconnect_when_dead():
    """连接探测失败应重连，而不是继续用死连接。"""
    calls = []

    class DeadConn(FakeConn):
        def cursor(self):
            class C(FakeCursor):
                def execute(self, sql, params=None):
                    # 必须是 OperationalError/InterfaceError 才会触发重连;
                    # 其它异常会被当成「查询本身出错」而直接冒泡(这是有意的设计)
                    raise _FakeOperationalError("connection dead")
            return C(self)

    class FakePsycopg2:
        OperationalError = _FakeOperationalError
        InterfaceError = _FakeInterfaceError

        @staticmethod
        def connect(dsn, connect_timeout=None):
            calls.append(dsn)
            return DeadConn()

    old = db_writer.psycopg2
    db_writer.psycopg2 = FakePsycopg2
    try:
        w = db_writer.DBWriter(dsn="postgresql://x/y")
        w._get_conn()
        w._get_conn()
        check("探活失败会重连", len(calls) == 2, f"calls={calls}")
    finally:
        db_writer.psycopg2 = old


def test_close_closes_connection():
    """close() 应关闭底层连接并置空。"""
    w = FakeWriter()
    w._conn = w.fake
    w.close()
    check("close() 后连接被关闭", w.fake.closed is True)
    check("close() 后 _conn 置空", w._conn is None)


# ============ 2. write_network_state ============

def test_write_network_state_success():
    """成功路径：返回 True + 计数 +1 + SQL 指向 network_state。"""
    w = FakeWriter()
    ok = w.write_network_state({"meta": {"update_count": 1}})
    check("write_network_state 返回 True", ok is True)
    check("network_state_writes 计数 +1", w.stats["network_state_writes"] == 1)
    check("errors 计数未动", w.stats["errors"] == 0)
    check("SQL 写的是 network_state 表",
          any("intensure.network_state" in s for s in _sqls(w)), _sqls(w))
    check("用了 ON CONFLICT upsert",
          any("ON CONFLICT" in s for s in _sqls(w)))


def test_write_network_state_failure():
    """失败路径：返回 False + errors 计数 +1，不抛异常。"""
    w = FakeWriter(fail=True)
    ok = w.write_network_state({"meta": {}})
    check("写失败返回 False（不抛）", ok is False)
    check("errors 计数 +1", w.stats["errors"] == 1)
    check("network_state_writes 未增加", w.stats["network_state_writes"] == 0)


# ============ 3. write_state_history_if_needed（选择性写）============

def test_state_history_writes_on_first_call():
    """首次调用 summary 从 None 变过来 → 写。"""
    w = FakeWriter()
    ok = w.write_state_history_if_needed({"a": 1}, {"up": 2})
    check("首次调用会写", ok is True)
    check("state_history_writes +1", w.stats["state_history_writes"] == 1)


def test_state_history_skips_when_unchanged():
    """summary 未变且 30s 内 → 跳过（这是「选择性写库」的核心）。"""
    w = FakeWriter()
    w.write_state_history_if_needed({"a": 1}, {"up": 2})
    before = w.stats["state_history_skipped"]
    ok = w.write_state_history_if_needed({"a": 1}, {"up": 2})
    check("summary 未变且未超时 → 跳过", ok is False)
    check("skipped 计数 +1", w.stats["state_history_skipped"] == before + 1)
    check("只写了 1 次（未重复写）", w.stats["state_history_writes"] == 1)


def test_state_history_writes_on_change():
    """summary 变了 → 立即写（reason=on-change）。"""
    w = FakeWriter()
    w.write_state_history_if_needed({"a": 1}, {"up": 2})
    ok = w.write_state_history_if_needed({"a": 2}, {"up": 3})
    check("summary 变化 → 写", ok is True)
    check("共写 2 次", w.stats["state_history_writes"] == 2)
    # capture_reason 是作为 SQL 参数传的, 不在 SQL 文本里 —— 要查 params
    params = w.fake.executed[-1][1]
    check("reason 标记为 on-change", "on-change" in params, f"params={params}")
    check("第 3 个参数是 reason", len(params) >= 3, f"params={params}")


def test_state_history_force_bypasses_throttle():
    """force=True 时无视节流强制写。"""
    w = FakeWriter()
    w.write_state_history_if_needed({"a": 1}, {"up": 2})
    ok = w.write_state_history_if_needed({"a": 1}, {"up": 2}, force=True)
    check("force=True 强制写", ok is True)
    check("共写 2 次", w.stats["state_history_writes"] == 2)


def test_state_history_writes_after_interval():
    """超过 30s 节流窗口 → 写（reason=scheduled）。"""
    w = FakeWriter()
    w.write_state_history_if_needed({"a": 1}, {"up": 2})
    w._last_state_history_at = time.time() - (db_writer.STATE_HISTORY_MIN_INTERVAL + 1)
    ok = w.write_state_history_if_needed({"a": 1}, {"up": 2})
    check("超过节流窗口 → 写", ok is True)
    check("共写 2 次", w.stats["state_history_writes"] == 2)


def test_state_history_failure():
    """失败路径：返回 False + errors +1。"""
    w = FakeWriter(fail=True)
    ok = w.write_state_history_if_needed({"a": 1}, {"up": 2})
    check("写失败返回 False", ok is False)
    check("errors 计数 +1", w.stats["errors"] == 1)


# ============ 4. 事件写入 ============

def test_write_port_event():
    """端口事件：成功 + 失败。"""
    w = FakeWriter()
    ok = w.write_port_event("0000000000000001", 1, "s1-eth1", "link_down")
    check("write_port_event 返回 True", ok is True)
    check("port_event_writes +1", w.stats["port_event_writes"] == 1)
    check("SQL 写的是 port_events",
          any("intensure.port_events" in s for s in _sqls(w)))
    wf = FakeWriter(fail=True)
    check("失败返回 False", wf.write_port_event("d", 1, "n", "e") is False)
    check("失败 errors +1", wf.stats["errors"] == 1)


def test_write_link_event():
    """链路事件：成功 + 失败。"""
    w = FakeWriter()
    ok = w.write_link_event("dpid1", 1, "dpid2", 2, "discovered")
    check("write_link_event 返回 True", ok is True)
    check("link_event_writes +1", w.stats["link_event_writes"] == 1)
    check("SQL 写的是 link_events",
          any("intensure.link_events" in s for s in _sqls(w)))
    wf = FakeWriter(fail=True)
    check("失败返回 False", wf.write_link_event("a", 1, "b", 2, "e") is False)


# ============ 5. 探针（选择性写：异常才写）============

def test_should_write_probe_first():
    """第一次探测 → anomaly_type=first。"""
    w = FakeWriter()
    is_anom, atype = w.should_write_probe("10.0.0.4", True, 0.0, 10)
    check("首次探测判为异常", is_anom is True)
    check("anomaly_type=first", atype == "first", atype)


def test_should_write_probe_reachability_change():
    """可达性翻转 → reachability_change。"""
    w = FakeWriter()
    w.should_write_probe("10.0.0.4", True, 0.0, 10)
    is_anom, atype = w.should_write_probe("10.0.0.4", False, 0.0, None)
    check("可达性翻转判为异常", is_anom is True)
    check("anomaly_type=reachability_change", atype == "reachability_change", atype)


def test_should_write_probe_high_loss():
    """丢包超阈值 → high_loss。"""
    w = FakeWriter()
    w.should_write_probe("10.0.0.4", True, 0.0, 10)
    is_anom, atype = w.should_write_probe("10.0.0.4", True, 0.5, 10)
    check("高丢包判为异常", is_anom is True)
    check("anomaly_type=high_loss", atype == "high_loss", atype)


def test_should_write_probe_high_latency():
    """延迟超阈值 → high_latency。"""
    w = FakeWriter()
    w.should_write_probe("10.0.0.4", True, 0.0, 10)
    is_anom, atype = w.should_write_probe("10.0.0.4", True, 0.0, 9999)
    check("高延迟判为异常", is_anom is True)
    check("anomaly_type=high_latency", atype == "high_latency", atype)


def test_should_write_probe_normal_not_anomaly():
    """一切正常 → 不写库。"""
    w = FakeWriter()
    w.should_write_probe("10.0.0.4", True, 0.0, 10)
    is_anom, atype = w.should_write_probe("10.0.0.4", True, 0.0, 10)
    check("正常状态不判异常", is_anom is False)
    check("anomaly_type=None", atype is None, atype)


def test_write_probe_with_decision():
    """正常 → 跳过写库；异常 → 写。"""
    w = FakeWriter()
    wrote = w.write_probe_with_decision("h1", "10.0.0.4", True, 0.0, 10)
    check("首次探测会写库", wrote is True)
    wrote2 = w.write_probe_with_decision("h1", "10.0.0.4", True, 0.0, 10)
    check("正常状态跳过写库", wrote2 is False)
    check("probe_skipped +1", w.stats["probe_skipped"] == 1)
    check("只写 1 次", w.stats["probe_writes"] == 1)


def test_write_probe_result_failure():
    """失败路径。"""
    w = FakeWriter(fail=True)
    ok = w.write_probe_result("h1", "h4", True, 0.0, 10)
    check("失败返回 False", ok is False)
    check("errors +1", w.stats["errors"] == 1)


# ============ 6. 一致性检查（选择性：变化 + 心跳）============

def test_should_write_consistency_first():
    """首次 → 写（非心跳）。"""
    w = FakeWriter()
    should, hb = w.should_write_consistency("intent-1", "pass")
    check("首次应写", should is True)
    check("首次不是心跳", hb is False)


def test_should_write_consistency_unchanged():
    """结果没变且心跳未到期 → 不写。"""
    w = FakeWriter()
    w.should_write_consistency("intent-1", "pass")
    should, hb = w.should_write_consistency("intent-1", "pass")
    check("结果未变 → 不写", should is False)
    check("非心跳", hb is False)


def test_should_write_consistency_changed():
    """结果变化 → 写（非心跳）。"""
    w = FakeWriter()
    w.should_write_consistency("intent-1", "pass")
    should, hb = w.should_write_consistency("intent-1", "violated")
    check("结果变化 → 写", should is True)
    check("变化不是心跳", hb is False)


def test_should_write_consistency_heartbeat():
    """结果没变但心跳到期 → 写且标记为心跳。"""
    w = FakeWriter()
    w.should_write_consistency("intent-1", "pass")
    w._last_consistency_heartbeat["intent-1"] = (
        time.time() - (db_writer.PROBE_HEARTBEAT_INTERVAL + 1))
    should, hb = w.should_write_consistency("intent-1", "pass")
    check("心跳到期 → 写", should is True)
    check("标记为心跳", hb is True)


def test_should_write_consistency_diagnosing():
    """诊断阶段每次都写，且不算心跳。"""
    w = FakeWriter()
    w.should_write_consistency("intent-1", "pass")
    should, hb = w.should_write_consistency("intent-1", "pass", is_diagnosing=True)
    check("诊断阶段 → 写", should is True)
    check("诊断阶段不算心跳", hb is False)


def test_write_consistency_check():
    """一致性检查落库：成功 + 失败。"""
    w = FakeWriter()
    ok = w.write_consistency_check("intent-1", "reachability",
                                {"reachable": True}, {"reachable": False},
                                "violated", ["unreachable"], {"down_ports": ["s1:eth1"]})
    check("write_consistency_check 返回 True", ok is True)
    check("consistency_writes +1", w.stats["consistency_writes"] == 1)
    check("SQL 写的是 assurance.consistency_checks",
          any("assurance.consistency_checks" in s for s in _sqls(w)))
    wf = FakeWriter(fail=True)
    check("失败返回 False",
          wf.write_consistency_check("i", "t", {}, {}, "pass", [], {}) is False)


# ============ 7. 诊断 / 自愈 / 审计 / 心跳 ============

def test_write_diagnosis():
    """诊断：成功返回 id 字符串；失败返回 None。"""
    w = FakeWriter()
    rid = w.write_diagnosis("intent-1", [{"type": "port_failure"}], 0.85,
                           ["s1"], {"down_ports": ["s1:eth1"]})
    check("成功返回 id 字符串", isinstance(rid, str), repr(rid))
    check("SQL 写的是 assurance.diagnoses",
          any("assurance.diagnoses" in s for s in _sqls(w)))
    wf = FakeWriter(fail=True)
    check("失败返回 None",
          wf.write_diagnosis("i", [], 0.5, [], {}) is None)


def test_write_healing_intent():
    """自愈意图：成功返回 id；失败返回 None。"""
    w = FakeWriter()
    rid = w.write_healing_intent("intent-1", None, "reset_link",
                                [{"type": "port_up"}], "low", {},
                                "approved", [])
    check("成功返回 id 字符串", isinstance(rid, str), repr(rid))
    check("SQL 写的是 assurance.healing_intents",
          any("assurance.healing_intents" in s for s in _sqls(w)))
    wf = FakeWriter(fail=True)
    check("失败返回 None",
          wf.write_healing_intent("i", None, "s", [], "low", {}, "v", []) is None)


def test_write_audit():
    """审计日志：成功 + 失败。"""
    w = FakeWriter()
    ok = w.write_audit("intensure", "operator", "inject_fault",
                      target="s1-eth1", severity="WARN")
    check("write_audit 返回 True", ok is True)
    check("SQL 写的是 shared.audit_log",
          any("shared.audit_log" in s for s in _sqls(w)))
    wf = FakeWriter(fail=True)
    check("失败返回 False", wf.write_audit("m", "a", "act") is False)


def test_write_heartbeat():
    """心跳：成功 + 失败。"""
    w = FakeWriter()
    ok = w.write_heartbeat("healthy", "3.10.4")
    check("write_heartbeat 返回 True", ok is True)
    check("SQL 写的是 shared.module_health",
          any("shared.module_health" in s for s in _sqls(w)))
    check("心跳写的是自己的 module_name=intensure",
          any("intensure" in s for s in _sqls(w)))
    wf = FakeWriter(fail=True)
    check("失败返回 False", wf.write_heartbeat() is False)


# ============ 8. 查询 ============

def test_query_returns_dicts():
    """query 返回 list[dict]，按列名映射。"""
    w = FakeWriter()
    rows = w.query("SELECT a, b FROM t")
    check("query 返回 2 行", len(rows) == 2, f"rows={rows}")
    check("行是 dict", isinstance(rows[0], dict))
    check("列名正确映射", rows[0] == {"col_a": "a", "col_b": 1}, rows[0])


def test_query_failure_returns_empty():
    """查询失败 → 空列表 + errors +1（不抛）。"""
    w = FakeWriter(fail=True)
    rows = w.query("SELECT 1")
    check("查询失败返回 []", rows == [])
    check("errors +1", w.stats["errors"] == 1)


def test_query_one():
    """query_one 返回首行 dict；空结果返回 None。"""
    w = FakeWriter()
    row = w.query_one("SELECT a, b FROM t")
    check("query_one 返回 dict", isinstance(row, dict))
    w2 = FakeWriter()
    w2.fake.fetchall_result = []
    check("空结果返回 None", w2.query_one("SELECT 1") is None)


# ============ 9. 真实 DB 集成（DB 不可用则跳过）============

def _real_writer():
    """拿到真实 DBWriter；连不上返回 None。"""
    try:
        w = db_writer.DBWriter()
        w._get_conn()
        return w
    except Exception:
        return None


def test_real_db_network_state_upsert():
    """真实库：network_state upsert 后 version 递增，且只有一行。"""
    w = _real_writer()
    if w is None:
        print("[SKIP] 真实 DB 不可达，跳过")
        return
    before = w.query_one("SELECT version FROM intensure.network_state WHERE state_key='current'")
    v0 = before["version"] if before else 0
    ok = w.write_network_state({"meta": {"update_count": 999999},
                             "switches": {}, "hosts": {}})
    check("真实库写入成功", ok is True)
    after = w.query_one("SELECT version FROM intensure.network_state WHERE state_key='current'")
    check("version 递增", after["version"] == v0 + 1, f"before={v0}")
    n = w.query_one("SELECT count(*) AS c FROM intensure.network_state")
    check("network_state 仍只有一行（upsert 非 insert）", n["c"] == 1, n)


def test_real_db_state_history_is_idempotent_guarded():
    """真实库：节流生效 —— 连续两次同样 summary 只落一行。"""
    w = _real_writer()
    if w is None:
        print("[SKIP] 真实 DB 不可达，跳过")
        return
    # 跑前先清掉历史残留（否则这个测试只能跑一次，第二次就失败）
    marker = "idempotent-test-9999"
    w.query("DELETE FROM intensure.state_history "
            "WHERE summary->>'probe' = '%s'" % marker)
    try:
        w._last_state_summary = None
        w._last_state_history_at = 0.0
        summary = {"probe": marker}
        payload = {"meta": {"probe": marker}}
        first = w.write_state_history_if_needed(payload, summary)
        second = w.write_state_history_if_needed(payload, summary)
        check("第一次写", first is True)
        check("第二次被节流跳过", second is False)
        n = w.query_one("SELECT count(*) AS c FROM intensure.state_history "
                        "WHERE summary->>'probe' = '%s'" % marker)
        check("DB 里只落了一行", n["c"] == 1, n)
    finally:
        # 跑完清掉，别污染真实库
        w.query("DELETE FROM intensure.state_history "
                "WHERE summary->>'probe' = '%s'" % marker)


def test_query_with_literal_percent():
    """query() 带字面量 % (如 LIKE) 不能静默返回空。

    2026-10-26 实测 bug: query() 原本 cur.execute(sql, params or ())，
    空元组触发 %-插值，SQL 里字面量 % 被当格式符 -> 报错 -> except 吞掉
    -> 返回 []。表现为「含 LIKE 的查询永远查不到数据」。
    query_one() 也有同样问题（它也传了 params or ()）。
    """
    w = FakeWriter()
    w.query("SELECT 1 WHERE name LIKE 'a%'")
    _sql, params = w.fake.executed[-1]
    check("params=None 时不传空元组", params is None, repr(params))

    w2 = FakeWriter()
    w2.query("SELECT 1 WHERE id=%s", ("x",))
    _sql2, params2 = w2.fake.executed[-1]
    check("有 params 时正常传参", params2 == ("x",), repr(params2))

    w3 = FakeWriter()
    w3.query_one("SELECT 1 WHERE name LIKE 'b%'")
    _sql3, params3 = w3.fake.executed[-1]
    check("query_one 也不传空元组", params3 is None, repr(params3))


class _FakeOperationalError(Exception):
    pass


class _FakeInterfaceError(Exception):
    pass


class _FakePsycopg2:
    """假的 psycopg2 —— 可控 connect 行为。"""

    OperationalError = _FakeOperationalError
    InterfaceError = _FakeInterfaceError

    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times
        self.timeouts = []

    def connect(self, dsn, connect_timeout=None):
        self.calls += 1
        self.timeouts.append(connect_timeout)
        if self.calls <= self.fail_times:
            raise _FakeOperationalError('server closed the connection unexpectedly')
        return FakeConn()


class _FakeTime:
    """假的 time 模块 —— 只记录 sleep 时长, 不真睡。"""

    def __init__(self, real):
        self._real = real
        self.delays = []

    def sleep(self, d):
        self.delays.append(d)

    def __getattr__(self, k):
        return getattr(self._real, k)


def test_get_conn_retries_on_connection_death():
    """PG 短暂不可用 -> 自动重试并拿到连接（P2-4 核心行为）。"""
    old_psycopg2 = db_writer.psycopg2
    old_time = db_writer.time
    fake = _FakePsycopg2(fail_times=2)
    ft = _FakeTime(old_time)
    db_writer.psycopg2 = fake
    db_writer.time = ft
    try:
        w = db_writer.DBWriter(dsn="postgresql://fake/fake")
        conn = w._get_conn()
        check("重试后拿到连接", conn is not None)
        check("尝试了 3 次", fake.calls == 3, fake.calls)
        check("退避是指数的 [0.5, 1.0]", ft.delays == [0.5, 1.0], ft.delays)
        check("connect 带了 timeout", all(t is not None for t in fake.timeouts), fake.timeouts)
        # reconnects = 成功(重)建连次数; connect_retries = 失败重试次数
        check("成功建连 1 次", w.get_stats()["reconnects"] == 1, w.get_stats())
        check("失败重试 2 次", w.get_stats()["connect_retries"] == 2, w.get_stats())
        check("errors 仍为 0", w.get_stats()["errors"] == 0, w.get_stats())
    finally:
        db_writer.psycopg2 = old_psycopg2
        db_writer.time = old_time


def test_get_conn_gives_up_after_max_attempts():
    """一直连不上 -> 重试到上限后抛出, errors 计数 +1。"""
    old_psycopg2 = db_writer.psycopg2
    old_time = db_writer.time
    fake = _FakePsycopg2(fail_times=99)
    ft = _FakeTime(old_time)
    db_writer.psycopg2 = fake
    db_writer.time = ft
    try:
        w = db_writer.DBWriter(dsn="postgresql://fake/fake")
        raised = False
        try:
            w._get_conn()
        except _FakeOperationalError:
            raised = True
        check("最终抛出连接错误", raised is True)
        check("尝试次数 = 上限", fake.calls == db_writer.CONNECT_MAX_ATTEMPTS, fake.calls)
        check("errors 计数 +1", w.get_stats()["errors"] == 1, w.get_stats())
    finally:
        db_writer.psycopg2 = old_psycopg2
        db_writer.time = old_time


def test_get_conn_does_not_retry_on_query_error():
    """非连接类异常(语法错/权限不足) -> 不重连, 立刻冒泡（免得掩盖真因）。"""
    old_psycopg2 = db_writer.psycopg2
    old_time = db_writer.time

    class BadConn(FakeConn):
        def cursor(self):
            c = FakeCursor(self)

            def execute(sql, params=None):
                raise ValueError('syntax error at or near "FOO"')

            c.execute = execute
            return c

    class FakePy2:
        OperationalError = _FakeOperationalError
        InterfaceError = _FakeInterfaceError

        def __init__(self):
            self.calls = 0

        def connect(self, dsn, connect_timeout=None):
            self.calls += 1
            return BadConn()

    fake = FakePy2()
    ft = _FakeTime(old_time)
    db_writer.psycopg2 = fake
    db_writer.time = ft
    try:
        w = db_writer.DBWriter(dsn="postgresql://fake/fake")
        w._conn = fake.connect("postgresql://fake/fake")
        raised = False
        try:
            w._get_conn()
        except ValueError:
            raised = True
        check("查询类异常直接冒泡", raised is True)
        check("没有重连", fake.calls == 1, fake.calls)
    finally:
        db_writer.psycopg2 = old_psycopg2
        db_writer.time = old_time


TESTS = [
    test_connection_lazy_load_and_reuse,
    test_connection_reconnect_when_dead,
    test_close_closes_connection,
    test_write_network_state_success,
    test_write_network_state_failure,
    test_state_history_writes_on_first_call,
    test_state_history_skips_when_unchanged,
    test_state_history_writes_on_change,
    test_state_history_force_bypasses_throttle,
    test_state_history_writes_after_interval,
    test_state_history_failure,
    test_write_port_event,
    test_write_link_event,
    test_should_write_probe_first,
    test_should_write_probe_reachability_change,
    test_should_write_probe_high_loss,
    test_should_write_probe_high_latency,
    test_should_write_probe_normal_not_anomaly,
    test_write_probe_with_decision,
    test_write_probe_result_failure,
    test_should_write_consistency_first,
    test_should_write_consistency_unchanged,
    test_should_write_consistency_changed,
    test_should_write_consistency_heartbeat,
    test_should_write_consistency_diagnosing,
    test_write_consistency_check,
    test_write_diagnosis,
    test_write_healing_intent,
    test_write_audit,
    test_write_heartbeat,
    test_query_returns_dicts,
    test_query_failure_returns_empty,
    test_query_one,
    test_query_with_literal_percent,
    test_get_conn_retries_on_connection_death,
    test_get_conn_gives_up_after_max_attempts,
    test_get_conn_does_not_retry_on_query_error,
    test_real_db_network_state_upsert,
    test_real_db_state_history_is_idempotent_guarded,
]


if __name__ == "__main__":
    passed = 0
    for t in TESTS:
        try:
            t()
            passed += 1
        except AssertionError:
            pass
        except Exception as e:
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
            FAILURES.append(t.__name__)
    print()
    print(f"PASSED: {passed}/{len(TESTS)}")
    if FAILURES:
        print(f"FAILED: {FAILURES}")
    sys.exit(1 if FAILURES else 0)
