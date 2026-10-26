#!/usr/bin/env python3
"""test_state_server.py — state_server.py REST 端点单元测试

用 Flask test_client（不起真服务）覆盖 12 个端点：
  1. /api/health      —— 状态 + source（memory/file）
  2. /api/state       —— 完整状态透传
  3. /api/switches /hosts /port_stats /flows —— 子集提取 + 缺失时返回 {}
  4. /api/links       —— 双向去重成对 + 端口名解析
  5. /api/events      —— since / limit 参数过滤
  6. /api/validate    —— 调 state_model.validate_state
  7. /api/history     —— 读 state_history
  8. /api/probe       —— POST 连通性上报

运行: python3 -m pytest test_state_server.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import flask  # noqa: F401
except ImportError:
    flask = None

if flask is not None:
    import state_server
    import state_history
else:
    state_server = None
    state_history = None

FAILURES = []
SKIP_REASON = ""


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        FAILURES.append(name)
        raise AssertionError(f"{name} {detail}")


def _skip_if_no_flask():
    if flask is None:
        global SKIP_REASON
        SKIP_REASON = "flask 未安装（该测试需在 VM 内跑）"
        print(f"[SKIP] {SKIP_REASON}")
        return True
    return False


def _client():
    """Flask test_client + 注入假状态。"""
    state_server.CONTROLLER_STATE = {
        "meta": {"update_count": 7, "start_time": 1000.0},
        "switches": {
            "0000000000000001": {
                "dpid": "0000000000000001",
                "ports": [
                    {"port_no": 1, "name": "s1-eth1", "state": 0},
                    {"port_no": 3, "name": "s1-eth3", "state": 0},
                ],
            },
            "0000000000000002": {
                "dpid": "0000000000000002",
                "ports": [{"port_no": 3, "name": "s2-eth3", "state": 0}],
            },
        },
        "hosts": {
            "aa:bb:cc:dd:ee:01": {"ipv4": ["10.0.0.1"], "port": 1,
                                 "dpid": "0000000000000001"},
        },
        "port_stats": {"0000000000000001": [{"port_no": 1, "rx_bytes": 100}]},
        "flows": {"0000000000000001": [{"priority": 1, "match": {}}]},
        "links": {
            "0000000000000001:3": {
                "peer_dpid": "0000000000000002", "peer_port": 3,
                "last_seen": 1234.5,
            },
            "0000000000000002:3": {
                "peer_dpid": "0000000000000001", "peer_port": 3,
                "last_seen": 1234.5,
            },
        },
        "events": [
            {"time": 100.0, "type": "switch_connected"},
            {"time": 200.0, "type": "port_down"},
            {"time": 300.0, "type": "link_up"},
        ],
    }
    state_server.app.config["TESTING"] = True
    return state_server.app.test_client()


# ============ 1. /api/health ============

def test_health():
    """/api/health 返回 ok + source=memory。"""
    if _skip_if_no_flask():
        return
    c = _client()
    r = c.get("/api/health")
    check("状态码 200", r.status_code == 200, r.status_code)
    d = r.get_json()
    check("status=ok", d["status"] == "ok", d)
    check("source=memory（注入内存状态时）", d["source"] == "memory", d)
    check("带 time 字段", "time" in d, d)


# ============ 2. /api/state ============

def test_state_endpoint():
    """/api/state 透传完整状态。"""
    if _skip_if_no_flask():
        return
    c = _client()
    r = c.get("/api/state")
    check("状态码 200", r.status_code == 200, r.status_code)
    d = r.get_json()
    check("含 meta", "meta" in d, list(d.keys()))
    check("含 switches/hosts/flows",
          all(k in d for k in ("switches", "hosts", "flows")), list(d.keys()))
    check("update_count 正确", d["meta"]["update_count"] == 7, d["meta"])


# ============ 3. 子集端点 ============

def test_subset_endpoints():
    """switches/hosts/port_stats/flows 各自提取对应子集。"""
    if _skip_if_no_flask():
        return
    c = _client()
    sw = c.get("/api/switches").get_json()
    check("/api/switches 返回 2 台交换机", len(sw) == 2, list(sw.keys()))
    ho = c.get("/api/hosts").get_json()
    check("/api/hosts 返回 1 个主机", len(ho) == 1, list(ho.keys()))
    ps = c.get("/api/port_stats").get_json()
    check("/api/port_stats 非空", len(ps) >= 1, ps)
    fl = c.get("/api/flows").get_json()
    check("/api/flows 非空", len(fl) >= 1, fl)


def test_subset_endpoints_empty_when_missing():
    """状态里没这些 key → 返回 {} 而不是 500。"""
    if _skip_if_no_flask():
        return
    state_server.CONTROLLER_STATE = {"meta": {}}
    c = state_server.app.test_client()
    for path in ("/api/switches", "/api/hosts", "/api/port_stats", "/api/flows"):
        r = c.get(path)
        check(f"{path} 状态码 200", r.status_code == 200, r.status_code)
        check(f"{path} 返回 {{}}", r.get_json() == {}, r.get_json())


# ============ 4. /api/links ============

def test_links_dedup_pairs():
    """双向描述的同一条链路 → 只返回一对。"""
    if _skip_if_no_flask():
        return
    c = _client()
    r = c.get("/api/links")
    check("状态码 200", r.status_code == 200, r.status_code)
    pairs = r.get_json()
    check("去重后只剩 1 对", len(pairs) == 1, pairs)
    p = pairs[0]
    check("含 a/b 两侧", "a" in p and "b" in p, p)
    check("端口名解析正确", p["a"]["name"] == "s1-eth3", p["a"])
    check("对端端口名正确", p["b"]["name"] == "s2-eth3", p["b"])
    check("带 last_seen", p["last_seen"] == 1234.5, p)


# ============ 5. /api/events ============

def test_events_since_and_limit():
    """/api/events 支持 since 与 limit 过滤。"""
    if _skip_if_no_flask():
        return
    c = _client()
    all_ev = c.get("/api/events").get_json()
    check("默认返回全部 3 条", len(all_ev) == 3, all_ev)
    since = c.get("/api/events?since=150").get_json()
    check("since=150 过滤掉旧事件", len(since) == 2, since)
    check("since 保留 time>150 的", all(e["time"] > 150 for e in since), since)
    lim = c.get("/api/events?limit=1").get_json()
    check("limit=1 只返回 1 条", len(lim) == 1, lim)
    check("limit 取的是最近的", lim[0]["time"] == 300.0, lim)


# ============ 6. /api/validate ============

def test_validate_endpoint():
    """/api/validate 调 state_model.validate_state 并返回报告。"""
    if _skip_if_no_flask():
        return
    c = _client()
    r = c.get("/api/validate")
    check("状态码 200", r.status_code == 200, r.status_code)
    d = r.get_json()
    check("返回 dict", isinstance(d, dict), type(d))
    check("含 issues 列表", "issues" in d, list(d.keys()))
    check("issues 是 list", isinstance(d["issues"], list), d["issues"])


# ============ 7. /api/history ============

def test_history_endpoint():
    """/api/history 读 state_history。"""
    if _skip_if_no_flask():
        return
    state_history.HISTORY["connectivity"] = []
    for i in range(5):
        state_history.add_connectivity({"time": float(i), "loss_pct": 0.0})
    c = _client()
    r = c.get("/api/history?key=connectivity&limit=2")
    check("状态码 200", r.status_code == 200, r.status_code)
    arr = r.get_json()
    check("limit=2 返回 2 条", len(arr) == 2, arr)
    check("取的是最近 2 条", arr[-1]["time"] == 4.0, arr)
    allk = c.get("/api/history").get_json()
    check("不带 key 返回全部 key 的 dict", isinstance(allk, dict), type(allk))
    check("含 connectivity 键", "connectivity" in allk, list(allk.keys()))
    state_history.HISTORY["connectivity"] = []


# ============ 8. POST /api/probe ============

def test_probe_post_records():
    """POST /api/probe 记录连通性并返回 ok。"""
    if _skip_if_no_flask():
        return
    state_history.HISTORY["connectivity"] = []
    c = _client()
    r = c.post("/api/probe", json={"reachable": ["10.0.0.4"],
                                  "unreachable": [],
                                  "loss_pct": 0.0,
                                  "latency_ms": 12})
    check("状态码 200", r.status_code == 200, r.status_code)
    d = r.get_json()
    check("status=ok", d["status"] == "ok", d)
    check("recorded=True", d["recorded"] is True, d)
    arr = state_history.get("connectivity")
    check("历史里记了 1 条", len(arr) == 1, arr)
    check("reachable 正确入库", arr[0]["reachable"] == ["10.0.0.4"], arr)
    check("latency 正确入库", arr[0]["latency_ms"] == 12, arr)
    state_history.HISTORY["connectivity"] = []


def test_probe_post_empty_body():
    """空 body 也不该 500。"""
    if _skip_if_no_flask():
        return
    state_history.HISTORY["connectivity"] = []
    c = _client()
    r = c.post("/api/probe", json={})
    check("空 body 状态码 200", r.status_code == 200, r.status_code)
    check("返回 ok", r.get_json()["status"] == "ok", r.get_json())
    state_history.HISTORY["connectivity"] = []


# ============ 9. 根路由 ============

def test_index_route():
    """根路由返回 dashboard（存在则 200，不存在则 404，都不能 500）。"""
    if _skip_if_no_flask():
        return
    c = _client()
    r = c.get("/")
    check("根路由不是 500", r.status_code != 500, r.status_code)
    check("根路由 200 或 404", r.status_code in (200, 404), r.status_code)


TESTS = [
    test_health,
    test_state_endpoint,
    test_subset_endpoints,
    test_subset_endpoints_empty_when_missing,
    test_links_dedup_pairs,
    test_events_since_and_limit,
    test_validate_endpoint,
    test_history_endpoint,
    test_probe_post_records,
    test_probe_post_empty_body,
    test_index_route,
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
