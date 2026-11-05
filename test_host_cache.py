#!/usr/bin/env python3
"""test_host_cache.py — 主机表的维护与淘汰（host_cache）

覆盖场景：
  1. touch：首次见到 / 又见到 / 换位置（三种事件）
  2. drop_switch：交换机断开 → 精确清掉它上面的主机（别的交换机的不动）
  3. evict_stale：TTL 内的留着，超时的淘汰掉
  4. host_ttl：环境变量可调，非法值退回默认（不能让控制器挂掉）
  5. count_fresh：体检读数只算还活着的主机

为什么单独测：controller_app.py 在 import 时就 eventlet.monkey_patch()，
在 pytest 里 import 它会污染整个进程 → 所以逻辑抽到了 host_cache.py。

运行: python3 -m pytest test_host_cache.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_cache  # noqa: E402


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'FAIL: {name} {detail}')


def _mk_hosts(*entries):
    """造一份 hosts 表：entries = (mac, dpid, port, last_seen)。"""
    return {mac: {'mac': mac, 'ipv4': [], 'dpid': dpid, 'port': port,
                  'first_seen': ts - 100, 'last_seen': ts}
            for mac, dpid, port, ts in entries}


# ====================================================================
# 1. touch
# ====================================================================

def test_touch_new_host():
    hosts = {}
    event, old = host_cache.touch(hosts, 'aa:bb:cc:dd:ee:01', '0001', 3, now=1000.0)
    check('首次见到 → new', event == 'new', event)
    check('没有旧位置', old is None, str(old))
    h = hosts['aa:bb:cc:dd:ee:01']
    check('条目建好了', h['dpid'] == '0001' and h['port'] == 3, str(h))
    check('first_seen / last_seen 都记了', h['first_seen'] == 1000.0
          and h['last_seen'] == 1000.0, str(h))
    check('ipv4 留空待填', h['ipv4'] == [], str(h))


def test_touch_seen_updates_last_seen_only():
    hosts = {}
    host_cache.touch(hosts, 'aa:bb:cc:dd:ee:01', '0001', 3, now=1000.0)
    event, old = host_cache.touch(hosts, 'aa:bb:cc:dd:ee:01', '0001', 3, now=1100.0)
    check('又见到 → seen', event == 'seen', event)
    check('last_seen 更新了', hosts['aa:bb:cc:dd:ee:01']['last_seen'] == 1100.0,
          str(hosts['aa:bb:cc:dd:ee:01']))
    check('first_seen 没动', hosts['aa:bb:cc:dd:ee:01']['first_seen'] == 1000.0,
          str(hosts['aa:bb:cc:dd:ee:01']))
    check('没有旧位置', old is None, str(old))


def test_touch_moved():
    hosts = {}
    host_cache.touch(hosts, 'aa:bb:cc:dd:ee:01', '0001', 3, now=1000.0)
    event, old = host_cache.touch(hosts, 'aa:bb:cc:dd:ee:01', '0002', 5, now=1100.0)
    check('换位置 → moved', event == 'moved', event)
    check('旧位置记下了', old == ('0001', 3), str(old))
    h = hosts['aa:bb:cc:dd:ee:01']
    check('位置更新了', h['dpid'] == '0002' and h['port'] == 5, str(h))


# ====================================================================
# 2. drop_switch
# ====================================================================

def test_drop_switch_only_that_switch():
    hosts = _mk_hosts(
        ('aa:00', '0001', 1, 1000.0),
        ('aa:01', '0001', 2, 1000.0),
        ('bb:00', '0002', 1, 1000.0),
    )
    gone = host_cache.drop_switch(hosts, '0001')
    check('删了 2 条', len(gone) == 2, str(gone))
    check('剩下的只有另一台交换机的主机',
          list(hosts.keys()) == ['bb:00'], str(list(hosts.keys())))


def test_drop_switch_no_match():
    hosts = _mk_hosts(('aa:00', '0001', 1, 1000.0))
    gone = host_cache.drop_switch(hosts, '9999')
    check('没有该交换机 → 删 0 条', gone == [], str(gone))
    check('原表不动', len(hosts) == 1, str(hosts))


def test_drop_switch_empty_table():
    check('空表不炸', host_cache.drop_switch({}, '0001') == [], '')


# ====================================================================
# 3. evict_stale
# ====================================================================

def test_evict_stale_keeps_fresh():
    hosts = _mk_hosts(
        ('aa:00', '0001', 1, 1000.0),      # 新鲜
        ('aa:01', '0001', 2, 100.0),       # 很久没见
    )
    gone = host_cache.evict_stale(hosts, ttl=300, now=1000.0)
    check('淘汰了 1 条', gone == ['aa:01'], str(gone))
    check('新鲜的留下了', list(hosts.keys()) == ['aa:00'], str(list(hosts.keys())))


def test_evict_stale_all_fresh():
    hosts = _mk_hosts(('aa:00', '0001', 1, 1000.0), ('aa:01', '0001', 2, 999.0))
    gone = host_cache.evict_stale(hosts, ttl=300, now=1000.0)
    check('全新鲜 → 不淘汰', gone == [], str(gone))
    check('表没变', len(hosts) == 2, str(hosts))


def test_evict_stale_boundary():
    """刚好等于 TTL 的不算过期（> ttl 才淘汰）。"""
    hosts = _mk_hosts(('aa:00', '0001', 1, 700.0))
    gone = host_cache.evict_stale(hosts, ttl=300, now=1000.0)
    check('正好 300s 不淘汰', gone == [], str(gone))
    hosts2 = _mk_hosts(('aa:00', '0001', 1, 699.0))
    gone2 = host_cache.evict_stale(hosts2, ttl=300, now=1000.0)
    check('301s 淘汰', gone2 == ['aa:00'], str(gone2))


def test_evict_stale_empty():
    check('空表不炸', host_cache.evict_stale({}, ttl=300) == [], '')


# ====================================================================
# 4. TTL 配置
# ====================================================================

def test_host_ttl_default():
    old = os.environ.pop(host_cache.TTL_ENV_VAR, None)
    try:
        check('默认 900 秒', host_cache.host_ttl() == 900,
              str(host_cache.host_ttl()))
    finally:
        if old is not None:
            os.environ[host_cache.TTL_ENV_VAR] = old


def test_host_ttl_from_env():
    old = os.environ.get(host_cache.TTL_ENV_VAR)
    os.environ[host_cache.TTL_ENV_VAR] = '1800'
    try:
        check('环境变量生效', host_cache.host_ttl() == 1800, str(host_cache.host_ttl()))
    finally:
        if old is None:
            os.environ.pop(host_cache.TTL_ENV_VAR, None)
        else:
            os.environ[host_cache.TTL_ENV_VAR] = old


def test_host_ttl_invalid_falls_back():
    """环境变量写坏了不能让控制器挂掉。"""
    old = os.environ.get(host_cache.TTL_ENV_VAR)
    os.environ[host_cache.TTL_ENV_VAR] = '不是数字'
    try:
        check('非法值退回默认', host_cache.host_ttl() == 900, str(host_cache.host_ttl()))
        os.environ[host_cache.TTL_ENV_VAR] = '0'
        check('0 也退回默认', host_cache.host_ttl() == 900, str(host_cache.host_ttl()))
        os.environ[host_cache.TTL_ENV_VAR] = '-5'
        check('负数也退回默认', host_cache.host_ttl() == 900, str(host_cache.host_ttl()))
    finally:
        if old is None:
            os.environ.pop(host_cache.TTL_ENV_VAR, None)
        else:
            os.environ[host_cache.TTL_ENV_VAR] = old


# ====================================================================
# 5. count_fresh
# ====================================================================

def test_count_fresh():
    hosts = _mk_hosts(
        ('aa:00', '0001', 1, 1000.0),
        ('aa:01', '0001', 2, 100.0),
    )
    n = host_cache.count_fresh(hosts, ttl=300, now=1000.0)
    check('只数 TTL 内的', n == 1, str(n))
    check('空表 → 0', host_cache.count_fresh({}, ttl=300) == 0, '')


def test_count_fresh_matches_eviction():
    """体检读数与淘汰结果必须一致（否则读数还是脏的）。"""
    hosts = _mk_hosts(
        ('aa:00', '0001', 1, 1000.0),
        ('aa:01', '0001', 2, 100.0),
        ('aa:02', '0001', 3, 500.0),
    )
    before = host_cache.count_fresh(hosts, ttl=300, now=1000.0)
    host_cache.evict_stale(hosts, ttl=300, now=1000.0)
    after = host_cache.count_fresh(hosts, ttl=300, now=1000.0)
    check('淘汰前后读数一致', before == after == 1, f'{before} → {after}')
    check('表里也只剩 1 条', len(hosts) == 1, str(hosts))


def test_entries_without_last_seen_are_kept():
    """没有 last_seen 的条目：无法判定陈旧 → 不淘汰、也不从读数里抹掉。

    缺字段不等于陈旧 —— 误杀还在线的主机比留一条幽灵更糟。
    """
    hosts = {'aa:00': {'mac': 'aa:00', 'ipv4': [], 'dpid': '0001', 'port': 1}}
    gone = host_cache.evict_stale(hosts, ttl=300, now=1000.0)
    check('不淘汰', gone == [], str(gone))
    check('条目还在', 'aa:00' in hosts, str(hosts))
    check('读数把它算进去', host_cache.count_fresh(hosts, ttl=300, now=1000.0) == 1,
          str(host_cache.count_fresh(hosts, ttl=300, now=1000.0)))


# ====================================================================
# 6. 端到端场景：换拓扑后旧主机必须消失
# ====================================================================

def test_topology_change_leaves_no_ghost_hosts():
    """实测场景：Mininet 重启 → 交换机断开 → 旧主机被清掉 → 重新发现新的。"""
    hosts = {}
    # 旧拓扑：4 台主机
    for i in range(1, 5):
        host_cache.touch(hosts, f'00:00:00:00:00:0{i}', '0001', i, now=1000.0)
    check('旧拓扑 4 台', len(hosts) == 4, str(len(hosts)))

    # 交换机断开 → 精确清理
    host_cache.drop_switch(hosts, '0001')
    check('交换机断开后主机清空', len(hosts) == 0, str(hosts))

    # 新拓扑：4 台新主机（MAC 不同）
    for i in range(1, 5):
        host_cache.touch(hosts, f'11:22:33:44:55:0{i}', '0002', i, now=1100.0)
    check('新拓扑又是 4 台', len(hosts) == 4, str(len(hosts)))
    check('没有残留旧 MAC',
          all(k.startswith('11:') for k in hosts), str(list(hosts.keys())))
