#!/usr/bin/env python3
"""test_graceful.py — graceful.py 单元测试

覆盖:
  1. install_graceful_shutdown 能注册 handler
  2. SIGTERM 触发时按顺序执行全部 cleanup
  3. 某个 cleanup 抛异常不影响后续 cleanup（不吞掉整段收尾）
  4. 重复信号幂等（第二次直接强退）
  5. is_installed() 状态正确

运行: python3 -m pytest test_graceful.py -v
"""
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import graceful

FAILURES = []


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if condition:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name} {detail}")
        FAILURES.append(name)
        raise AssertionError(f"{name} {detail}")


def test_install_registers_handler():
    """注册后 signal.getsignal(SIGTERM) 应该被换掉。"""
    original = signal.getsignal(signal.SIGTERM)
    try:
        graceful._installed = False
        handler = graceful.install_graceful_shutdown([], name="test-empty")
        check("注册后 SIGTERM handler 已替换",
              signal.getsignal(signal.SIGTERM) is handler)
        check("is_installed() 返回 True", graceful.is_installed())
    finally:
        signal.signal(signal.SIGTERM, original)
        graceful._installed = False


def test_cleanups_run_in_order():
    """收尾函数按注册顺序执行。"""
    calls = []
    graceful._installed = False
    graceful._in_progress = False
    original = signal.getsignal(signal.SIGTERM)
    try:
        graceful.install_graceful_shutdown(
            [lambda: calls.append("a"), lambda: calls.append("b"),
             lambda: calls.append("c")],
            name='test-order')
        handler = signal.getsignal(signal.SIGTERM)
        # 直接调 handler，不真发信号（handler 结尾会 os._exit）
        _patch_exit(graceful)
        try:
            handler(signal.SIGTERM, None)
        finally:
            _restore_exit(graceful)
    finally:
        signal.signal(signal.SIGTERM, original)
        graceful._installed = False
        graceful._in_progress = False
    check("3 个 cleanup 按序执行", calls == ["a", "b", "c"], f"实际={calls}")


def test_failing_cleanup_does_not_block_others():
    """一个 cleanup 抛异常, 后续的仍要执行。"""
    calls = []

    def boom():
        raise RuntimeError("故意炸")

    graceful._installed = False
    graceful._in_progress = False
    original = signal.getsignal(signal.SIGTERM)
    try:
        graceful.install_graceful_shutdown(
            [boom, lambda: calls.append("after-boom")],
            name='test-boom')
        handler = signal.getsignal(signal.SIGTERM)
        _patch_exit(graceful)
        try:
            handler(signal.SIGTERM, None)
        finally:
            _restore_exit(graceful)
    finally:
        signal.signal(signal.SIGTERM, original)
        graceful._installed = False
        graceful._in_progress = False
    check("异常 cleanup 之后的仍执行", calls == ["after-boom"], f"实际={calls}")


def test_second_signal_force_exit():
    """重复信号幂等: 第二次直接 os._exit(130)。"""
    graceful._installed = False
    graceful._in_progress = False
    original = signal.getsignal(signal.SIGTERM)
    exited = []
    try:
        graceful.install_graceful_shutdown([], name="test-double")
        handler = signal.getsignal(signal.SIGTERM)

        real_exit = os._exit
        os._exit = lambda code: exited.append(code)
        try:
            handler(signal.SIGTERM, None)   # 第一次: 正常收尾 + exit(0)
            handler(signal.SIGTERM, None)   # 第二次: 应直接 exit(130)
        finally:
            os._exit = real_exit
    finally:
        signal.signal(signal.SIGTERM, original)
        graceful._installed = False
        graceful._in_progress = False
    check("重复信号触发 130 强退", exited and exited[-1] == 130, f"exits={exited}")


def _patch_exit(mod):
    """把 os._exit 换成记录器，避免测试真的杀进程。"""
    mod._test_exits = []
    real = os._exit

    def fake(code):
        mod._test_exits.append(code)

    mod._real_exit = real
    os._exit = fake


def _restore_exit(mod):
    os._exit = getattr(mod, "_real_exit", os._exit)


def run_all():
    for fn in (test_install_registers_handler,
               test_cleanups_run_in_order,
               test_failing_cleanup_does_not_block_others,
               test_second_signal_force_exit):
        fn()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}/{4} — {FAILURES}")
        return 1
    print("PASSED: 4/4")
    return 0


if __name__ == '__main__':
    sys.exit(run_all())
