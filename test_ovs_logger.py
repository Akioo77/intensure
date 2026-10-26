#!/usr/bin/env python3
"""test_ovs_logger.py — ovs_logger.py 单元测试

覆盖：
  1. _parse_line —— OVS 日志行解析（时间戳/seq/module/level/msg）
  2. UTC 时间戳处理（不能偏 8 小时）
  3. get_logs 的 since / until / module / level / keyword / limit 六种过滤
  4. get_stats 返回结构
  5. correlate_with_link_events 关联结构

注意: 只测解析与查询，不调用 start()（那会真读 /var/log/openvswitch/）。

运行: python3 -m pytest test_ovs_logger.py -v
"""
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ovs_logger import OVSLogger

FAILURES = []


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        FAILURES.append(name)
        raise AssertionError(f"{name} {detail}")


def _logger():
    """造一个不启动的 logger（只测纯函数）。"""
    lg = OVSLogger(auto_verbosity=False)
    lg._running = False
    return lg


def _line(ts="2026-09-07T20:14:46.696Z", seq="00007",
         module="ofproto", level="INFO", msg="hello world"):
    return f"{ts}|{seq}|{module}|{level}|{msg}"


# ============ 1. _parse_line ============

def test_parse_line_valid():
    """标准 OVS 行能解析出 5 个字段。"""
    lg = _logger()
    e = lg._parse_line(_line())
    check("解析成功", e is not None, e)
    check("module 正确", e["module"] == "ofproto", e)
    check("level 正确", e["level"] == "INFO", e)
    check("msg 正确", e["msg"] == "hello world", e)
    check("seq 是 int", e["seq"] == 7, e["seq"])
    check("ts_str 保留原文", e["ts_str"] == "2026-09-07T20:14:46.696Z", e)
    check("ts 是 float", isinstance(e["ts"], float), type(e["ts"]))


def test_parse_line_empty():
    """空行 → None。"""
    lg = _logger()
    check("空行 → None", lg._parse_line("") is None)
    check("只有换行 → None", lg._parse_line("\n") is None)


def test_parse_line_malformed():
    """格式不对 → None（不抛异常）。"""
    lg = _logger()
    check("垃圾行 → None", lg._parse_line("this is not an ovs log line") is None)
    check("缺字段 → None", lg._parse_line("2026-09-07T20:14:46.696Z|00007") is None)


def test_parse_line_utc_timestamp_correct():
    """关键: OVS 时间戳是 UTC，不能按本地时区解析（会偏 8 小时）。"""
    lg = _logger()
    e = lg._parse_line(_line(ts="2026-09-07T20:14:46.696Z"))
    expected = datetime(2026, 9, 7, 20, 14, 46, 696000,
                         tzinfo=timezone.utc).timestamp()
    check("ts 按 UTC 解析", abs(e["ts"] - expected) < 0.001,
          f"got={e['ts']} expected={expected}")
    check("不是本地时区偏移", abs(e["ts"] - expected) < 1)


def test_parse_line_msg_with_pipes():
    """msg 里含 | 也能正确切分（前面 4 个字段是定长结构）。"""
    lg = _logger()
    e = lg._parse_line(_line(msg="a|b|c"))
    check("msg 保留全部管道符", e["msg"] == "a|b|c", e)


# ============ 2. get_logs 过滤 ============

def _seed_logs(lg, n=5):
    """往 buffer 里塞 n 条日志，时间戳递增。"""
    base = 1757276000.0
    for i in range(n):
        lg._buffer.append({
            "ts": base + i,
            "ts_str": "2026-09-07T20:14:46.696Z",
            "seq": i,
            "module": "ofproto" if i % 2 == 0 else "netdev",
            "level": "INFO" if i % 2 == 0 else "WARN",
            "msg": f"message number {i}",
        })
    return base


def test_get_logs_no_filter():
    """不过滤 → 返回全部（受 limit 约束）。"""
    lg = _logger()
    _seed_logs(lg, 5)
    got = lg.get_logs()
    check("返回 5 条", len(got) == 5, len(got))
    check("顺序保持", got[0]["seq"] == 0 and got[-1]["seq"] == 4, got)


def test_get_logs_since_until():
    """时间窗口过滤。"""
    lg = _logger()
    base = _seed_logs(lg, 5)
    got = lg.get_logs(since=base + 2, until=base + 3)
    check("窗口内 2 条", len(got) == 2, [g["seq"] for g in got])
    check("seq 正确", [g["seq"] for g in got] == [2, 3], got)


