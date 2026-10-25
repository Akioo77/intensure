#!/usr/bin/env python3
"""graceful.py - 统一优雅退出（信号处理）

P0-3 优化项：之前 tmux kill-session 或 kill 时，
controller / live_env / assurance_mvp 会立即死亡，
正在写的 network_state / state_history 可能写一半，
重启后看到上次周期未完成的写入（演示现场 Ctrl+C 后状态错乱）。

设计：
1. 收尾逻辑全部包 try/except —— 收尾失败不能阻止退出
2. 收尾函数按注册顺序执行
3. 打印统一标记 GRACEFUL_SHUTDOWN，方便脚本/日志断言
4. 重复触发幂等（第二次收到信号直接强退）
"""
import os
import signal
import sys
import time

MARKER = "GRACEFUL_SHUTDOWN"
_installed = False
_in_progress = False


def _run_cleanups(cleanups):
    """执行收尾列表，逐个 try/except，绝不因一个失败而中断。"""
    for fn in cleanups:
        try:
            fn()
        except Exception as e:
            print(f"  [graceful] cleanup failed: {type(e).__name__}: {e}",
                  flush=True)


def install_graceful_shutdown(cleanups, name='process', exit_code=0):
    """注册 SIGTERM / SIGINT 处理器。

    参数:
        cleanups: 可调用对象列表，按顺序执行
        name: 进程名（仅用于日志）
        exit_code: 正常退出码
    """
    global _installed, _in_progress

    def _handler(signum, _frame):
        global _in_progress
        if _in_progress:
            print(f'[{MARKER}] second signal, force exit', flush=True)
            os._exit(130)
            return
        _in_progress = True
        signame = signal.Signals(signum).name
        print(f'[{MARKER}] {name} received {signame}, running '
              f'{len(cleanups)} cleanup(s)...', flush=True)
        t0 = time.time()
        _run_cleanups(cleanups)
        print(f'[{MARKER}] {name} done in {time.time() - t0:.2f}s, exiting',
              flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)

    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
        _installed = True
        print(f'[graceful] {name}: SIGTERM/SIGINT handler installed',
              flush=True)
    except ValueError as e:
        print(f'[graceful] {name}: cannot install signal handler: {e}',
              flush=True)
    return _handler


def is_installed():
    """是否已成功注册（测试用）。"""
    return _installed
