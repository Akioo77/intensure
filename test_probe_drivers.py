#!/usr/bin/env python3
"""test_probe_drivers.py — 探测驱动适配层（probe_drivers）

覆盖场景：
  1. 驱动工厂：按名字选驱动、未知名字退回 icmp
  2. 四个驱动的 available()（缺依赖时要说清楚缺什么）
  3. ICMP / Mininet 驱动的结果解析（用假 target，不真发包）
  4. CLI 驱动：命令模板替换 + 正则解析
  5. SNMP：**BER 编码字节级** + **响应解析** + **真实 UDP 收发**（本地起 agent）
  6. env_config 的 snmp / cli 子配置读取

运行: python3 -m pytest test_probe_drivers.py -v
"""
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env_config      # noqa: E402
import exec_target     # noqa: E402
import probe_drivers   # noqa: E402
from probe_drivers import (DRIVER_CLI, DRIVER_ICMP, DRIVER_MININET,  # noqa: E402
                           DRIVER_SNMP, ProbeResult)


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'FAIL: {name} {detail}')


# ====================================================================
# 假 target —— 记录命令、按脚本返回输出
# ====================================================================

class FakeTarget(exec_target.ExecTarget):
    """假执行目标：按「输出脚本」返回结果，不真的执行命令。

    继承真实的 ExecTarget —— 这样 `ping()` 的解析逻辑（pgrep → mnexec）
    走的是真代码，只是底层命令被假掉了。
    """

    kind = 'fake'

    def __init__(self, outputs=None, default=(0, '', '')):
        self.outputs = outputs or {}
        self.default = default
        self.calls = []

    def run(self, argv, timeout=10.0):
        self.calls.append(list(argv))
        joined = ' '.join(argv)
        for pattern, out in self.outputs.items():
            if pattern in joined:
                rc, stdout, stderr = out
                return exec_target.CommandResult(
                    returncode=rc, stdout=stdout, stderr=stderr)
        rc, stdout, stderr = self.default
        return exec_target.CommandResult(
            returncode=rc, stdout=stdout, stderr=stderr)


# ====================================================================
# 1. 驱动工厂
# ====================================================================

def test_build_driver_picks_class():
    for name, cls in ((DRIVER_MININET, probe_drivers.MininetDriver),
                      (DRIVER_ICMP, probe_drivers.IcmpDriver),
                      (DRIVER_SNMP, probe_drivers.SnmpDriver),
                      (DRIVER_CLI, probe_drivers.CliDriver)):
        d = probe_drivers.build_driver(name)
        check(f'{name} → {cls.__name__}', isinstance(d, cls), str(d))
        check(f'{name} 的 name 属性对', d.name == name, d.name)


def test_build_driver_unknown_falls_back_to_icmp():
    """未知驱动不能炸 —— 退回 icmp，至少还能测点什么。"""
    d = probe_drivers.build_driver('nosuchdriver')
    check('未知驱动退回 icmp', isinstance(d, probe_drivers.IcmpDriver), str(d))


def test_list_drivers_contains_four():
    names = [d['name'] for d in probe_drivers.list_drivers()]
    for want in (DRIVER_MININET, DRIVER_ICMP, DRIVER_SNMP, DRIVER_CLI):
        check(f'驱动列表里有 {want}', want in names, str(names))


# ====================================================================
# 2. available()
# ====================================================================

def test_snmp_driver_needs_host():
    d = probe_drivers.SnmpDriver(cfg={})
    ok, reason = d.available()
    check('SNMP 没配 host → 不可用', ok is False, reason)
    check('原因里说清楚缺什么', 'host' in reason, reason)
    d2 = probe_drivers.SnmpDriver(cfg={'host': '10.0.0.2'})
    ok2, _ = d2.available()
    check('SNMP 配了 host → 可用', ok2 is True, str(ok2))


def test_cli_driver_needs_command_template():
    d = probe_drivers.CliDriver(cfg={})
    ok, reason = d.available()
    check('CLI 没配模板 → 不可用', ok is False, reason)
    check('原因里提到 command', 'command' in reason, reason)
    d2 = probe_drivers.CliDriver(cfg={'command': 'ping -c 3 {dst}'})
    ok2, _ = d2.available()
    check('CLI 配了模板 → 可用', ok2 is True, str(ok2))


# ====================================================================
# 3. ICMP / Mininet 驱动
# ====================================================================

PING_OUT = ('3 packets transmitted, 3 received, 0% packet loss, time 2002ms\n'
            'rtt min/avg/max/mdev = 0.045/0.052/0.061/0.007 ms\n')


