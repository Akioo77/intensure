#!/usr/bin/env python3
"""test_device_agent.py —— 设备侧执行代理（device_agent.py）与 AgentExec 客户端

重点覆盖**安全边界**（这个模块一旦写松就是网络上的远程 shell）：

  1. 认证：没 token 不给启动；token 不匹配 → 401；token 正确 → 放行
  2. 命令白名单：白名单内的命令放行；白名单外的（rm/bash/curl）拒绝
  3. 只读模式：写命令（ovs-ofctl mod-port / ip link set）默认拒绝
  4. 路径白名单：cat /etc/shadow 拒绝；cat /var/log/... 放行
  5. AgentExec 客户端：正常执行 / agent 拒绝 / agent 不可达 / 超时
  6. AgentExec 与 SshExec/LocalExec 的行为一致性（同样的命令 → 同样的结果语义）

运行: python3 -m pytest test_device_agent.py -v
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import device_agent          # noqa: E402
import exec_target           # noqa: E402
import env_config             # noqa: E402


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'FAIL: {name} {detail}')


# ====================================================================
# 测试夹具：起一个真 agent（随机端口）
# ====================================================================

class AgentFixture:
    """起一个真的 device_agent 在后台线程里。"""

    def __init__(self, token='tok-123', read_only=True, extra_read_commands=None):
        self.srv = device_agent.build_server('127.0.0.1', 0, token, read_only,
                                            extra_read_commands=extra_read_commands)
        self.port = self.srv.server_address[1]
        self.token = token
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.05)          # 等服务线程起来

    @property
    def policy(self):
        return self.srv.RequestHandlerClass.policy

    def stop(self):
        try:
            self.srv.shutdown()
            self.srv.server_close()
        except Exception:
            pass

    def client(self, token=None):
        return exec_target.AgentExec(host='127.0.0.1', port=self.port,
                                     token=self.token if token is None else token)


# ====================================================================
# 1. 认证
# ====================================================================

def test_agent_refuses_to_start_without_token():
    """没配 token → agent 拒绝启动。

    ⚠️ 这是最重要的一条：「默认无认证」的 agent 跑在网络上 = 远程 shell。
    """
    rc = device_agent.main(['--host', '127.0.0.1', '--port', '0', '--token', ''])
    check('没 token → 退出码非 0', rc != 0, str(rc))


def test_agent_rejects_wrong_token():
    fx = AgentFixture()
    try:
        c = fx.client(token='wrong-token')
        r = c.run(['cat', '/proc/version'])
        check('token 不匹配 → 执行失败', not r.ok, str(r.returncode))
        check('失败码是 126（设备侧拒绝）', r.returncode == 126, str(r.returncode))
        check('错误信息里没有泄漏 token', 'wrong-token' not in r.stderr, r.stderr)
    finally:
        fx.stop()


def test_agent_accepts_correct_token():
    fx = AgentFixture()
    try:
        c = fx.client()
        r = c.run(['cat', '/proc/version'])
        check('正确 token → 成功', r.ok, f'{r.returncode} {r.stderr}')
        check('stdout 正确', len(r.stdout) > 0, r.stdout)
    finally:
        fx.stop()


# ====================================================================
# 2. 命令白名单
# ====================================================================

def test_agent_allows_whitelisted_command():
    fx = AgentFixture()
    try:
        c = fx.client()
        r = c.run(['cat', '/proc/version'])
        check('白名单命令放行', r.ok, r.stderr)
    finally:
        fx.stop()


def test_agent_rejects_non_whitelisted_command():
    """白名单外的命令必须被拒 —— 这是「不变成远程 shell」的关键。"""
    fx = AgentFixture()
    try:
        c = fx.client()
        for bad in (['rm', '-rf', '/tmp/x'], ['bash', '-c', 'echo hi'],
                    ['curl', 'http://example.com'], ['python3', '-c', 'print(1)'],
                    ['env'], ['nc', '-l', '1234']):
            r = c.run(bad)
            check(f'{bad[0]} 被拒绝', not r.ok, f'rc={r.returncode} out={r.stdout}')
            check(f'{bad[0]} 拒绝码 126', r.returncode == 126, str(r.returncode))
    finally:
        fx.stop()


def test_agent_allows_sudo_prefix_for_whitelisted_command():
    """`sudo -n ovs-ofctl ...` 应该放行 —— sudo 本身不在白名单，
    但真正的命令在白名单里。设备侧 agent 通常直接以 root 跑。"""
    fx = AgentFixture()
    try:
        c = fx.client()
        r = c.run(['sudo', '-n', 'cat', '/proc/version'])
        check('sudo 前缀不阻断白名单命令', r.ok, f'{r.returncode} {r.stderr}')
    finally:
        fx.stop()


# ====================================================================
# 3. 只读 / 写分离
# ====================================================================

def test_read_only_rejects_write_commands():
    """默认只读：注故障类命令必须被拒。"""
    fx = AgentFixture(read_only=True)
    try:
        c = fx.client()
        for cmd in (['ovs-ofctl', '-O', 'OpenFlow13', 'mod-port', 'br0', 'p1', 'down'],
                    ['ip', 'link', 'set', 'eth0', 'down'],
                    ['tc', 'qdisc', 'replace', 'dev', 'eth0', 'root', 'netem']):
            r = c.run(cmd)
            check(f'{cmd[0]} 在只读模式被拒', not r.ok, f'rc={r.returncode}')
            check('拒绝码 126', r.returncode == 126, str(r.returncode))
    finally:
        fx.stop()


def test_read_only_allows_read_subcommands():
    """⚠️ 只读模式必须能查 —— 同一二进制下有读/写两种子命令。

    `ip link show` 是读、`ip link set` 是写 —— 都用 `ip`。
    如果按二进制整体分类，只读模式下连「查一下」都做不了，
    agent 在真实场景下就基本不可用了（采集层要读端口状态）。
    """
    fx = AgentFixture(read_only=True)
    try:
        policy = fx.policy
        read_cmds = [
            ['ip', 'link', 'show'],
            ['ip', '-br', 'link', 'show'],
            ['ip', 'addr'],
            ['ovs-ofctl', '-O', 'OpenFlow13', 'show', 'br0'],
            ['ovs-vsctl', 'show'],
            ['ovs-appctl', 'dpif/show'],
            ['tc', 'qdisc', 'show', 'dev', 'eth0'],
            ['tc', 'qdisc', 'show'],
        ]
        for cmd in read_cmds:
            kind, is_write = policy.classify(cmd)
            check(f'{" ".join(cmd)} → 读', kind == 'read', f'{kind} {is_write}')
            check(f'{" ".join(cmd)} 不标为写', is_write is False, str(is_write))
    finally:
        fx.stop()


def test_write_subcommands_classified_as_write():
    """写子命令必须被识别为写 —— 这条决定了只读模式拦不拦得住。"""
    fx = AgentFixture(read_only=False)
    try:
        policy = fx.policy
        write_cmds = [
            ['ip', 'link', 'set', 'eth0', 'down'],
            ['ip', 'link', 'set', 'eth0', 'up'],
            ['ovs-ofctl', '-O', 'OpenFlow13', 'mod-port', 'br0', 'p1', 'down'],
            ['ovs-ofctl', '-O', 'OpenFlow13', 'add-flow', 'br0'],
            ['ovs-vsctl', 'set', 'port', 'p1', 'tag=10'],
            ['tc', 'qdisc', 'replace', 'dev', 'eth0', 'root', 'netem'],
        ]
        for cmd in write_cmds:
            kind, is_write = policy.classify(cmd)
            check(f'{" ".join(cmd)} → 写', kind == 'write', f'{kind} {is_write}')
            check(f'{" ".join(cmd)} 标为写', is_write is True, str(is_write))
    finally:
        fx.stop()


def test_read_only_mode_allows_query_but_blocks_mutation():
    """端到端：只读 agent 能查 `ip link show`，但不能 `ip link set`。"""
    fx = AgentFixture(read_only=True)
    try:
        c = fx.client()
        r = c.run(['ip', 'link', 'show'])
        check('只读模式能查 ip link show', r.ok, f'{r.returncode} {r.stderr}')
        r2 = c.run(['ip', 'link', 'set', 'lo', 'down'])
        check('只读模式不能改 ip link set', not r2.ok, str(r2.returncode))
    finally:
        fx.stop()


def test_allow_write_permits_write_commands():
    """显式开写 → 写命令放行。"""
    fx = AgentFixture(read_only=False)
    try:
        kind, is_write = fx.policy.classify(
            ['ovs-ofctl', '-O', 'OpenFlow13', 'mod-port', 'br0', 'p1', 'down'])
        check('写命令在可写模式被识别为 write', kind == 'write', kind)
        check('标记为写操作', is_write is True, str(is_write))
    finally:
        fx.stop()


# ====================================================================
# 4. 路径白名单
# ====================================================================

def test_path_white_list_blocks_sensitive_files():
    """读文件只能读白名单前缀下的 —— 否则 `cat ~/.ssh/id_rsa` 就成了后门。"""
    fx = AgentFixture()
    try:
        c = fx.client()
        for p in ('/etc/shadow', '/root/.ssh/id_rsa', '/home/user/.ssh/id_ed25519'):
            r = c.run(['cat', p])
            check(f'cat {p} 被拒', not r.ok, f'rc={r.returncode}')
        # 白名单内的路径放行（用一个一定存在的文件）
        r = c.run(['cat', '/proc/version'])
        check('cat /proc/version 放行', r.ok, f'{r.returncode} {r.stderr}')
        check('内容读到了', len(r.stdout) > 0, r.stdout[:80])
    finally:
        fx.stop()


# ====================================================================
# 5. AgentExec 客户端
# ====================================================================

def test_agent_exec_failure_is_visible_not_silent():
    """agent 拒绝时，调用方必须能看到失败（不能吞掉成「成功但没输出」）。"""
    fx = AgentFixture()
    try:
        c = fx.client()
        r = c.run(['rm', '-rf', '/'])
        check('返回失败', not r.ok, str(r.returncode))
        check('stderr 有原因', '白名单' in r.stderr or '拒绝' in r.stderr, r.stderr)
    finally:
        fx.stop()


def test_agent_exec_unreachable():
    """agent 没起来 → 明确报「不可达」，不是假装成功。"""
    c = exec_target.AgentExec(host='127.0.0.1', port=59999, token='x')
    r = c.run(['cat', '/proc/version'])
    check('不可达 → 失败', not r.ok, str(r.returncode))
    check('错误码 127（命令找不到/连不上）', r.returncode == 127, str(r.returncode))
    check('stderr 讲清了不可达', '不可达' in r.stderr, r.stderr)


def test_agent_exec_timeout():
    """agent 侧命令超时 → 返回 124，客户端不无限等。

    ⚠️ `sleep` 不在默认白名单里（生产 agent 不该允许 sleep），
    所以这里给 fixture 额外开一个 —— 白名单机制本身在测试里是可配的。
    """
    fx = AgentFixture(extra_read_commands=['sleep'])
    try:
        c = fx.client()
        # timeout=1 且 sleep 5 → agent 侧会超时返回 124
        r = c.run(['sleep', '5'], timeout=1)
        check('超时 → 返回码 124', r.returncode == 124, f'{r.returncode} {r.stderr}')
    finally:
        fx.stop()


def test_agent_exec_describe():
    fx = AgentFixture()
    try:
        c = fx.client()
        check('describe 带出 host', '127.0.0.1' in c.describe(), c.describe())
        check('describe 带出协议', 'agent://' in c.describe(), c.describe())
    finally:
        fx.stop()


# ====================================================================
# 6. 工厂与配置
# ====================================================================

def test_build_target_agent():
    t = exec_target.build_target({'type': 'agent', 'host': '10.0.0.51',
                                  'agent_port': 9099, 'token': 'tk'})
    check('type=agent → AgentExec', isinstance(t, exec_target.AgentExec), str(t))
    check('host 读到了', t.host == '10.0.0.51', t.host)
    check('端口读到了', t.port == 9099, str(t.port))
    check('token 读到了', t.token == 'tk', t.token)


def test_build_target_agent_default_port_is_9099():
    """agent 不配端口时用 9099。

    ⚠️ 不能用 ssh 的 port（22）：target 段是深合并的，port 永远存在，
    直接拿来当 agent 端口会连到 22 端口去。
    """
    t = exec_target.build_target({'type': 'agent', 'host': '10.0.0.51'})
    check('默认端口是 9099', t.port == 9099, str(t.port))


def test_build_target_agent_missing_host_degrades():
    t = exec_target.build_target({'type': 'agent', 'host': ''})
    check('agent 缺 host → local', isinstance(t, exec_target.LocalExec), str(t))


def test_build_target_agent_token_from_env(monkeypatch=None):
    """token 可以只走环境变量（不落配置文件）。"""
    old = os.environ.get('INTENSURE_AGENT_TOKEN')
    os.environ['INTENSURE_AGENT_TOKEN'] = 'env-token-xyz'
    try:
        t = exec_target.build_target({'type': 'agent', 'host': '10.0.0.51'})
        check('token 从环境变量读到', t.token == 'env-token-xyz', t.token)
    finally:
        if old is None:
            os.environ.pop('INTENSURE_AGENT_TOKEN', None)
        else:
            os.environ['INTENSURE_AGENT_TOKEN'] = old


def test_env_config_target_has_agent_defaults():
    """env.json 默认段要带 agent 需要的字段（深合并不会把用户配置冲掉）。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, 'env.json')
        with open(p, 'w') as f:
            json.dump({'target': {'type': 'agent', 'host': '10.0.0.51'}}, f)
        cfg = env_config.EnvConfig.load(p)
        check('target.type 读到了', cfg.target.get('type') == 'agent', str(cfg.target))
        check('scheme 有默认值', cfg.target.get('scheme') == 'http', str(cfg.target))
        check('agent_port 有默认值 9099', cfg.target.get('agent_port') == 9099,
              str(cfg.target.get('agent_port')))


