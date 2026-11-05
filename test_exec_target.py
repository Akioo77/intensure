#!/usr/bin/env python3
"""test_exec_target.py — 接入适配层（exec_target）

覆盖场景：
  1. build_target：local / ssh / 未知类型 / ssh 缺 host 的降级
  2. LocalExec.run：成功 / 失败 / 超时 / 命令不存在
  3. LocalExec.ping：丢包率与 RTT 解析、源主机 PID 解析
  4. SshExec：argv 拼装、参数转义、远端命令串
  5. resolve_source_pid：pgrep 输出解析 / 找不到
  6. CommandResult.ok / output
  7. env_config：target / state_file / probe.source / probe.destination
  8. fault_api 走适配层：命令确实发给了 target（不是本机 subprocess）

运行: python3 -m pytest test_exec_target.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import exec_target  # noqa: E402
import env_config  # noqa: E402


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'FAIL: {name} {detail}')


# ====================================================================
# 1. build_target
# ====================================================================

def test_build_target_local():
    t = exec_target.build_target({'type': 'local'})
    check('type=local → LocalExec', isinstance(t, exec_target.LocalExec), str(t))
    check('本地目标的 describe 是 local', t.describe() == 'local', t.describe())


def test_build_target_ssh():
    t = exec_target.build_target({'type': 'ssh', 'host': '10.0.0.9', 'user': 'partner',
                                  'port': 2222, 'key': '~/.ssh/id_ed25519'})
    check('type=ssh → SshExec', isinstance(t, exec_target.SshExec), str(t))
    check('host 读到了', t.host == '10.0.0.9', t.host)
    check('user 读到了', t.user == 'partner', t.user)
    check('端口读到了', t.port == 2222, str(t.port))
    check('私钥路径展开了 ~', not t.key.startswith('~'), t.key)
    check('describe 带出目标', '10.0.0.9' in t.describe(), t.describe())


def test_build_target_degrades():
    """ssh 缺 host / 类型不认识 → 退回 local（不能因为配置问题让服务起不来）。"""
    t = exec_target.build_target({'type': 'ssh', 'host': ''})
    check('ssh 缺 host → local', isinstance(t, exec_target.LocalExec), str(t))
    t2 = exec_target.build_target({'type': 'whatever'})
    check('未知类型 → local', isinstance(t2, exec_target.LocalExec), str(t2))
    t3 = exec_target.build_target(None)
    check('空配置 → local', isinstance(t3, exec_target.LocalExec), str(t3))


# ====================================================================
# 2. LocalExec.run
# ====================================================================

def test_local_run_ok():
    t = exec_target.LocalExec()
    r = t.run(['echo', 'hello'])
    check('成功时 rc=0', r.ok, str(r.returncode))
    check('stdout 拿到了', 'hello' in r.stdout, r.stdout)
    check('output 合并 stdout+stderr', 'hello' in r.output, r.output)


def test_local_run_failure():
    t = exec_target.LocalExec()
    r = t.run(['false'])
    check('失败时 rc≠0', not r.ok, str(r.returncode))
    check('失败时 stderr 非空', r.stderr != '' or r.returncode != 0, r.stderr)


def test_local_run_missing_binary():
    t = exec_target.LocalExec()
    r = t.run(['__intensure_no_such_binary__'])
    check('命令不存在 → rc=127', r.returncode == 127, str(r.returncode))


def test_local_run_timeout():
    t = exec_target.LocalExec()
    r = t.run(['sleep', '5'], timeout=0.3)
    check('超时 → rc=124', r.returncode == 124, str(r.returncode))
    check('超时信息里带命令', 'sleep' in r.stderr, r.stderr)


# ====================================================================
# 3. ping 解析
# ====================================================================

class _FakeTarget(exec_target.ExecTarget):
    """假目标：按预设脚本返回输出，并记录收到的命令。"""

    kind = 'fake'

    def __init__(self, outputs=None, default=None):
        self.outputs = outputs or {}
        self.default = default
        self.calls = []

    def run(self, argv, timeout=exec_target.DEFAULT_TIMEOUT):
        self.calls.append(list(argv))
        key = ' '.join(argv)
        for pattern, out in self.outputs.items():
            if pattern in key:
                rc, stdout, stderr = out
                return exec_target.CommandResult(returncode=rc, stdout=stdout, stderr=stderr)
        if self.default is not None:
            rc, stdout, stderr = self.default
            return exec_target.CommandResult(returncode=rc, stdout=stdout, stderr=stderr)
        return exec_target.CommandResult(returncode=1, stdout='', stderr='no match')


def test_ping_parses_loss_and_rtt():
    out = ('3 packets transmitted, 3 received, 0% packet loss, time 2002ms\n'
           'rtt min/avg/max/mdev = 0.045/0.052/0.061/0.007 ms\n')
    t = _FakeTarget(default=(0, out, ''))
    loss, latency = t.ping('', '10.0.0.4')
    check('丢包率解析成 0', loss == 0.0, str(loss))
    check('RTT 取的是 avg（0.052），不是 min（0.045）',
          abs(latency - 0.052) < 1e-6, str(latency))


def test_parse_ping_output_takes_avg_not_min():
    r"""旧正则 `avg[^=]*= ([\d.]+)` 匹配到的是 min —— 口径必须是 avg。"""
    out = 'rtt min/avg/max/mdev = 0.045/0.052/0.061/0.007 ms\n'
    _, latency = exec_target.parse_ping_output(out)
    check('取 avg', abs(latency - 0.052) < 1e-6, str(latency))
    out2 = '3 packets transmitted, 1 received, 66% packet loss, time 2002ms\n'
    loss, lat2 = exec_target.parse_ping_output(out2)
    check('丢包率 66', abs(loss - 66.0) < 1e-6, str(loss))
    check('没有 RTT 段 → None', lat2 is None, str(lat2))


def test_ping_full_loss():
    out = '3 packets transmitted, 0 received, 100% packet loss, time 2002ms\n'
    t = _FakeTarget(default=(1, out, ''))
    loss, latency = t.ping('', '10.0.0.4')
    check('全丢包 = 100', loss == 100.0, str(loss))
    check('没 RTT 时 latency=None', latency is None, str(latency))


def test_ping_uses_mnexec_when_source_resolvable():
    """源主机能定位到命名空间 PID → 用 mnexec 进那个命名空间 ping。"""
    t = _FakeTarget(
        outputs={'pgrep -f mininet:h1': (0, '26024\n', '')},
        default=(0, '0% packet loss\n', ''))
    loss, _ = t.ping('h1', '10.0.0.4')
    check('走了 mnexec', any('mnexec' in ' '.join(c) for c in t.calls),
          str(t.calls))
    check('mnexec 带上了 PID', any('26024' in ' '.join(c) for c in t.calls),
          str(t.calls))
    check('丢包率 0', loss == 0.0, str(loss))


def test_ping_without_source_uses_plain_ping():
    t = _FakeTarget(default=(0, '0% packet loss\n', ''))
    t.ping('', '10.0.0.4')
    check('没有源 → 直接 ping', any(c[0] == 'ping' for c in t.calls), str(t.calls))


# ====================================================================
# 4. SshExec
# ====================================================================

def test_ssh_argv_shape():
    t = exec_target.SshExec(host='10.0.0.9', user='partner', port=2222,
                             key='/tmp/k', connect_timeout=7)
    argv = t._ssh_argv('echo hi')
    check('用 ssh 启动', argv[0] == 'ssh', argv[0])
    check('禁交互式密码', 'BatchMode=yes' in argv, str(argv))
    check('带连接超时', any('ConnectTimeout=7' in a for a in argv), str(argv))
    check('带私钥', '/tmp/k' in argv, str(argv))
    check('带端口', '2222' in argv, str(argv))
    check('目标是 user@host', 'partner@10.0.0.9' in argv, str(argv))
    check('远端命令在最后', argv[-1] == 'echo hi', str(argv))


def test_ssh_quotes_arguments():
    """参数要正确转义 —— 否则带空格/分号的参数会被远端 shell 拆错。"""
    import shlex
    t = exec_target.SshExec(host='h', user='u')
    args = ['tc', 'qdisc', 'replace', 'dev', 's1 eth1; rm -rf /']
    remote = ' '.join(shlex.quote(a) for a in args)
    argv = t._ssh_argv(remote)
    check('危险参数被引起来', "'s1 eth1; rm -rf /'" in argv[-1], argv[-1])
    check('远端 shell 解析回来还是原参数（转义无损）',
          shlex.split(argv[-1]) == args, argv[-1])


def test_ssh_run_uses_target_host(monkeypatch=None):
    """SshExec.run 真的把命令发到目标主机（用假 subprocess 验证）。"""
    t = exec_target.SshExec(host='10.0.0.9', user='partner')
    captured = {}

    class _FakeProc:
        returncode = 0
        stdout = 'ok\n'
        stderr = ''

    def _fake_run(argv, **kwargs):
        captured['argv'] = argv
        return _FakeProc()

    orig = exec_target.subprocess.run
    exec_target.subprocess.run = _fake_run
    try:
        r = t.run(['sudo', '-n', 'tc', 'qdisc', 'show'])
    finally:
        exec_target.subprocess.run = orig
    check('远端命令拼进了一个串', 'sudo -n tc qdisc show' in captured['argv'][-1],
          str(captured['argv']))
    check('结果读回来了', 'ok' in r.stdout, r.stdout)


# ====================================================================
# 5. resolve_source_pid
# ====================================================================

def test_resolve_source_pid_parses_pgrep():
    t = _FakeTarget(outputs={'pgrep': (0, '26024\n26025\n', '')})
    pid = t.resolve_source_pid('h1')
    check('取第一个 PID', pid == 26024, str(pid))


def test_resolve_source_pid_pattern_is_anchored():
    """pgrep 模式必须锚定结尾 —— 否则会把执行命令的 shell 自己匹配上。

    踩过：宽松的 `pgrep -f mininet:h1` 在跨机场景下匹配到了 ssh 远端 shell
    （它的命令行里就含 "mininet:h1"）→ 拿到错 PID → mnexec 进错命名空间
    → ping 永远 100% 丢包。
    """
    t = _FakeTarget(default=(1, '', ''))
    t.resolve_source_pid('h1')
    joined = ' '.join(t.calls[0]) if t.calls else ''
    check('pgrep 模式锚定结尾', 'mininet:h1$' in joined, joined)


def test_ping_retries_with_sudo_on_permission_denied():
    """对方 Mininet 是 root 起的 → mnexec 进命名空间要 sudo，失败后自动补。"""
    out = '2 packets transmitted, 2 received, 0% packet loss, time 1004ms\n'
    t = _FakeTarget(outputs={'pgrep': (0, '33536\n', '')},
                    default=(0, out, ''))
    # 第一次 mnexec 报 Permission denied，第二次带 sudo 才成功
    calls = {'n': 0}

    class _PermDeniedOnce(_FakeTarget):
        def run(self, argv, timeout=exec_target.DEFAULT_TIMEOUT):
            self.calls.append(list(argv))
            joined = ' '.join(argv)
            if 'pgrep' in joined:
                return exec_target.CommandResult(returncode=0, stdout='33536\n')
            if 'sudo' not in joined:
                return exec_target.CommandResult(
                    returncode=1, stdout='',
                    stderr='/proc/33536/ns/net: Permission denied')
            return exec_target.CommandResult(returncode=0, stdout=out, stderr='')

    t2 = _PermDeniedOnce()
    loss, latency = t2.ping('h1', '10.0.0.4', count=2, timeout=8)
    check('第一次没用 sudo', any('mnexec' in ' '.join(c) and 'sudo' not in ' '.join(c)
                                 for c in t2.calls), str(t2.calls))
    check('第二次补了 sudo', any('sudo -n mnexec' in ' '.join(c) for c in t2.calls),
          str(t2.calls))
    check('最终拿到丢包率', loss == 0.0, str(loss))


def test_resolve_source_pid_not_found():
    t = _FakeTarget(default=(1, '', 'no match'))
    check('找不到返回 None', t.resolve_source_pid('h9') is None)


def test_resolve_source_pid_accepts_int():
    t = _FakeTarget(default=(1, '', ''))
    check('已经是 PID 就直接用', t.resolve_source_pid(1234) == 1234)


# ====================================================================
# 6. CommandResult
# ====================================================================

def test_command_result():
    r = exec_target.CommandResult(returncode=0, stdout='a', stderr='b')
    check('ok 跟着 rc', r.ok, str(r.returncode))
    check('output 合并', r.output == 'ab', r.output)
    r2 = exec_target.CommandResult(returncode=1)
    check('rc≠0 → not ok', not r2.ok, str(r2.returncode))


# ====================================================================
# 7. env_config 新配置项
# ====================================================================

def test_env_config_target_and_state_file():
    cfg = env_config.EnvConfig.load()
    check('target 默认 local', cfg.target.get('type') == 'local', str(cfg.target))
    check('target 有 host 字段', 'host' in cfg.target, str(cfg.target))
    check('state_file 默认值', cfg.state_file == '/tmp/network_state.json', cfg.state_file)
    check('probe_source 有默认值', cfg.probe_source == '10.0.0.1', cfg.probe_source)
    check('probe_destination 有默认值', cfg.probe_destination == '10.0.0.4',
          cfg.probe_destination)
    d = cfg.describe()
    check('describe 里有 target_type', 'target_type' in d, str(d))
    check('describe 里有 state_file', 'state_file' in d, str(d))


def test_env_config_repo_env_json_parses():
    """仓库里的 env.json 必须能解析，且带上新增的两段。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'env.json')
    cfg = env_config.EnvConfig.load(path)
    check('env.json 的 target 可读', cfg.target.get('type') == 'local', str(cfg.target))
    check('env.json 的 state_file 可读',
          cfg.state_file == '/tmp/network_state.json', cfg.state_file)


