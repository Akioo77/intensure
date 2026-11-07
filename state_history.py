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
import logging
import os
import threading
from typing import Dict

logger = logging.getLogger(__name__)

HISTORY_FILE = '/tmp/network_history.json'
MAX_ENTRIES = 200

HISTORY = {
    'summary': [],        # {time, switches, hosts, flows, rx_bytes, tx_bytes, ports_down}
    # [{time, dpid, port_no, name, rx_bytes, tx_bytes, rx_dropped, tx_dropped}]
    'port_stats': [],
    'validation': [],     # {time, ok, errors, warnings, infos, issues[]}
    'connectivity': [],   # {time, reachable, unreachable, loss_pct}
}

_lock = threading.Lock()

# 上次 load() 时文件的 mtime（用来判断要不要重读）
_loaded_mtime = None


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
    """原子写文件 (先写 .tmp 再 rename).

    ⚠️ **tmp 文件名必须带 pid** —— 之前固定用 `HISTORY_FILE + '.tmp'`，
    而控制器进程和 API 进程**是两个进程**、共用同一个 HISTORY_FILE：
      · 两个进程同时写同一个 .tmp → A 写一半、B 截断 → A 把半截 JSON 换上去
        → 下次 load() 解析失败，而 `except: pass` 静默吞掉 → 历史全丢且无日志
      · API 进程写的是它自己那份**陈旧**内存快照 → 把控制器刚写的整段覆盖掉
    带 pid 后每个进程用各自的 tmp，互不干扰。
    """
    with _lock:
        snap = {k: list(v) for k, v in HISTORY.items()}
    tmp = f'{HISTORY_FILE}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(snap, f, default=str)
        os.replace(tmp, HISTORY_FILE)
    except Exception as e:
        logger.warning(f'历史落盘失败: {e}')
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load():
    """从文件恢复 (供独立运行的 state_server 使用).

    ⚠️ **合并而不是整体替换**：API 进程自己也往 HISTORY 里写（比如收到
    POST /api/probe 后追加一条 connectivity），如果整体替换，文件里
    还没有的那条就会被冲掉 → 丢数据。
    合并按 'time' 去重（同一时刻以**文件里的**为准 —— 文件是权威副本）。
    """
    global _loaded_mtime
    try:
        with open(HISTORY_FILE, 'r') as f:
            raw = f.read()
    except FileNotFoundError:
        _loaded_mtime = None
        return False
    except Exception as e:
        logger.warning(f'历史文件读不了（{HISTORY_FILE}）: {e}')
        return False
    if not raw.strip():
        _loaded_mtime = _file_mtime()
        return False
    try:
        data = json.loads(raw)
    except Exception as e:
        # ⚠️ 不要静默吞掉 —— 之前这里 `except Exception: pass`，
        #    文件被写坏时历史直接消失且没有任何日志
        logger.warning(f'历史文件解析失败（{HISTORY_FILE}）: {e}')
        return False
    with _lock:
        for k, v in data.items():
            if k not in HISTORY:
                continue
            local = HISTORY[k] if isinstance(HISTORY[k], list) else []
            if isinstance(v, list):
                merged: Dict[object, object] = {}
                for e in (list(local) + list(v)):
                    key = e.get('time') if isinstance(e, dict) and 'time' in e else id(e)
                    merged[key] = e
                items = list(merged.values())
                items.sort(key=lambda e: e.get('time', 0)
                           if isinstance(e, dict) else 0)
                HISTORY[k] = items[-MAX_ENTRIES:]
            else:
                HISTORY[k] = v
    _loaded_mtime = _file_mtime()
    return True


def _file_mtime():
    try:
        return os.path.getmtime(HISTORY_FILE)
    except OSError:
        return None


def reload_if_changed() -> bool:
    """文件变了就重读（跨进程场景）。

    ⚠️ **为什么必须有这个**：控制器进程每 5s 写一次历史，API 进程如果不重读，
    内存里的历史就永远停在启动那一刻 —— 对方每 5s 轮询 `/api/history`
    拿到的是同一份旧数据（2026-11-05 实测：恒为 0 条，因为 API 启动时
    控制器还没写第一份）。

    只在 mtime 变化时重读 → 每请求一次 stat 的开销，可忽略。
    """
    mtime = _file_mtime()
    if mtime is None or mtime == _loaded_mtime:
        return False
    return load()
