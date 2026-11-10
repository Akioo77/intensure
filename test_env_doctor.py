#!/usr/bin/env python3
"""test_env_doctor.py — 环境体检（env_doctor）的判定语义

重点覆盖「主机已发现」这一项 —— 它的判定前提很容易定错：

**主机的 MAC 是被动学到的** —— 主机不发包，我们就看不到它。
所以「发现的主机数 >= 期望」这个假设，在有 TTL 淘汰之后必然不稳：
2026-11-08 实测，闲置 15 分钟的主机被淘汰 → 3 台 < 期望 4 台 →
体检误报「环境坏了」，其实只是那台主机没说话。

判定应该是：
- 发现数 >= 期望 → 通过（info 级）
- 少于期望 → **提示级**（说明白原因），不是错误
- 一台都没发现 → 同样**只是提示**，不是错误

⚠️ 为什么「一台都没发现」也不判错：
主机数衡量的是「最近有谁在说话」，不是环境好坏。采集层健不健康，
由**交换机已连接 / 状态新鲜度 / 链路发现方式**判定 —— 那三项才是判据。
如果主机全静默也判 error，那么一台真实园区网络在深夜（没人发包）就会被
误报成「环境坏了」，而实际上交换机连着、状态新鲜、链路正常。

运行: python3 -m pytest test_env_doctor.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env_config  # noqa: E402
import env_doctor   # noqa: E402


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'FAIL: {name} {detail}')


def _state(hosts=0, switches=1):
    return {
        'meta': {'update_count': 1},
        'switches': {
            f'000000000000000{i+1}': {
                'ports': [{'port_no': j + 1, 'name': f's1-eth{j+1}', 'state': 0}
                          for j in range(4)],
            } for i in range(switches)
        },
        'hosts': {f'00:00:00:00:00:{i+1:02x}': {'ipv4': [f'10.0.0.{i+1}']}
                  for i in range(hosts)},
        'links': {},
    }


def _cfg(tmp_path):
    """写一个临时 env.json，期望 1 交换机 / 4 主机 / 0 条交换机间链路。"""
    import json
    p = os.path.join(tmp_path, 'env.json')
    with open(p, 'w') as f:
        json.dump({
            'mode': 'active',
            'topology_expect': {'switches': 1, 'hosts': 4, 'inter_switch_links': 0},
        }, f)
    return env_config.EnvConfig.load(p)


def _host_check(checks):
    for c in checks:
        if c.name.startswith('主机已发现'):
            return c
    return None


def test_hosts_count_meets_expectation():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        checks = env_doctor.run_checks(_state(hosts=4), cfg)
        c = _host_check(checks)
        check('找到主机检查项', c is not None)
        check('4 台 >= 期望 4 → 通过', c.ok is True, c.detail)
        check('级别是 info（不参与失败判定）', c.level == 'info', c.level)


def test_hosts_partially_silent_is_warning_not_error():
    """发现 3 台 < 期望 4 台 → 提示级，不是错误。

    ⚠️ 曾经的 bug：这里按「数量不足」判成 error → 体检报「环境坏了」，
    实际只是那台主机没发包（MAC 是被动学习的）。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        checks = env_doctor.run_checks(_state(hosts=3), cfg)
        c = _host_check(checks)
        check('找到主机检查项', c is not None)
        check('3 台 < 期望 4 → 不算错误', c.ok is True, c.detail)
        check('级别是 warn', c.level == 'warn', c.level)
        check('说明里讲清了原因', '被动学习' in c.detail or '不发包' in c.detail, c.detail)


def test_no_host_found_is_not_an_error():
    """一台都没发现 → 只是提示，**不是错误**。

    ⚠️ 曾经的 bug（v3.21.0 → v3.22.0 改）：这里判成 error，
    于是「所有主机都静默」的夜晚，体检会说环境坏了 ——
    而交换机连着、状态新鲜、链路都正常，其实好得很。
    主机数不是健康判据，判据是交换机连接 / 状态新鲜度 / 链路发现。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(tmp)
        checks = env_doctor.run_checks(_state(hosts=0), cfg)
        c = _host_check(checks)
        check('找到主机检查项', c is not None)
        check('0 台 → 不算错误', c.ok is True, c.detail)
        check('级别是 warn', c.level == 'warn', c.level)
        check('说明里讲清了原因', '被动学习' in c.detail or '不发包' in c.detail, c.detail)


def test_host_count_never_enters_failed_list():
    """无论发现几台主机，都不会出现在 failed 列表里。

    这条是给 JSON 输出和退出码用的：failed 列表为空 → 退出码 0。
    """
    import tempfile
    for n in (0, 1, 3):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _cfg(tmp)
            checks = env_doctor.run_checks(_state(hosts=n), cfg)
            failed = [c.name for c in checks if not c.ok and c.level == 'error']
            check(f'发现 {n} 台时 failed 列表不含主机项',
                  not any('主机' in name for name in failed), failed)


def test_no_expectation_configured():
    """没配期望值 → 不做判定。"""
    import json
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        # ⚠️ 必须**显式**写 hosts: None —— EnvConfig 是深合并，
        #    只写 {"switches": 2} 仍会带上内置默认的 hosts: 4；
        #    而 load("/nonexistent") 走 fail-safe 回退到内置默认，也一样有 hosts: 4。
        cfg_path = os.path.join(tmp, 'env.json')
        with open(cfg_path, 'w') as f:
            json.dump({'topology_expect': {'hosts': None}}, f)
        cfg = env_config.EnvConfig.load(cfg_path)
        checks = env_doctor.run_checks(_state(hosts=0), cfg)
        c = _host_check(checks)
        check('找到主机检查项', c is not None)
        check('没期望值 → 不判错', c.ok is True, c.detail)


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