def test_icmp_driver_parses_ping():
    t = FakeTarget(default=(0, PING_OUT, ''))
    d = probe_drivers.IcmpDriver(cfg={}, target=t)
    r = d.probe('10.0.0.1', '10.0.0.4')
    check('可达', r.reachable is True, str(r))
    check('丢包 0', r.loss_pct == 0.0, str(r.loss_pct))
    check('时延取 avg', abs((r.latency_ms or 0) - 0.052) < 1e-6, str(r.latency_ms))
    check('method 是 icmp', r.method == DRIVER_ICMP, r.method)
    check('确实调了 ping', any('ping' in ' '.join(c) for c in t.calls), str(t.calls))


def test_icmp_driver_detects_unreachable():
    out = '3 packets transmitted, 0 received, 100% packet loss, time 2002ms\n'
    t = FakeTarget(default=(1, out, ''))
    d = probe_drivers.IcmpDriver(cfg={}, target=t)
    r = d.probe('10.0.0.1', '10.0.0.4')
    check('不可达', r.reachable is False, str(r))
    check('丢包 100', r.loss_pct == 100.0, str(r.loss_pct))


def test_mininet_driver_uses_mnexec():
    """Mininet 驱动要进源主机的命名空间发包（mnexec -a <pid>）。"""
    t = FakeTarget(outputs={'pgrep': (0, '26024\n', '')},
                   default=(0, PING_OUT, ''))
    d = probe_drivers.MininetDriver(cfg={}, target=t)
    r = d.probe('h1', '10.0.0.4')
    check('走了 mnexec', any('mnexec' in ' '.join(c) for c in t.calls), str(t.calls))
    check('带上了 PID', any('26024' in ' '.join(c) for c in t.calls), str(t.calls))
    check('可达', r.reachable is True, str(r))
    check('method 是 mininet', r.method == DRIVER_MININET, r.method)


def test_driver_without_destination_is_graceful():
    for cls in (probe_drivers.IcmpDriver, probe_drivers.MininetDriver):
        d = cls(cfg={}, target=FakeTarget())
        r = d.probe('10.0.0.1', '')
        check(f'{cls.__name__} 没目标时不崩', r.reachable is False, str(r))
        check('detail 说清楚', 'destination' in r.detail, r.detail)


# ====================================================================
# 4. CLI 驱动
# ====================================================================

def test_cli_driver_substitutes_and_parses():
    t = FakeTarget(default=(0, PING_OUT, ''))
    d = probe_drivers.CliDriver(cfg={'command': 'ping -c 3 {dst}'}, target=t)
    r = d.probe('10.0.0.1', '10.0.0.4')
    check('模板里的 {dst} 被替换', any('10.0.0.4' in ' '.join(c) for c in t.calls),
          str(t.calls))
    check('没有残留 {dst}', not any('{dst}' in ' '.join(c) for c in t.calls),
          str(t.calls))
    check('可达', r.reachable is True, str(r))
    check('丢包 0', r.loss_pct == 0.0, str(r.loss_pct))
    check('method 是 cli', r.method == DRIVER_CLI, r.method)


def test_cli_driver_custom_regex():
    """厂商 CLI 的输出格式各不相同 → 正则要能配。"""
    out = 'Average delay: 12.5ms, packet loss: 25 percent'
    t = FakeTarget(default=(0, out, ''))
    d = probe_drivers.CliDriver(cfg={
        'command': 'show health',
        'loss_regex': 'packet loss:\\s*(\\d+) percent',
        'latency_regex': 'Average delay:\\s*([\\d.]+)ms',
    }, target=t)
    r = d.probe('10.0.0.1', '10.0.0.4')
    check('自定义正则解析出丢包 25', r.loss_pct == 25.0, str(r.loss_pct))
    check('自定义正则解析出时延 12.5', abs((r.latency_ms or 0) - 12.5) < 1e-6,
          str(r.latency_ms))
    # 25% 丢包 = 链路劣化但**仍然可达**（不是不可达）
    check('25% 丢包仍算可达（劣化 ≠ 断）', r.reachable is True, str(r))


def test_cli_driver_full_loss_is_unreachable():
    out = 'Average delay: 0ms, packet loss: 100 percent'
    t = FakeTarget(default=(1, out, ''))
    d = probe_drivers.CliDriver(cfg={
        'command': 'show health',
        'loss_regex': 'packet loss:\\s*(\\d+) percent',
    }, target=t)
    r = d.probe('10.0.0.1', '10.0.0.4')
    check('100% 丢包 → 不可达', r.reachable is False, str(r))
    check('丢包 100', r.loss_pct == 100.0, str(r.loss_pct))


# ====================================================================
# 5. SNMP —— 编码 / 解析 / 真实收发
# ====================================================================

