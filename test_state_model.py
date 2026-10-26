#!/usr/bin/env python3
"""test_state_model.py — state_model.py 单元测试

覆盖 5 个验证器 + 主入口：
  1. validate_schema       —— 必填字段缺失 / 类型不对
  2. validate_sanity       —— 负数计数器 / 非 int / port_no 缺失 / connected_at 非法
  3. validate_consistency  —— num_ports 不符 / 孤儿 dpid / host 挂在未知交换机
  4. validate_completeness —— 交换机/主机/链路/预期 IP 数量
  5. validate_anomaly      —— 端口 down / 计数器回退 / 主机消失 / 交换机掉线 / 数据陈旧
  6. validate_state        —— 汇总 ok / issue_count / by_level

运行: python3 -m pytest test_state_model.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import state_model as sm

FAILURES = []


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        FAILURES.append(name)
        raise AssertionError(f"{name} {detail}")


DPID1 = "0000000000000001"
DPID2 = "0000000000000002"
OFPP_LOCAL = 0xfffffffe


def good_state():
    """一个「完全正常」的状态：2 交换机 + 4 主机 + 1 互联链路。"""
    return {
        "meta": {"update_count": 10, "last_update": time.time(),
                 "start_time": time.time() - 100},
        "switches": {
            DPID1: {
                "dpid": DPID1, "connected_at": time.time(), "num_ports": 3,
                "ports": [
                    {"port_no": 1, "name": "s1-eth1", "state": 0},
                    {"port_no": 2, "name": "s1-eth2", "state": 0},
                    {"port_no": 3, "name": "s1-eth3", "state": 0},
                ],
            },
            DPID2: {
                "dpid": DPID2, "connected_at": time.time(), "num_ports": 3,
                "ports": [
                    {"port_no": 1, "name": "s2-eth1", "state": 0},
                    {"port_no": 2, "name": "s2-eth2", "state": 0},
                    {"port_no": 3, "name": "s2-eth3", "state": 0},
                ],
            },
        },
        "hosts": {
            "mac1": {"dpid": DPID1, "port": 1, "ipv4": ["10.0.0.1"]},
            "mac2": {"dpid": DPID2, "port": 1, "ipv4": ["10.0.0.2"]},
            "mac3": {"dpid": DPID1, "port": 2, "ipv4": ["10.0.0.3"]},
            "mac4": {"dpid": DPID2, "port": 2, "ipv4": ["10.0.0.4"]},
        },
        "port_stats": {
            DPID1: [{"port_no": 1, "rx_bytes": 100, "tx_bytes": 200,
                     "rx_packets": 10, "tx_packets": 20,
                     "rx_errors": 0, "tx_errors": 0,
                     "rx_dropped": 0, "tx_dropped": 0}],
        },
        "flows": {DPID1: [{"priority": 1, "match": {}, "actions": []}]},
        "events": [{"time": time.time(), "type": "switch_connected"}],
        "links": {
            f"{DPID1}:3": {"peer_dpid": DPID2, "peer_port": 3,
                          "last_seen": time.time()},
        },
    }


def _levels(issues):
    """issues 里按 level 归类。"""
    out = {}
    for i in issues:
        out.setdefault(i["level"], []).append(i)
    return out


def _cats(issues):
    """issues 里按 category 归类。"""
    out = {}
    for i in issues:
        out.setdefault(i["category"], []).append(i)
    return out


# ============ 1. validate_schema ============

def test_schema_good_state_has_no_error():
    """正常状态不该有 schema 错误。"""
    issues = sm.validate_schema(good_state())
    errs = [i for i in issues if i["level"] == "error"]
    check("正常状态无 schema 错误", errs == [], errs)
    check("issue 有 level/category/message/time",
          all(k in issues[0] for k in ("level", "category", "message", "time"))
          if issues else True)


def test_schema_missing_top_level_keys():
    """缺必填字段 → error + missing 列表。"""
    issues = sm.validate_schema({"meta": {}})
    cats = _cats(issues)
    check("有 schema 类问题", "schema" in cats, list(cats.keys()))
    check("level=error", all(i["level"] == "error" for i in cats["schema"]),
          cats["schema"])
    check("带 missing 字段列表", "missing" in cats["schema"][0], cats["schema"][0])


def test_schema_wrong_types():
    """switches 不是 dict → error。"""
    st = good_state()
    st["switches"] = []
    issues = sm.validate_schema(st)
    msgs = " ".join(i["message"] for i in issues)
    check("报 switches should be dict", "switches should be dict" in msgs, msgs)


def test_schema_not_a_dict():
    """state 不是 dict → 立即 error 返回。"""
    issues = sm.validate_schema("not a dict")
    check("非 dict → 1 条 error", len(issues) == 1, issues)
    check("level=error", issues[0]["level"] == "error", issues[0])


# ============ 2. validate_sanity ============

def test_sanity_negative_counter():
    """rx_bytes 为负 → error。"""
    st = good_state()
    st["port_stats"][DPID1][0]["rx_bytes"] = -1
    issues = sm.validate_sanity(st)
    errs = [i for i in issues if i["level"] == "error"]
    check("负数计数器被报出", len(errs) >= 1, issues)
    check("提到 negative", "negative" in errs[0]["message"], errs[0])


def test_sanity_non_int_counter():
    """rx_bytes 不是 int → error。"""
    st = good_state()
    st["port_stats"][DPID1][0]["rx_bytes"] = "abc"
    issues = sm.validate_sanity(st)
    errs = [i for i in issues if i["level"] == "error"]
    check("非 int 计数器被报出", len(errs) >= 1, issues)
    check("提到 not int", "not int" in errs[0]["message"], errs[0])


def test_sanity_missing_port_no():
    """port_no 缺失 → error。"""
    st = good_state()
    del st["port_stats"][DPID1][0]["port_no"]
    issues = sm.validate_sanity(st)
    msgs = " ".join(i["message"] for i in issues)
    check("port_no missing 被报出", "port_no missing" in msgs, msgs)


def test_sanity_bad_connected_at():
    """connected_at 非数字 → error。"""
    st = good_state()
    st["switches"][DPID1]["connected_at"] = "not-a-number"
    issues = sm.validate_sanity(st)
    errs = [i for i in issues if i["level"] == "error"]
    check("connected_at 非法被报出", len(errs) >= 1, issues)
    check("提到 connected_at", "connected_at" in errs[0]["message"], errs[0])


# ============ 3. validate_consistency ============

def test_consistency_num_ports_mismatch():
    """num_ports 与实际端口数不符 → warning。"""
    st = good_state()
    st["switches"][DPID1]["num_ports"] = 99
    issues = sm.validate_consistency(st)
    warns = [i for i in issues if i["level"] == "warning"]
    check("num_ports 不符被报出", len(warns) >= 1, issues)
    check("提到 num_ports", "num_ports" in warns[0]["message"], warns[0])


def test_consistency_orphan_dpid():
    """port_stats/flows 里的 dpid 不在 switches → warning。"""
    st = good_state()
    st["port_stats"]["0000000000000099"] = [{"port_no": 1, "rx_bytes": 0}]
    st["flows"]["0000000000000099"] = []
    issues = sm.validate_consistency(st)
    warns = [i for i in issues if i["level"] == "warning"]
    check("孤儿 dpid 被报出（2 处）", len(warns) >= 2, issues)
    check("提到 not in switches",
          any("not in switches" in w["message"] for w in warns), warns)


def test_consistency_host_on_unknown_switch():
    """host 挂在未知交换机 → warning。"""
    st = good_state()
    st["hosts"]["macX"] = {"dpid": "00000000000000ff", "port": 1,
                          "ipv4": ["10.0.0.9"]}
    issues = sm.validate_consistency(st)
    warns = [i for i in issues if i["level"] == "warning"]
    check("未知交换机被报出", len(warns) >= 1, issues)
    check("提到 unknown switch",
          any("unknown switch" in w["message"] for w in warns), warns)


def test_consistency_host_on_undiscovered_port_is_info():
    """host 在已知交换机但端口还没发现 → info（不是 warning）。"""
    st = good_state()
    st["hosts"]["macY"] = {"dpid": DPID1, "port": 99, "ipv4": ["10.0.0.8"]}
    issues = sm.validate_consistency(st)
    infos = [i for i in issues if i["level"] == "info"]
    check("端口未发现记为 info", len(infos) >= 1, issues)
    check("提到 not in discovered ports yet",
          any("not in discovered ports yet" in i["message"] for i in infos),
          infos)


# ============ 4. validate_completeness ============

def test_completeness_missing_switch():
    """交换机少于期望 → warning。"""
    st = good_state()
    del st["switches"][DPID2]
    issues = sm.validate_completeness(st)
    warns = [i for i in issues if i["level"] == "warning"]
    check("缺交换机被报出", len(warns) >= 1, issues)
    check("提到 expected 2 switches",
          any("expected 2 switches" in w["message"] for w in warns), warns)


def test_completeness_more_switches_is_info():
    """交换机多于期望 → info（不是 warning）。"""
    st = good_state()
    st["switches"]["0000000000000003"] = {
        "dpid": "0000000000000003", "connected_at": time.time(),
        "num_ports": 1, "ports": [{"port_no": 1, "name": "s3-eth1", "state": 0}]}
    issues = sm.validate_completeness(st)
    infos = [i for i in issues if i["level"] == "info"]
    check("多交换机记为 info", len(infos) >= 1, issues)
    check("提到 more switches than expected",
          any("more switches than expected" in i["message"] for i in infos),
          infos)


def test_completeness_missing_host_ip():
    """预期 IP 没发现 → info + missing_ips。"""
    st = good_state()
    st["hosts"]["mac4"]["ipv4"] = ["10.0.0.44"]
    issues = sm.validate_completeness(st)
    infos = [i for i in issues if i["level"] == "info"]
    check("缺 IP 被报出", len(infos) >= 1, issues)
    check("带 missing_ips 字段",
          any("missing_ips" in i for i in infos), infos)


def test_completeness_missing_link():
    """互联链路少于期望 → info。"""
    st = good_state()
    st["links"] = {}
    issues = sm.validate_completeness(st)
    infos = [i for i in issues if i["level"] == "info"]
    check("缺互联链路被报出", len(infos) >= 1, issues)
    check("提到 inter-switch link",
          any("inter-switch link" in i["message"] for i in infos), infos)


# ============ 5. validate_anomaly ============

def test_anomaly_port_down():
    """端口 LINK_DOWN → warning。"""
    st = good_state()
    st["switches"][DPID1]["ports"][0]["state"] = 1
    issues = sm.validate_anomaly(st)
    warns = [i for i in issues if i["level"] == "warning"]
    check("端口 down 被报出",
          any("port link down" in w["message"] for w in warns), warns)
    check("带 port_name",
          any(w.get("port_name") == "s1-eth1" for w in warns), warns)


def test_anomaly_skips_ofpp_local():
    """OFPP_LOCAL 永远 down，要跳过（否则一直误报）。"""
    st = good_state()
    st["switches"][DPID1]["ports"].append({
        "port_no": OFPP_LOCAL, "name": "local", "state": 1})
    issues = sm.validate_anomaly(st)
    warns = [i for i in issues if i["level"] == "warning"]
    check("OFPP_LOCAL 未被误报",
          not any(w.get("port_no") == OFPP_LOCAL for w in warns), warns)


def test_anomaly_counter_went_down():
    """相对基线计数器回退 → warning。"""
    st = good_state()
    st["port_stats"][DPID1][0]["rx_bytes"] = 50
    baseline = {
        "port_stats": {DPID1: [{"port_no": 1, "rx_bytes": 100}]},
        "hosts": {}, "switches": {},
    }
    issues = sm.validate_anomaly(st, baseline)
    warns = [i for i in issues if i["level"] == "warning"]
    check("计数器回退被报出",
          any("counter went down" in w["message"] for w in warns), warns)
    check("带 metric 字段", any(w.get("metric") == "rx_bytes" for w in warns),
          warns)


def test_anomaly_host_disappeared():
    """基线里有、现在没了的主机 → warning。"""
    st = good_state()
    baseline = {
        "port_stats": {},
        "hosts": {"macGONE": {"dpid": DPID1, "port": 1}},
        "switches": {},
    }
    issues = sm.validate_anomaly(st, baseline)
    warns = [i for i in issues if i["level"] == "warning"]
    check("主机消失被报出",
          any("hosts disappeared" in w["message"] for w in warns), warns)


def test_anomaly_switch_disconnected_is_error():
    """交换机掉线 → error（比 warning 严重）。"""
    st = good_state()
    baseline = {
        "port_stats": {}, "hosts": {},
        "switches": {"0000000000000099": {"dpid": "0000000000000099"}},
    }
    issues = sm.validate_anomaly(st, baseline)
    errs = [i for i in issues if i["level"] == "error"]
    check("交换机掉线记为 error", len(errs) >= 1, issues)
    check("提到 switches disconnected",
          any("switches disconnected" in e["message"] for e in errs), errs)


def test_anomaly_stale_state():
    """meta.last_update 太旧 → warning。"""
    st = good_state()
    st["meta"]["last_update"] = time.time() - 120
    issues = sm.validate_anomaly(st)
    warns = [i for i in issues if i["level"] == "warning"]
    check("数据陈旧被报出",
          any("state is stale" in w["message"] for w in warns), warns)


# ============ 6. validate_state（主入口）============

def test_validate_state_report_structure():
    """主入口返回固定结构。"""
    r = sm.validate_state(good_state())
    for k in ("ok", "issue_count", "by_level", "issues", "validated_at"):
        check(f"含字段 {k}", k in r, list(r.keys()))
    check("issue_count 与 issues 长度一致",
          r["issue_count"] == len(r["issues"]), r["issue_count"])
    check("by_level 含三种级别",
          set(r["by_level"].keys()) == {"error", "warning", "info"}, r["by_level"])
    check("by_level 计数与实际一致",
          r["by_level"]["error"] == len(_levels(r["issues"]).get("error", [])),
          r["by_level"])


def test_validate_state_ok_when_no_errors():
    """没有 error → ok=True。"""
    st = good_state()
    st["meta"]["last_update"] = time.time()
    r = sm.validate_state(st)
    check("ok=True", r["ok"] is True, r["by_level"])
    check("by_level error=0", r["by_level"]["error"] == 0, r["by_level"])


def test_validate_state_not_ok_when_error():
    """有 error → ok=False。"""
    st = good_state()
    st["port_stats"][DPID1][0]["rx_bytes"] = -5
    r = sm.validate_state(st)
    check("ok=False", r["ok"] is False, r["by_level"])
    check("by_level error>0", r["by_level"]["error"] > 0, r["by_level"])


def test_validate_state_connectivity_injected():
    """注入 connectivity 且有不可达 → error。"""
    r = sm.validate_state(good_state(),
                          connectivity={"reachable": ["10.0.0.1"],
                                        "unreachable": ["10.0.0.4"]})
    check("不可达记为 error", r["by_level"]["error"] >= 1, r["by_level"])
    check("ok=False", r["ok"] is False, r["by_level"])
    check("有 connectivity 类问题",
          "connectivity" in _cats(r["issues"]), list(_cats(r["issues"]).keys()))


TESTS = [
    test_schema_good_state_has_no_error,
    test_schema_missing_top_level_keys,
    test_schema_wrong_types,
    test_schema_not_a_dict,
    test_sanity_negative_counter,
    test_sanity_non_int_counter,
    test_sanity_missing_port_no,
    test_sanity_bad_connected_at,
    test_consistency_num_ports_mismatch,
    test_consistency_orphan_dpid,
    test_consistency_host_on_unknown_switch,
    test_consistency_host_on_undiscovered_port_is_info,
    test_completeness_missing_switch,
    test_completeness_more_switches_is_info,
    test_completeness_missing_host_ip,
    test_completeness_missing_link,
    test_anomaly_port_down,
    test_anomaly_skips_ofpp_local,
    test_anomaly_counter_went_down,
    test_anomaly_host_disappeared,
    test_anomaly_switch_disconnected_is_error,
    test_anomaly_stale_state,
    test_validate_state_report_structure,
    test_validate_state_ok_when_no_errors,
    test_validate_state_not_ok_when_error,
    test_validate_state_connectivity_injected,
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
