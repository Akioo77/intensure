"""状态历史: 环形缓冲 + 原子持久化.

保存"部分历史状态"(任务清单要求):
  - summary       网络汇总快照 (交换机/主机/流表数/全网 RX/TX/down 端口)
  - port_stats    各数据端口计数器历史
  - validation    验证报告摘要历史 (ok/errors/warnings)
  - connectivity  连通性探测历史 (pingall 结果)

设计:
  - 环形缓冲, 默认最多 200 条 (5s 周期 ≈ 16 分钟)
  - 内存共享 (run_all 单进程: 控制器与 Flask 同一进程直接 import)
  - 原子写 /tmp/network_history.json, 供独立进程/重启后恢复
"""
import json
import os
import time
import threading

HISTORY_FILE = '/tmp/network_history.json'
MAX_ENTRIES = 200

HISTORY = {
    'summary': [],        # {time, switches, hosts, flows, rx_bytes, tx_bytes, ports_down}
    'port_stats': [],     # [{time, dpid, port_no, name, rx_bytes, tx_bytes, rx_dropped, tx_dropped}]
    'validation': [],     # {time, ok, errors, warnings, infos, issues[]}
    'connectivity': [],   # {time, reachable, unreachable, loss_pct}
}

_lock = threading.Lock()


def _append(key, entry):
    with _lock:
        HISTORY[key].append(entry)
        if len(HISTORY[key]) > MAX_ENTRIES:
            HISTORY[key] = HISTORY[key][-MAX_ENTRIES:]


def add_summary(s):      _append('summary', s)
def add_port_stats(s):   _append('port_stats', s)
def add_validation(v):   _append('validation', v)
def add_connectivity(c): _append('connectivity', c)


def get(key=None, limit=100):
    """取历史. key 为空返回全部 key 的最近 limit 条."""
    if key:
        arr = HISTORY.get(key, [])
        return arr[-limit:] if limit else arr
    return {k: (v[-limit:] if limit else v) for k, v in HISTORY.items()}


def dump():
    """原子写文件 (先写 .tmp 再 rename)."""
    with _lock:
        snap = {k: list(v) for k, v in HISTORY.items()}
    try:
        tmp = HISTORY_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(snap, f, default=str)
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass


def load():
    """从文件恢复 (供独立运行的 state_server 使用)."""
    try:
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
        with _lock:
            for k, v in data.items():
                if k in HISTORY:
                    HISTORY[k] = v
    except Exception:
        pass
