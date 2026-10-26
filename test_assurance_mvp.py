#!/usr/bin/env python3
"""test_assurance_mvp.py — assurance_mvp.py 单元测试

覆盖：
  1. extract_actual_state_hosts  —— 按 IP / 按 MAC 索引、present 判定、/mask 剥离
  2. extract_actual_down_ports   —— LINK_DOWN 位判定、OFPP_LOCAL 跳过、设备名推断
  3. extract_actual_down_links   —— 单端 down 判定、双向去重
  4. check_one_policy            —— 状态机全部转换：pass / 4 种 violation_types
  5. diagnose_violation          —— 根因规则：port_failure / link_failure / unknown + confidence

运行: python3 -m pytest test_assurance_mvp.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assurance_mvp as m

FAILURES = []


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        FAILURES.append(name)
        raise AssertionError(f"{name} {detail}")


# ============ 构造测试数据 ============

DPID1 = "0000000000000001"
DPID2 = "0000000000000002"
OFPP_LOCAL = 4294967294
OFPPS_LINK_DOWN = 1


def policy(src="10.0.0.1/32", dst="10.0.0.4/32", intent_id="live-AC-001"):
    return {
        "intent_id": intent_id,
        "intent_type": "ACCESS_CONTROL",
        "policy_spec": {
            "source": {"cidr": src},
            "destination": {"cidr": dst},
        },
    }


def state(hosts=None, ports=None, links=None):
    """构造 network_state.payload。"""
    return {
        "hosts": hosts if hosts is not None else {},
        "switches": {
            DPID1: {"ports": ports if ports is not None else []},
        },
        "links": links if links is not None else {},
    }


def host_entry(ip=None, mac="aa:bb:cc:dd:ee:01", port=1, dpid=DPID1):
    h = {"dpid": dpid, "port": port}
    if ip is not None:
        h["ipv4"] = [ip]
    return mac, h


# ============ 1. extract_actual_state_hosts ============

def test_extract_hosts_empty():
    """空 payload → 空 dict。"""
    out = m.extract_actual_state_hosts({})
    check("空 payload → {}", out == {}, out)


def test_extract_hosts_by_ip():
    """有 ipv4 的主机按 IP 索引。"""
    mac, h = host_entry(ip="10.0.0.1/32")
    out = m.extract_actual_state_hosts({"hosts": {mac: h}})
    check("按 IP 建索引", "10.0.0.1" in out, list(out.keys()))
    check("/mask 被剥离", "10.0.0.1/32" not in out)
    check("present=True（port 非 None）", out["10.0.0.1"]["present"] is True)
    check("mac 也在索引里", f"mac:{mac}" in out)


def test_extract_hosts_without_ip():
    """没学到 IP 的主机只按 MAC 索引，present 由 port 决定。"""
    mac, h = host_entry(ip=None, port=None)
    out = m.extract_actual_state_hosts({"hosts": {mac: h}})
    check("只按 MAC 索引", list(out.keys()) == [f"mac:{mac}"], list(out.keys()))
    check("port=None → present=False", out[f"mac:{mac}"]["present"] is False)
    check("ipv4_list 为空", out[f"mac:{mac}"]["ipv4_list"] == [])


def test_extract_hosts_ips_alias():
    """同一主机多个 IP 都建索引。"""
    mac = "aa:bb:cc:dd:ee:02"
    h = {"dpid": DPID1, "port": 2, "ipv4": ["10.0.0.2/32", "10.1.0.2/32"]}
    out = m.extract_actual_state_hosts({"hosts": {mac: h}})
    check("两个 IP 都建索引", "10.0.0.2" in out and "10.1.0.2" in out, list(out.keys()))
    check("MAC 索引也在", f"mac:{mac}" in out)


# ============ 2. extract_actual_down_ports ============

def test_extract_down_ports_empty():
    """无端口 → 空列表。"""
    out = m.extract_actual_down_ports(state())
    check("无端口 → []", out == [], out)


def test_extract_down_ports_link_down_bit():
    """state & 1 = LINK_DOWN 的端口要挑出来。"""
    ports = [
        {"port_no": 1, "state": OFPPS_LINK_DOWN, "name": "s1-eth1"},
        {"port_no": 2, "state": 0, "name": "s1-eth2"},
    ]
    out = m.extract_actual_down_ports(state(ports=ports))
    check("只挑出 down 的", out == ["s1:eth1"], out)


def test_extract_down_ports_skips_local():
    """OFPP_LOCAL(4294967294) 要跳过。"""
    ports = [{"port_no": OFPP_LOCAL, "state": OFPPS_LINK_DOWN, "name": "local"}]
    out = m.extract_actual_down_ports(state(ports=ports))
    check("OFPP_LOCAL 被跳过", out == [], out)


def test_extract_down_ports_device_from_dpid():
    """设备名从 dpid 十六进制推断：0000000000000001 → s1。"""
    ports = [{"port_no": 3, "state": OFPPS_LINK_DOWN, "name": "s1-eth3"}]
    out = m.extract_actual_down_ports(state(ports=ports))
    check("设备名 s1 推断正确", out == ["s1:eth3"], out)


# ============ 3. extract_actual_down_links ============

def test_extract_down_links_empty():
    """无 links → 空列表。"""
    out = m.extract_actual_down_links(state())
    check("无 links → []", out == [], out)


def test_extract_down_links_one_end_down():
    """链路一端 down → 整条算 down。"""
    ports = [{"port_no": 3, "state": OFPPS_LINK_DOWN, "name": "s1-eth3"}]
    links = {
        "k1": {"src_dpid": DPID1, "src_port": 3,
               "dst_dpid": DPID2, "dst_port": 3},
    }
    out = m.extract_actual_down_links(state(ports=ports, links=links))
    check("一端 down → 链路 down", len(out) == 1, out)
    check("链路格式 s1:3-s2:3", out and out[0] == "s1:3-s2:3", out)


def test_extract_down_links_both_up():
    """两端都 up → 不算 down。"""
    ports = [{"port_no": 3, "state": 0, "name": "s1-eth3"}]
    links = {
        "k1": {"src_dpid": DPID1, "src_port": 3,
               "dst_dpid": DPID2, "dst_port": 3},
    }
    out = m.extract_actual_down_links(state(ports=ports, links=links))
    check("两端都 up → []", out == [], out)


def test_extract_down_links_dedup():
    """双向描述同一条链路 → 只算一次。"""
    ports = [{"port_no": 3, "state": OFPPS_LINK_DOWN, "name": "s1-eth3"}]
    links = {
        "k1": {"src_dpid": DPID1, "src_port": 3,
               "dst_dpid": DPID2, "dst_port": 3},
        "k2": {"src_dpid": DPID2, "src_port": 3,
               "dst_dpid": DPID1, "dst_port": 3},
    }
    out = m.extract_actual_down_links(state(ports=ports, links=links))
    check("双向去重 → 只 1 条", len(out) == 1, out)


# ============ 4. check_one_policy（状态机）============

def test_check_policy_pass():
    """一切正常 → result=pass，无 violation。"""
    m1, h1 = host_entry(ip="10.0.0.1/32")
    m4, h4 = host_entry(ip="10.0.0.4/32", mac="aa:bb:cc:dd:ee:04", port=4)
    st = state(hosts={m1: h1, m4: h4})
    r = m.check_one_policy(policy(), st)
    check("正常 → pass", r["result"] == "pass", r)
    check("无 violation_types", r["violation_types"] == [], r["violation_types"])
    check("evidence 为空", r["evidence"] == {}, r["evidence"])
    check("expected 带 src/dst", r["expected"]["src"] == "10.0.0.1", r["expected"])


def test_check_policy_skip_when_no_ip_learned():
    """关键边界：ARP 未触发（所有 host 都没 ipv4）→ 跳过 host 检查，不算违例。"""
    m1, h1 = host_entry(ip=None)
    st = state(hosts={m1: h1})
    r = m.check_one_policy(policy(), st)
    check("没学到 IP → 不判 SOURCE_NOT_FOUND",
          "SOURCE_NOT_FOUND" not in r["violation_types"], r["violation_types"])
    check("没学到 IP → 不判 DEST_NOT_FOUND",
          "DEST_NOT_FOUND" not in r["violation_types"], r["violation_types"])
    check("没学到 IP → pass", r["result"] == "pass", r)


def test_check_policy_source_not_found():
    """采集已稳定但 src 不在 → SOURCE_NOT_FOUND。"""
    m4, h4 = host_entry(ip="10.0.0.4/32", mac="aa:bb:cc:dd:ee:04", port=4)
    st = state(hosts={m4: h4})
    r = m.check_one_policy(policy(), st)
    check("src 缺失 → violated", r["result"] == "violated", r)
    check("含 SOURCE_NOT_FOUND",
          "SOURCE_NOT_FOUND" in r["violation_types"], r["violation_types"])
    check("evidence 记录 missing_source",
          r["evidence"].get("missing_source") == "10.0.0.1", r["evidence"])


def test_check_policy_dest_not_found():
    """采集已稳定但 dst 不在 → DEST_NOT_FOUND。"""
    m1, h1 = host_entry(ip="10.0.0.1/32")
    st = state(hosts={m1: h1})
    r = m.check_one_policy(policy(), st)
    check("dst 缺失 → violated", r["result"] == "violated", r)
    check("含 DEST_NOT_FOUND",
          "DEST_NOT_FOUND" in r["violation_types"], r["violation_types"])
    check("evidence 记录 missing_destination",
          r["evidence"].get("missing_destination") == "10.0.0.4", r["evidence"])


def test_check_policy_port_down():
    """端口 down → PORT_DOWN。"""
    m1, h1 = host_entry(ip="10.0.0.1/32")
    m4, h4 = host_entry(ip="10.0.0.4/32", mac="aa:bb:cc:dd:ee:04", port=4)
    ports = [{"port_no": 1, "state": OFPPS_LINK_DOWN, "name": "s1-eth1"}]
    st = state(hosts={m1: h1, m4: h4}, ports=ports)
    r = m.check_one_policy(policy(), st)
    check("端口 down → violated", r["result"] == "violated", r)
    check("含 PORT_DOWN", "PORT_DOWN" in r["violation_types"], r["violation_types"])
    check("evidence 带 down_ports",
          r["evidence"].get("down_ports") == ["s1:eth1"], r["evidence"])


def test_check_policy_link_down():
    """链路 down → LINK_DOWN。"""
    m1, h1 = host_entry(ip="10.0.0.1/32")
    m4, h4 = host_entry(ip="10.0.0.4/32", mac="aa:bb:cc:dd:ee:04", port=4)
    ports = [{"port_no": 3, "state": OFPPS_LINK_DOWN, "name": "s1-eth3"}]
    links = {"k1": {"src_dpid": DPID1, "src_port": 3,
                     "dst_dpid": DPID2, "dst_port": 3}}
    st = state(hosts={m1: h1, m4: h4}, ports=ports, links=links)
    r = m.check_one_policy(policy(), st)
    check("链路 down → violated", r["result"] == "violated", r)
    check("含 LINK_DOWN", "LINK_DOWN" in r["violation_types"], r["violation_types"])
    check("evidence 带 down_links",
          bool(r["evidence"].get("down_links")), r["evidence"])


def test_check_policy_source_unreachable():
    """src 在表里但 present=False → SOURCE_HOST_UNREACHABLE。"""
    m1, h1 = host_entry(ip="10.0.0.1/32", port=None)
    m4, h4 = host_entry(ip="10.0.0.4/32", mac="aa:bb:cc:dd:ee:04", port=4)
    st = state(hosts={m1: h1, m4: h4})
    r = m.check_one_policy(policy(), st)
    check("src 不可达 → violated", r["result"] == "violated", r)
    check("含 SOURCE_HOST_UNREACHABLE",
          "SOURCE_HOST_UNREACHABLE" in r["violation_types"], r["violation_types"])


def test_check_policy_multiple_violations():
    """多种违例同时命中 → 全部列出。"""
    m4, h4 = host_entry(ip="10.0.0.4/32", mac="aa:bb:cc:dd:ee:04", port=4)
    ports = [{"port_no": 1, "state": OFPPS_LINK_DOWN, "name": "s1-eth1"}]
    st = state(hosts={m4: h4}, ports=ports)
    r = m.check_one_policy(policy(), st)
    check("多违例都列出",
          "SOURCE_NOT_FOUND" in r["violation_types"]
          and "PORT_DOWN" in r["violation_types"], r["violation_types"])
    check("actual 带 down_ports/down_links",
          "down_ports" in r["actual"] and "down_links" in r["actual"], r["actual"])


# ============ 5. diagnose_violation ============

def test_diagnose_port_failure():
    """down_ports → port_failure, confidence 0.85。"""
    cr = {"evidence": {"down_ports": ["s1:eth1"]}}
    d = m.diagnose_violation("intent-1", cr)
    check("root_causes 有 1 条", len(d["root_causes"]) == 1, d["root_causes"])
    check("类型 port_failure",
          d["root_causes"][0]["type"] == "port_failure", d["root_causes"][0])
    check("confidence=0.85", d["confidence"] == 0.85, d["confidence"])
    check("affected_devices=[s1]", d["affected_devices"] == ["s1"], d["affected_devices"])
    check("有 candidate_reasons",
          bool(d["root_causes"][0]["candidate_reasons"]))
    check("intent_id 透传", d["intent_id"] == "intent-1")


def test_diagnose_link_failure():
    """down_links → link_failure, confidence 0.8。"""
    cr = {"evidence": {"down_links": ["s1:eth3-s2:eth3"]}}
    d = m.diagnose_violation("intent-1", cr)
    check("类型 link_failure",
          d["root_causes"][0]["type"] == "link_failure", d["root_causes"][0])
    check("confidence=0.8", d["confidence"] == 0.8, d["confidence"])
    check("affected_devices 含 s1 和 s2",
          d["affected_devices"] == ["s1", "s2"], d["affected_devices"])


def test_diagnose_both():
    """两种证据都有 → 两条 root_causes，confidence 取 max。"""
    cr = {"evidence": {"down_ports": ["s1:eth1"],
                      "down_links": ["s1:eth3-s2:eth3"]}}
    d = m.diagnose_violation("intent-1", cr)
    check("2 条 root_causes", len(d["root_causes"]) == 2, d["root_causes"])
    types = sorted(rc["type"] for rc in d["root_causes"])
    check("类型齐全", types == ["link_failure", "port_failure"], types)
    check("confidence 取 max=0.85", d["confidence"] == 0.85, d["confidence"])


def test_diagnose_unknown():
    """无证据 → unknown, confidence 0.4。"""
    d = m.diagnose_violation("intent-1", {"evidence": {}})
    check("类型 unknown", d["root_causes"][0]["type"] == "unknown", d["root_causes"])
    check("confidence=0.4", d["confidence"] == 0.4, d["confidence"])
    check("reason=insufficient_evidence",
          d["root_causes"][0]["candidate_reasons"] == ["insufficient_evidence"])
    check("affected_devices 为空", d["affected_devices"] == [], d["affected_devices"])


TESTS = [
    test_extract_hosts_empty,
    test_extract_hosts_by_ip,
    test_extract_hosts_without_ip,
    test_extract_hosts_ips_alias,
    test_extract_down_ports_empty,
    test_extract_down_ports_link_down_bit,
    test_extract_down_ports_skips_local,
    test_extract_down_ports_device_from_dpid,
    test_extract_down_links_empty,
    test_extract_down_links_one_end_down,
    test_extract_down_links_both_up,
    test_extract_down_links_dedup,
    test_check_policy_pass,
    test_check_policy_skip_when_no_ip_learned,
    test_check_policy_source_not_found,
    test_check_policy_dest_not_found,
    test_check_policy_port_down,
    test_check_policy_link_down,
    test_check_policy_source_unreachable,
    test_check_policy_multiple_violations,
    test_diagnose_port_failure,
    test_diagnose_link_failure,
    test_diagnose_both,
    test_diagnose_unknown,
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