def test_get_logs_module_filter():
    """模块过滤。"""
    lg = _logger()
    _seed_logs(lg, 5)
    got = lg.get_logs(module="netdev")
    check("只返回 netdev", all(g["module"] == "netdev" for g in got), got)
    check("netdev 有 2 条", len(got) == 2, len(got))


def test_get_logs_level_filter():
    """级别过滤。"""
    lg = _logger()
    _seed_logs(lg, 5)
    got = lg.get_logs(level="WARN")
    check("只返回 WARN", all(g["level"] == "WARN" for g in got), got)
    check("WARN 有 2 条", len(got) == 2, len(got))


def test_get_logs_keyword_filter():
    """关键词过滤（大小写不敏感）。"""
    lg = _logger()
    _seed_logs(lg, 5)
    got = lg.get_logs(keyword="NUMBER 3")
    check("大小写不敏感命中 1 条", len(got) == 1, got)
    check("命中的是 number 3", got and got[0]["seq"] == 3, got)


def test_get_logs_limit_takes_latest():
    """limit 取最近的 N 条。"""
    lg = _logger()
    _seed_logs(lg, 5)
    got = lg.get_logs(limit=2)
    check("返回 2 条", len(got) == 2, len(got))
    check("是最近 2 条", [g["seq"] for g in got] == [3, 4], got)


# ============ 3. buffer 容量 ============

def test_buffer_is_bounded():
    """buffer 是定长 deque，超出丢最旧。"""
    lg = OVSLogger(buffer_size=10, auto_verbosity=False)
    for i in range(25):
        lg._buffer.append({"ts": float(i), "seq": i, "module": "m",
                           "level": "INFO", "msg": "x"})
    check("buffer 不超过容量 10", len(lg._buffer) == 10, len(lg._buffer))
    check("保留最近的", lg._buffer[-1]["seq"] == 24, lg._buffer[-1])


# ============ 4. get_stats ============

def test_get_stats_structure():
    """get_stats 返回固定字段集。"""
    lg = _logger()
    s = lg.get_stats()
    for k in ("running", "buffer_size", "buffer_capacity", "log_file",
              "pid", "started_at", "uptime_s"):
        check(f"含字段 {k}", k in s, list(s.keys()))
    check("running=False", s["running"] is False, s)
    check("buffer_capacity 正确",
          s["buffer_capacity"] == lg.buffer_size, s)
    check("pid 为 None（未启动）", s["pid"] is None, s)
    check("uptime_s 为 0", s["uptime_s"] == 0, s)


# ============ 5. correlate_with_link_events ============

def test_correlate_with_link_events():
    """关联函数返回 {link_event, ovs_logs, time_window_s} 结构。"""
    lg = _logger()
    base = _seed_logs(lg, 5)
    evs = [{"time": base + 2, "type": "port_link_status", "port": "s1-eth1"}]
    res = lg.correlate_with_link_events(evs, time_window=1.0)
    check("返回 1 条关联", len(res) == 1, res)
    r = res[0]
    check("含 link_event", r["link_event"]["port"] == "s1-eth1", r)
    check("含 ovs_logs 列表", isinstance(r["ovs_logs"], list), r)
    check("含 time_window_s", r["time_window_s"] == 1.0, r)
    check("窗口内命中日志", len(r["ovs_logs"]) >= 1, r["ovs_logs"])
    check("窗口外的不命中",
          all(abs(g["ts"] - (base + 2)) <= 1.0 for g in r["ovs_logs"]),
          r["ovs_logs"])


def test_correlate_empty_events():
    """没有 link event → 返回空列表。"""
    lg = _logger()
    check("空事件 → []", lg.correlate_with_link_events([]) == [])


TESTS = [
    test_parse_line_valid,
    test_parse_line_empty,
    test_parse_line_malformed,
    test_parse_line_utc_timestamp_correct,
    test_parse_line_msg_with_pipes,
    test_get_logs_no_filter,
    test_get_logs_since_until,
    test_get_logs_module_filter,
    test_get_logs_level_filter,
    test_get_logs_keyword_filter,
    test_get_logs_limit_takes_latest,
    test_buffer_is_bounded,
    test_get_stats_structure,
    test_correlate_with_link_events,
    test_correlate_empty_events,
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