def test_ber_oid_encoding_bytes():
    """OID 1.3.6.1.2.1.1.3.0 的 BER 编码应为 2b 06 01 02 01 01 03 00。"""
    encoded = probe_drivers._ber_oid('1.3.6.1.2.1.1.3.0')
    check('OID 编码字节对', encoded == bytes([0x06, 0x08, 0x2b, 0x06, 0x01,
                                             0x02, 0x01, 0x01, 0x03, 0x00]),
          encoded.hex())


def test_build_snmp_get_structure():
    pkt = probe_drivers.build_snmp_get('1.3.6.1.2.1.1.3.0', 'public', req_id=42)
    check('是 SEQUENCE 开头', pkt[0] == 0x30, pkt[:4].hex())
    check('长度字段对', pkt[1] == len(pkt) - 2, f'{pkt[1]} vs {len(pkt) - 2}')
    check('含 community "public"', b'public' in pkt, pkt.hex())
    check('含 GetRequest tag 0xA0', b'\xa0' in pkt, pkt.hex())
    check('含 request-id 42', b'\x2a' in pkt, pkt.hex())


def test_parse_snmp_response_counter():
    """构造一个 GetResponse（Counter32）并解析。"""
    def tlv(tag, value):
        return probe_drivers._ber_tlv(tag, value)
    oid = probe_drivers._ber_oid('1.3.6.1.2.1.1.3.0')
    varbind = probe_drivers._ber_sequence(oid, tlv(0x41, (12345).to_bytes(4, 'big')))
    pdu = tlv(0xA2,
              probe_drivers._ber_int(42) +
              probe_drivers._ber_int(0) +
              probe_drivers._ber_int(0) +
              probe_drivers._ber_sequence(varbind))
    msg = probe_drivers._ber_sequence(probe_drivers._ber_int(1),
                                      tlv(0x04, b'public'),
                                      pdu)
    parsed = probe_drivers.parse_snmp_response(msg)
    check('解析没报错', parsed['error'] is None, str(parsed))
    check('Counter32 值取到了', parsed['value'] == 12345, str(parsed))


def test_parse_snmp_response_error_status():
    """agent 回 error-status → 要能识别出来，不能当成正常值。"""
    def tlv(tag, value):
        return probe_drivers._ber_tlv(tag, value)
    oid = probe_drivers._ber_oid('1.3.6.1.2.1.1.3.0')
    varbind = probe_drivers._ber_sequence(oid, tlv(0x05, b''))
    pdu = tlv(0xA2,
              probe_drivers._ber_int(42) +
              probe_drivers._ber_int(2) +          # error-status = noSuchName
              probe_drivers._ber_int(1) +
              probe_drivers._ber_sequence(varbind))
    msg = probe_drivers._ber_sequence(probe_drivers._ber_int(1),
                                      tlv(0x04, b'public'), pdu)
    parsed = probe_drivers.parse_snmp_response(msg)
    check('识别出 error-status', parsed['error'] is not None, str(parsed))
    check('错误信息里有 error-status', 'error-status' in str(parsed['error']),
          str(parsed))


def test_parse_snmp_response_garbage():
    parsed = probe_drivers.parse_snmp_response(b'not-snmp-at-all')
    check('垃圾数据不会抛异常', parsed['error'] is not None, str(parsed))


def test_snmp_driver_real_udp_roundtrip():
    """真起一个 UDP agent 应答 → 驱动应判定可达并拿到时延。"""
    def tlv(tag, value):
        return probe_drivers._ber_tlv(tag, value)

    def make_response(req_id=1):
        oid = probe_drivers._ber_oid('1.3.6.1.2.1.1.3.0')
        varbind = probe_drivers._ber_sequence(
            oid, tlv(0x41, (999).to_bytes(4, 'big')))
        pdu = tlv(0xA2,
                  probe_drivers._ber_int(req_id) +
                  probe_drivers._ber_int(0) +
                  probe_drivers._ber_int(0) +
                  probe_drivers._ber_sequence(varbind))
        return probe_drivers._ber_sequence(probe_drivers._ber_int(1),
                                           tlv(0x04, b'public'), pdu)

    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(('127.0.0.1', 0))
    srv.settimeout(5)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def agent():
        try:
            while not stop.is_set():
                try:
                    data, addr = srv.recvfrom(4096)
                except socket.timeout:
                    return
                srv.sendto(make_response(), addr)
        finally:
            srv.close()

    th = threading.Thread(target=agent, daemon=True)
    th.start()
    try:
        d = probe_drivers.SnmpDriver(cfg={'host': '127.0.0.1', 'port': port,
                                           'community': 'public',
                                           'oid': '1.3.6.1.2.1.1.3.0',
                                           'timeout': 3})
        r = d.probe('', '')
        check('SNMP 应答 → 可达', r.reachable is True, str(r))
        check('丢包 0', r.loss_pct == 0.0, str(r.loss_pct))
        check('有时延', r.latency_ms is not None and r.latency_ms >= 0,
              str(r.latency_ms))
        check('method 是 snmp', r.method == DRIVER_SNMP, r.method)
        check('带上了 OID 与取值',
              r.extra.get('snmp_value') == 999, str(r.extra))
    finally:
        stop.set()