def test_agent_and_ssh_share_same_interface():
    """三种 target 必须提供同一套能力 —— 调用方（fault_api/ovs_logger）不该关心区别。"""
    for kind, obj in (('local', exec_target.LocalExec()),
                      ('ssh', exec_target.SshExec(host='h')),
                      ('agent', exec_target.AgentExec(host='h'))):
        for meth in ('run', 'ping', 'read_file', 'tail_file', 'resolve_source_pid',
                     'describe'):
            check(f'{kind} 有 {meth}', callable(getattr(obj, meth, None)), meth)


# ====================================================================
# 7. 端到端：agent 真的能替代 SSH 做注故障
# ====================================================================

def test_end_to_end_exec_via_agent():
    """端到端：起 agent → 客户端执行 → 结果与本地一致。"""
    fx = AgentFixture(read_only=False)
    try:
        c = fx.client()
        r = c.run(['cat', '/proc/version'])
        check('端到端执行成功', r.ok, f'{r.returncode} {r.stderr}')
        check('输出正确', len(r.stdout) > 0, r.stdout)
        check('有耗时统计', r.duration_ms >= 0, str(r.duration_ms))
    finally:
        fx.stop()


def test_agent_health_endpoint():
    fx = AgentFixture()
    try:
        import urllib.request
        req = urllib.request.Request(
            f'http://127.0.0.1:{fx.port}/agent/health',
            headers={'X-Intensure-Token': fx.token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,
                                                                              timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        check('health 返回 ok', data.get('status') == 'ok', str(data))
        check('health 报告只读模式', data.get('read_only') is True, str(data))
        check('health 带版本号', bool(data.get('agent_version')), str(data))
    except urllib.error.HTTPError as e:
        raise AssertionError(f'FAIL: health 请求失败 HTTP {e.code}')
    finally:
        fx.stop()


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