# ====================================================================
# 8. fault_api 确实走适配层（不是本机 subprocess）
# ====================================================================

def test_fault_api_uses_target():
    """注故障的命令必须发给 target —— 这样才能打到远端设备。"""
    import fault_api
    t = _FakeTarget(outputs={'ip -br link show': (
        0, 's1-eth1 UP\ns1-eth2 UP\ns1-eth3 UP\ns1-eth4 UP\n', '')},
        default=(0, 'ok\n', ''))
    api = fault_api.FaultApi(target=t)
    api.inject('link_down', 's1-eth1')
    joined = [' '.join(c) for c in t.calls]
    check('命令确实发给了 target', len(t.calls) > 0, str(t.calls))
    check('带上了 ip link set down', any('ip link set s1-eth1 down' in c for c in joined),
          str(joined))
    check('带上了 ovs-ofctl mod-port',
          any('ovs-ofctl' in c and 'mod-port' in c for c in joined), str(joined))
    check('命令带 sudo -n（非交互）', any('sudo -n' in c for c in joined), str(joined))


def test_fault_api_interface_listing_comes_from_target():
    """接口清单也必须来自 target（远端交换机上的端口列表）。"""
    import fault_api
    t = _FakeTarget(outputs={'ip -br link show': (0, 's9-eth7 UP\n', '')},
                    default=(0, 'ok\n', ''))
    api = fault_api.FaultApi(target=t)
    ports = api._all_switch_ports()
    check('端口清单来自 target', ports == {'s9': ['s9-eth7']}, str(ports))