def test_snmp_driver_timeout_is_unreachable():
    """没有 agent 应答 → 超时 → 不可达（不能卡死）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(('127.0.0.1', 0))
    port = srv.getsockname()[1]      # 绑了但不回 → 必然超时
    srv.close()
    d = probe_drivers.SnmpDriver(cfg={'host': '127.0.0.1', 'port': port,
                                       'timeout': 1})
    r = d.probe('', '')
    check('超时 → 不可达', r.reachable is False, str(r))
    check('丢包 100', r.loss_pct == 100.0, str(r.loss_pct))
    check('detail 说超时', '超时' in r.detail, r.detail)


# ====================================================================
# 6. env_config 子配置
# ====================================================================

def test_env_config_probe_sub_configs():
    cfg = env_config.EnvConfig.load()
    check('probe_snmp 是 dict', isinstance(cfg.probe_snmp, dict), str(cfg.probe_snmp))
    check('probe_cli 是 dict', isinstance(cfg.probe_cli, dict), str(cfg.probe_cli))
    check('默认 driver 是 mininet', cfg.probe_driver == 'mininet', cfg.probe_driver)


def test_env_config_repo_env_json_has_driver_config():
    """仓库里的 env.json 必须带全 4 个驱动要用的字段。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'env.json')
    cfg = env_config.EnvConfig.load(path)
    check('env.json 的 probe.driver 读到了', cfg.probe_driver == 'mininet',
          cfg.probe_driver)
    check('env.json 的 snmp 段读到了', 'host' in cfg.probe_snmp, str(cfg.probe_snmp))
    check('env.json 的 cli 段读到了', 'command' in cfg.probe_cli, str(cfg.probe_cli))


def test_driver_reads_sub_config_from_whole_probe_section():
    """驱动拿到的可能是**整段 probe**，自己的配置在子段里（snmp / cli）。

    踩过的坑（实测发现）：工厂把整段 probe 传给驱动，而驱动只在自己那层找
    `host` → 读不到 → 驱动永远「没配 host」→ SNMP 探测直接超时。
    → 驱动用 `opt()` 取值：顶层找不到就去自己的子段找，两种形状都能用。
    """
    d = probe_drivers.SnmpDriver(cfg={'driver': 'snmp',
                                       'snmp': {'host': '10.0.0.2', 'port': 161}})
    check('从子段读到 host', d.opt('host') == '10.0.0.2', str(d.opt('host')))
    ok, reason = d.available()
    check('因此 available 通过', ok is True, reason)

    d2 = probe_drivers.SnmpDriver(cfg={'host': '10.0.0.3'})     # 直接传子配置
    check('直接传子配置也读得到', d2.opt('host') == '10.0.0.3', str(d2.opt('host')))

    d3 = probe_drivers.CliDriver(cfg={'cli': {'command': 'ping {dst}'}})
    check('CLI 也能从子段读到', d3.opt('command') == 'ping {dst}', str(d3.opt('command')))


def test_probe_result_as_dict():
    r = ProbeResult(reachable=True, loss_pct=0.0, latency_ms=1.5,
                    method='icmp', detail='ok', extra={'x': 1})
    d = r.as_dict()
    check('字段齐', d['reachable'] is True and d['method'] == 'icmp', str(d))
    check('extra 合并进去了', d['x'] == 1, str(d))


def test_list_drivers_uses_real_sub_config():
    """capabilities 报的驱动可用性必须按**真实配置**判。

    ⚠️ 之前 list_drivers 一律用空配置造驱动 → snmp/cli 永远报"没配"，
    而 env_doctor 传了真实配置 → 同一个驱动两个工具结论矛盾。
    """
    import os
    import tempfile
    import env_config

    old = env_config.get_config()
    tmp = tempfile.NamedTemporaryFile(prefix='_env_', suffix='.json', delete=False)
    tmp.close()
    with open(tmp.name, 'w') as f:
        f.write('{"probe": {"snmp": {"host": "10.0.0.9"}, '
                '"cli": {"command": "ping -c 1 {dst}"}}}')
    env_config._config = env_config.EnvConfig.load(tmp.name)
    try:
        drivers = {d['name']: d for d in probe_drivers.list_drivers()}
        check('snmp 按真实配置判为可用', drivers['snmp']['available'] is True,
              str(drivers['snmp']))
        check('cli 按真实配置判为可用', drivers['cli']['available'] is True,
              str(drivers['cli']))
    finally:
        env_config._config = old
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
