"""host_cache.py —— 主机表的维护与淘汰（纯逻辑，无第三方依赖）

## 为什么单独拆一个模块
------------------------------------------------------------------------
`controller_app.py` 在 **import 时**就调 `eventlet.monkey_patch()`（必须在
os_ken 之前），单元测试里 import 它会污染整个 pytest 进程（socket / time
全被 patch），所以这块逻辑单独拎出来：

- 纯逻辑，测起来不碰网络
- controller_app 只需要调用几个函数，行为一目了然

## 要解决的真问题
------------------------------------------------------------------------
`hosts` 是「MAC → 主机信息」的字典，**之前只增不减**：
交换机断开时只更新交换机条目，它学到的主机永远留着 →
换过拓扑后旧 MAC 一直堆着（实测：4 台真实主机 + 4 条残留，
`env_doctor` 读数变成「发现 8 台（期望 4）」）。

真实园区里主机频繁上下线，这个字典会**无界增长**，而且状态每 5 秒
全量写库 → `state_history` 会被撑大。

## 淘汰策略（两层）
------------------------------------------------------------------------
① **按交换机精确清理** —— 交换机断了，它上面的主机也不在了 → 立即删
② **TTL 兜底** —— 长时间没再见到的主机（静默掉线 / 交换机异常消失）

TTL 默认 15 分钟：够长，不会误杀"安静但在线"的主机；短于它的话，
笔记本休眠后唤醒会被重新"发现"一次（多一条 host_discovered 事件）。
调整：`export INTENSURE_HOST_TTL=1800`
"""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Tuple

# 主机条目存活时间（秒）
DEFAULT_HOST_TTL = 900

# 环境变量名（两个默认值：采集侧自己用 + 外部可覆盖）
TTL_ENV_VAR = 'INTENSURE_HOST_TTL'


def host_ttl(default: int = DEFAULT_HOST_TTL) -> int:
    """当前生效的 TTL（秒）。环境变量非法时退回默认值，不让控制器挂掉。"""
    raw = os.environ.get(TTL_ENV_VAR)
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def touch(hosts: Dict[str, dict], mac: str, dpid: str, port: int,
          now: Optional[float] = None) -> Tuple[str, Optional[Tuple[str, int]]]:
    """登记/刷新一个主机条目。

    返回 (事件, 旧位置)：
      事件 = 'new'   首次见到
           = 'seen'  又见到，位置没变
           = 'moved' 又见到，但换位置了（旧位置在第二个返回值里）
    """
    now = now if now is not None else time.time()
    host = hosts.get(mac)
    if host is None:
        hosts[mac] = {
            'mac': mac,
            'ipv4': [],
            'dpid': dpid,
            'port': port,
            'first_seen': now,
            'last_seen': now,
        }
        return ('new', None)

    host['last_seen'] = now
    if host.get('dpid') != dpid or host.get('port') != port:
        old = (host.get('dpid'), host.get('port'))
        host['dpid'] = dpid
        host['port'] = port
        return ('moved', old)
    return ('seen', None)


def drop_switch(hosts: Dict[str, dict], dpid: str) -> List[str]:
    """删掉属于某台交换机的主机条目 → 返回被删的 MAC 列表。

    交换机断开时立刻调用：交换机没了，它上面的主机也不在了。
    """
    if not hosts:
        return []
    gone = [mac for mac, h in hosts.items() if h.get('dpid') == dpid]
    for mac in gone:
        del hosts[mac]
    return gone


def evict_stale(hosts: Dict[str, dict], ttl: Optional[int] = None,
                now: Optional[float] = None) -> List[str]:
    """淘汰超过 TTL 没再见到的主机 → 返回被淘汰的 MAC 列表。

    ttl 传 None 时读环境变量（INTENSURE_HOST_TTL）。

    ⚠️ **没有 last_seen 的条目不淘汰**：没时间戳就没法证明它陈旧，
    宁可不淘汰，也不要把还在线的主机误杀。
    """
    if not hosts:
        return []
    ttl = host_ttl() if ttl is None else ttl
    now = now if now is not None else time.time()
    gone = []
    for mac, h in hosts.items():
        last_seen = h.get('last_seen')
        if last_seen is None:
            continue
        if now - last_seen > ttl:
            gone.append(mac)
    for mac in gone:
        del hosts[mac]
    return gone


def count_fresh(hosts: Dict[str, dict], ttl: Optional[int] = None,
                now: Optional[float] = None) -> int:
    """当前还"活着"的主机数（TTL 内的）。

    给 /api/capabilities 用 —— 体检读数不能把幽灵主机算进去。

    ⚠️ **没有 last_seen 的条目算"活着"**：没有时间戳就没法证明它陈旧，
    不能因为缺字段就把它从读数里抹掉（那会让体检误报"没发现主机"）。
    """
    if not hosts:
        return 0
    ttl = host_ttl() if ttl is None else ttl
    now = now if now is not None else time.time()
    fresh = 0
    for h in hosts.values():
        last_seen = h.get('last_seen')
        if last_seen is None:
            fresh += 1          # 没时间戳 → 无法判定陈旧 → 计入
            continue
        if now - last_seen <= ttl:
            fresh += 1
    return fresh
