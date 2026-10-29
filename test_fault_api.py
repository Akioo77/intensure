"""单元测试：故障注入 REST 后端（v3.19.0）

测试的是**纯逻辑**（别名映射、接口解析、命令拼装、清理幂等性），
所有特权命令（ip link / tc / ovs-ofctl / ovs-vsctl）都被 mock 掉 ——
测试不该真的动网络。

跑法: pytest test_fault_api.py -v
"""
import sys

# ---- 用假实现替换所有特权命令 ----
RUN_CALLS = []


def _fake_run(cmd, check=True):
    """记录调用并假装成功。"""
    RUN_CALLS.append(list(cmd))
    if check:
        return 'ok\n'
    return ''


def _make_api(ports=None):
    """造一个不执行真实命令的 FaultApi 实例。

    特权命令（ip link / tc / ovs-ofctl）全被 mock 掉 ——
    测试不该真的动网络。
    """
    import fault_api
    api = fault_api.FaultApi()
    api._run = staticmethod(_fake_run)
    api._all_switch_ports = staticmethod(
        lambda: {sw: list(ps) for sw, ps in (ports or {
            's1': ['s1-eth1', 's1-eth2', 's1-eth3', 's1-eth4']}).items()})
    return api


def test_alias_mapping():
    """业务语义名 → 网络语义名的别名映射齐全。"""
    import fault_api
    # 演示脚本 04 用的三个名字必须都有映射
    for name in ('link_down', 'high_latency', 'packet_loss', 'flow_missing'):
        assert name in fault_api.FAULT_ALIASES, f'缺少别名: {name}'
    # 别名指向的实现名必须真实存在
    impl = set()
    for v in fault_api.FAULT_ALIASES.values():
        impl.add(v)
    for name in impl:
        if name == 'clear_all':
            continue
        assert hasattr(fault_api.FaultApi, f'_do_{name}'), f'无实现: _do_{name}'


def test_resolve_exact_interface():
    """target 是具体接口名 → 解析成它自己。"""
    api = _make_api()
    got = api._resolve_interfaces('s1-eth2')
    assert got == ['s1-eth2'], got


def test_resolve_switch_returns_all_ports():
    """target 是交换机名 → 返回该交换机所有端口。"""
    api = _make_api()
    got = api._resolve_interfaces('s1')
    assert got == ['s1-eth1', 's1-eth2', 's1-eth3', 's1-eth4'], got


def test_resolve_switch_with_port_param():
    """params.port 限定单个端口。"""
    api = _make_api()
    got = api._resolve_interfaces('s1', {'port': 3})
    assert got == ['s1-eth3'], got


def test_resolve_unknown_interface_raises():
    """接口不存在 → 抛 FaultInjectionError（路由层转 400）。"""
    import fault_api
    api = _make_api()
    try:
        api._resolve_interfaces('s9-eth1')
    except fault_api.FaultInjectionError:
        return
    raise AssertionError('不存在的接口应该抛 FaultInjectionError')


def test_unknown_fault_type_raises():
    """未知故障类型 → 抛异常。"""
    import fault_api
    api = _make_api()
    try:
        api.inject('nonsense', 's1-eth1')
    except fault_api.FaultInjectionError:
        return
    raise AssertionError('未知故障类型应该抛 FaultInjectionError')


def test_tc_args_are_separate_argv():
    """tc 的复合参数必须拆成独立 argv 元素。

    这是实测踩过的坑：写成 f'rate {x}mbit' 会让 tc 报
    `unknown parameter "rate 1.0mbit"`。
    """
    RUN_CALLS.clear()
    api = _make_api()
    api.inject('rate_limit', 's1-eth1', {'rate_mbps': 1, 'burst_kb': 32})
    cmd = RUN_CALLS[-1]
    assert cmd[:5] == ['tc', 'qdisc', 'replace', 'dev', 's1-eth1'], cmd
    # rate / 值 必须是两个独立元素，不能是 'rate 1mbit'
    assert 'rate' in cmd, cmd
    assert '1mbit' in cmd, cmd
    assert not any(' ' in c and c.startswith('rate ') for c in cmd), cmd
    # 数字不能带小数尾巴（tc 不认 '1.0mbit'）
    assert '1mbit' in cmd and '1.0mbit' not in cmd, cmd


def test_num_formats_cleanly():
    """_num: 1.0 → '1', 1.5 → '1.5'。"""
    import fault_api
    assert fault_api._num(1.0) == '1'
    assert fault_api._num(1.5) == '1.5'
    assert fault_api._num(300) == '300'


def test_link_down_uses_two_commands():
    """link_down 要同时 down 内核接口 + 通知 OVS（否则控制器看不到链路断）。"""
    RUN_CALLS.clear()
    api = _make_api()
    api.inject('link_down', 's1-eth1')
    cmds = [' '.join(c) for c in RUN_CALLS]
    assert any('ip link set s1-eth1 down' in c for c in cmds), cmds
    assert any('ovs-ofctl' in c and 'mod-port s1 s1-eth1 down' in c for c in cmds), cmds


def test_clear_all_tolerates_missing_qdisc():
    """清理时没有 qdisc 是**正常**的 → 不能抛异常（幂等）。"""
    import fault_api

    def run_raising_on_tc(cmd, check=True):
        RUN_CALLS.append(list(cmd))
        if check and cmd[:2] == ['tc', 'qdisc'] and 'del' in cmd:
            raise fault_api.FaultInjectionError(
                'Cannot delete qdisc with handle of zero.')
        return 'ok\n'

    api = _make_api()
    api._run = staticmethod(run_raising_on_tc)
    # tc del 的失败必须被吞掉（check=False）
    api._run = staticmethod(lambda cmd, check=True: run_raising_on_tc(cmd, check=False))
    result = api.clear_all('s1')
    assert result['cleared'] == 4, result
    assert len(result['interfaces']) == 4, result


def test_clear_all_clears_every_interface():
    """全量清理必须清**所有端口**（不能只清 1 个）。

    踩过的坑：_all_switch_ports() 返回 {sw: [ports]} 字典，
    直接迭代拿到的是 switch 名而不是端口。
    """
    RUN_CALLS.clear()
    api = _make_api()
    result = api.clear_all()
    assert result['cleared'] == 4, result
    assert sorted(result['interfaces']) == [
        's1-eth1', 's1-eth2', 's1-eth3', 's1-eth4'], result


def test_active_faults_tracked():
    """注入后能查到活跃故障。"""
    api = _make_api()
    api.inject('link_down', 's1-eth1')
    active = api.active_faults()
    assert len(active) == 1, active
    assert active[0]['fault_type'] == 'link_down', active
    assert active[0]['mode'] == 'test', active


if __name__ == '__main__':
    failed = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'  [PASS] {name}')
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f'  [FAIL] {name}: {e}')
    print(f'\n{"全部通过" if not failed else f"{failed} 个失败"}')
    sys.exit(1 if failed else 0)
