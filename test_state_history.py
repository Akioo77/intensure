#!/usr/bin/env python3
"""test_state_history.py — state_history.py 单元测试

覆盖：
  1. 环形缓冲：超过 MAX_ENTRIES 时丢弃最旧的
  2. get(key, limit) / get(None) 两种取法
  3. dump() 原子写（先写 .tmp 再 rename）+ load() 恢复
  4. 4 个 add_* 接口

运行: python3 -m pytest test_state_history.py -v
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import state_history

FAILURES = []


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        FAILURES.append(name)
        raise AssertionError(f"{name} {detail}")


def _reset():
    """清空所有 history 桶。"""
    for k in list(state_history.HISTORY.keys()):
        state_history.HISTORY[k] = []


# ============ 1. add_* 接口 ============

def test_add_summary():
    """add_summary 写进 summary 桶。"""
    _reset()
    state_history.add_summary({"switches": 2, "hosts": 4})
    check("summary 有 1 条", len(state_history.HISTORY["summary"]) == 1)
    check("内容正确", state_history.HISTORY["summary"][0]["switches"] == 2)
    _reset()


def test_add_port_stats_and_validation_and_connectivity():
    """另外 3 个 add_* 各写各的桶，不串。"""
    _reset()
    state_history.add_port_stats([{"port_no": 1}])
    state_history.add_validation({"ok": True})
    state_history.add_connectivity({"loss_pct": 0.0})
    check("port_stats 1 条", len(state_history.HISTORY["port_stats"]) == 1)
    check("validation 1 条", len(state_history.HISTORY["validation"]) == 1)
    check("connectivity 1 条", len(state_history.HISTORY["connectivity"]) == 1)
    check("summary 桶没被污染", len(state_history.HISTORY["summary"]) == 0)
    _reset()


# ============ 2. 环形缓冲 ============

def test_ring_buffer_caps_entries():
    """超过 MAX_ENTRIES 时只保留最近的。"""
    _reset()
    n = state_history.MAX_ENTRIES + 25
    for i in range(n):
        state_history.add_summary({"i": i})
    arr = state_history.HISTORY["summary"]
    check(f"长度封顶在 MAX_ENTRIES({state_history.MAX_ENTRIES})",
          len(arr) == state_history.MAX_ENTRIES, len(arr))
    check("保留的是最新的那条", arr[-1]["i"] == n - 1, arr[-1])
    check("丢弃的是最旧的", arr[0]["i"] == 25, arr[0])
    _reset()


# ============ 3. get ============

def test_get_with_key_and_limit():
    """get(key, limit) 返回最近 limit 条。"""
    _reset()
    for i in range(10):
        state_history.add_connectivity({"i": i})
    got = state_history.get("connectivity", 3)
    check("返回 3 条", len(got) == 3, len(got))
    check("是最近 3 条", [x["i"] for x in got] == [7, 8, 9], got)
    _reset()


def test_get_without_key_returns_all_buckets():
    """get() 不带 key → 返回所有桶的 dict。"""
    _reset()
    state_history.add_summary({"x": 1})
    state_history.add_connectivity({"y": 2})
    got = state_history.get()
    check("返回 dict", isinstance(got, dict), type(got))
    check("含 summary 桶", "summary" in got, list(got.keys()))
    check("含 connectivity 桶", "connectivity" in got, list(got.keys()))
    check("桶内容正确", got["connectivity"][0]["y"] == 2, got["connectivity"])
    _reset()


def test_get_unknown_key_returns_empty():
    """不存在的 key → 空列表（不报错）。"""
    got = state_history.get("no-such-key", 10)
    check("未知 key 返回 []", got == [], got)


def test_get_limit_zero_returns_all():
    """limit=0 → 返回全部（不截断）。"""
    _reset()
    for i in range(5):
        state_history.add_summary({"i": i})
    got = state_history.get("summary", 0)
    check("limit=0 返回全部 5 条", len(got) == 5, len(got))
    _reset()


# ============ 4. dump / load（原子持久化）============

def test_dump_and_load_roundtrip():
    """dump 写文件 → load 恢复，content 一致。"""
    _reset()
    state_history.add_summary({"switches": 2})
    state_history.add_connectivity({"loss_pct": 1.5})
    original_file = state_history.HISTORY_FILE
    tmpdir = tempfile.mkdtemp()
    tmpfile = os.path.join(tmpdir, "history.json")
    try:
        state_history.HISTORY_FILE = tmpfile
        state_history.dump()
        check("文件已生成", os.path.exists(tmpfile))
        check("没留 .tmp 残留", not os.path.exists(tmpfile + ".tmp"))
        # 清空内存后再 load
        _reset()
        check("内存已清空", len(state_history.HISTORY["summary"]) == 0)
        state_history.load()
        check("load 恢复了 summary",
              len(state_history.HISTORY["summary"]) == 1,
              state_history.HISTORY["summary"])
        check("load 恢复了 connectivity",
              len(state_history.HISTORY["connectivity"]) == 1)
        check("内容一致",
              state_history.HISTORY["connectivity"][0]["loss_pct"] == 1.5)
    finally:
        state_history.HISTORY_FILE = original_file
        _reset()


def test_dump_is_atomic_no_partial_file():
    """dump 先写 .tmp 再 rename —— 中途不留下半截正式文件。"""
    _reset()
    original_file = state_history.HISTORY_FILE
    tmpdir = tempfile.mkdtemp()
    tmpfile = os.path.join(tmpdir, "history.json")
    try:
        state_history.HISTORY_FILE = tmpfile
        state_history.add_summary({"a": 1})
        state_history.dump()
        # 正式文件必须是完整合法 JSON
        with open(tmpfile) as f:
            data = json.load(f)
        check("写出的是合法 JSON", isinstance(data, dict), type(data))
        check("含 summary 键", "summary" in data, list(data.keys()))
    finally:
        state_history.HISTORY_FILE = original_file
        _reset()


def test_load_missing_file_is_safe():
    """文件不存在时 load 不抛异常。"""
    original_file = state_history.HISTORY_FILE
    tmpdir = tempfile.mkdtemp()
    try:
        state_history.HISTORY_FILE = os.path.join(tmpdir, "nope.json")
        state_history.load()
        check("load 缺失文件不抛", True)
    finally:
        state_history.HISTORY_FILE = original_file
        _reset()


TESTS = [
    test_add_summary,
    test_add_port_stats_and_validation_and_connectivity,
    test_ring_buffer_caps_entries,
    test_get_with_key_and_limit,
    test_get_without_key_returns_all_buckets,
    test_get_unknown_key_returns_empty,
    test_get_limit_zero_returns_all,
    test_dump_and_load_roundtrip,
    test_dump_is_atomic_no_partial_file,
    test_load_missing_file_is_safe,
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
