#!/usr/bin/env python3
"""test_env_config.py — 环境配置 / 能力自检 / 只读守卫（v3.21.0）

覆盖场景：
  1. 默认配置加载（没有配置文件时用内置默认值 = 改造前行为）
  2. JSON 配置文件加载 / 注释行被忽略
  3. YAML 配置加载（装了 PyYAML 才测）
  4. 配置文件坏了 → 回退默认值，不让服务起不来
  5. $INTENSURE_ENV_FILE 覆盖路径
  6. 只读模式 → 写操作被禁
  7. allow_fault_injection=false → 写操作被禁
  8. dpid_to_switch / port_label_map / probe 读取
  9. capabilities 内容（交换机数、拓扑发现方式、契约版本）
  10. GET /api/capabilities 端点
  11. 只读模式下 POST /api/v1/test/inject-fault → 403（且没真的注故障）
  12. state_model 的校验基线可注入
  13. actual_state_builder 的 dpid 映射可从配置来

运行: python3 -m pytest test_env_config.py -v
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capabilities  # noqa: E402
import env_config  # noqa: E402

try:
    import flask  # noqa: F401
except ImportError:
    flask = None

if flask is not None:
    import state_server
else:
    state_server = None

try:
    import yaml  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def check(name, condition, detail=""):
    """断言。必须真的 raise, 否则 pytest 会把失败当通过（假绿）。"""
    if not condition:
        raise AssertionError(f'{name}  {detail}')


def write_tmp(content, suffix='.json'):
    """写临时配置文件，返回路径。"""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(content)
    return path


# ============ 默认值 ============

def test_default_config():
    """没有配置文件 → 内置默认值，且与改造前的硬编码一致。"""
    cfg = env_config.EnvConfig.load('/nonexistent/path/env.json')
    check('默认环境名', cfg.environment == 'mininet-lab', cfg.environment)
    check('默认模式 active', cfg.mode == 'active', cfg.mode)
    check('默认不是只读', cfg.is_read_only is False)
    check('默认允许写操作', cfg.write_operations_allowed is True)
    check('默认允许注故障', cfg.allow_fault_injection is True)

    # 拓扑期望：字段必须齐全（state_model.validate_completeness 直接索引这些键）
    expect = cfg.topology_expect
    for key in ('switches', 'hosts', 'inter_switch_links', 'host_switch_links',
                'total_links', 'expected_host_ips'):
        check(f'拓扑期望含 {key}', key in expect, str(expect))

    # 默认拓扑 = start_all.sh 起的 SingleSwitchTopo(k=4)：1 交换机 4 主机
    check('默认 1 台交换机', expect['switches'] == 1, str(expect))
    check('默认 4 台主机', expect['hosts'] == 4, str(expect))
    check('1 交换机环境没有交换机间链路', expect['inter_switch_links'] == 0,
          str(expect))
    check('默认探测驱动 mininet', cfg.probe_driver == 'mininet', cfg.probe_driver)

    # 仓库里自带的 env.json 必须和内置默认值一致（否则体检会误报）
    import os.path
    repo_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'env.json')
    if os.path.isfile(repo_env):
        shipped = env_config.EnvConfig.load(repo_env)
        check('仓库 env.json 与内置默认一致',
              shipped.topology_expect == expect, str(shipped.topology_expect))
        check('仓库 env.json 环境名一致',
              shipped.environment == cfg.environment, shipped.environment)


def test_default_config_matches_current_behaviour():
    """默认配置下，ActualStateBuilder 的行为跟改造前一样（末位推断）。"""
    from actual_state_builder import ActualStateBuilder
    builder = ActualStateBuilder({'0000000000000001': 'sw1'})
    check('显式映射仍然优先', builder.dpid_to_switch == {'0000000000000001': 'sw1'})

    builder2 = ActualStateBuilder()
    check('无映射时为空（走末位推断）', builder2.dpid_to_switch == {},
          str(builder2.dpid_to_switch))


# ============ 文件加载 ============

def test_json_config_loading():
    path = write_tmp(json.dumps({
        'environment': 'campus-lab',
        'mode': 'read-only',
        'topology_expect': {'switches': 12, 'hosts': 40, 'total_links': 60,
                            'expected_host_ips': ['10.10.0.1']},
        'dpid_to_switch': {'00000000000000aa': 'sw-core-1'},
        'port_label_map': {'GigabitEthernet0/1': 'eth1'},
        'probe': {'driver': 'icmp', 'source': '10.10.0.5', 'destination': '10.10.0.9'},
        'allow_fault_injection': False,
    }))
    try:
        cfg = env_config.EnvConfig.load(path)
        check('环境名读到了', cfg.environment == 'campus-lab', cfg.environment)
        check('只读模式读到了', cfg.is_read_only is True)
        check('只读 → 禁写操作', cfg.write_operations_allowed is False)
        check('allow_fault_injection=false → 禁写',
              cfg.allow_fault_injection is False)
        check('拓扑期望读到了', cfg.topology_expect['switches'] == 12)
        check('dpid 映射读到了',
              cfg.dpid_to_switch['00000000000000aa'] == 'sw-core-1')
        check('端口名映射读到了',
              cfg.port_label_map['GigabitEthernet0/1'] == 'eth1')
        check('探测驱动读到了', cfg.probe_driver == 'icmp', cfg.probe_driver)
        check('source 是文件路径', cfg.source == path, cfg.source)
    finally:
        os.unlink(path)


def test_json_comments_ignored():
    """手写配置常带 // 注释，要能解析。"""
    path = write_tmp('''{
      // 这是注释
      "environment": "lab-x",   // 行尾注释
      "mode": "active"
    }''')
    try:
        cfg = env_config.EnvConfig.load(path)
        check('注释被忽略，值正常读出', cfg.environment == 'lab-x', cfg.environment)
    except Exception as exc:
        raise AssertionError(f'带注释的 JSON 解析失败: {exc}')
    finally:
        os.unlink(path)


def test_yaml_config_loading():
    if not HAS_YAML:
        return  # 没装 PyYAML 就跳过（CI 装不上 PyYAML，属于预期）
    path = write_tmp('''environment: yaml-lab
mode: read-only
probe:
  driver: icmp
''', suffix='.yaml')
    try:
        cfg = env_config.EnvConfig.load(path)
        check('YAML 环境名读到了', cfg.environment == 'yaml-lab', cfg.environment)
        check('YAML 只读读到了', cfg.is_read_only is True)
        check('YAML 嵌套读到了', cfg.probe_driver == 'icmp', cfg.probe_driver)
    finally:
        os.unlink(path)


def test_broken_config_falls_back():
    """配置写坏了 → 用默认值 + 强制只读，且不能回退到别的文件。

    安全要点：用户本想开只读（接真实网络），配置一个笔误不能让它变回可写。
    """
    path = write_tmp('{ this is not valid json ')
    try:
        cfg = env_config.EnvConfig.load(path)
        check('回退到默认值', cfg.environment == 'mininet-lab', cfg.environment)
        check('source 标出是坏文件',
              cfg.source.startswith('broken:'), cfg.source)
        check('坏配置 → 强制只读', cfg.is_read_only is True, cfg.mode)
        check('坏配置 → 禁写操作',
              cfg.write_operations_allowed is False)
        check('坏配置 → 禁注故障', cfg.allow_fault_injection is False)
    finally:
        os.unlink(path)


def test_env_var_overrides_path():
    path = write_tmp(json.dumps({'environment': 'from-envvar'}))
    old = os.environ.get(env_config.CONFIG_ENV_VAR)
    os.environ[env_config.CONFIG_ENV_VAR] = path
    env_config.reset_cache()
    try:
        cfg = env_config.get_config()
        check('环境变量指定的文件生效', cfg.environment == 'from-envvar',
              cfg.environment)
    finally:
        if old is None:
            os.environ.pop(env_config.CONFIG_ENV_VAR, None)
        else:
            os.environ[env_config.CONFIG_ENV_VAR] = old
        env_config.reset_cache()
        os.unlink(path)


# ============ capabilities ============

SAMPLE_STATE = {
    'meta': {'update_count': 5},
    'switches': {
        '0000000000000001': {'ports': [{'port_no': 1, 'name': 's1-eth1', 'state': 0}]},
        '0000000000000002': {'ports': [{'port_no': 1, 'name': 's2-eth1', 'state': 0}]},
    },
    'hosts': {'aa:bb': {'ipv4': ['10.0.0.1']}, 'cc:dd': {'ipv4': ['10.0.0.2']}},
    'links': {'0000000000000001:3': {'peer_dpid': '0000000000000002',
                                     'peer_port': 3, 'last_seen': 1.0}},
    'flows': {},
}


def test_capabilities_content():
    cfg = env_config.EnvConfig.load('/nonexistent')
    caps = capabilities.build_capabilities(SAMPLE_STATE, cfg)
    check('有 environment', caps['environment'] == 'mininet-lab')
    check('有 mode', caps['mode'] == 'active')
    check('交换机数对', caps['openflow']['switches_connected'] == 2,
          str(caps['openflow']))
    check('OpenFlow 写操作标注为 none',
          caps['openflow']['write_operations'] == 'none')
    check('拓扑发现是 lldp', caps['topology_discovery']['method'] == 'lldp',
          str(caps['topology_discovery']))
    check('主机数对', caps['hosts']['discovered'] == 2)
    check('探测驱动对', caps['probe']['driver'] == 'mininet')
    check('故障注入可用', caps['fault_injection']['available'] is True)
    check('契约路径版本有值', caps['contract']['path_version'] == '1.0')
    check('列出了契约端点', len(caps['contract']['endpoints']) == 5)
    check('ready 为 True', caps['ready'] is True)


def test_capabilities_degraded_topology():
    """没有链路 → 拓扑发现标记为 degraded（真实设备不开 LLDP 时会这样）。"""
    cfg = env_config.EnvConfig.load('/nonexistent')
    state = dict(SAMPLE_STATE)
    state['links'] = {}
    caps = capabilities.build_capabilities(state, cfg)
    check('退化标记正确', caps['topology_discovery']['method'] == 'degraded',
          str(caps['topology_discovery']))
    check('退化时链路数为 0', caps['topology_discovery']['links_discovered'] == 0)


def test_capabilities_read_only():
    cfg = env_config.EnvConfig({'mode': 'read-only'}, source='test')
    caps = capabilities.build_capabilities(SAMPLE_STATE, cfg)
    check('只读标志', caps['read_only'] is True)
    check('写操作禁用', caps['write_operations'] is False)
    check('故障注入不可用', caps['fault_injection']['available'] is False)
    check('说明了原因', '只读' in caps['fault_injection']['reason'],
          caps['fault_injection']['reason'])


def test_capabilities_fault_api_unavailable():
    cfg = env_config.EnvConfig.load('/nonexistent')
    caps = capabilities.build_capabilities(SAMPLE_STATE, cfg,
                                            fault_api_available=False)
    check('模块不可用时故障注入不可用',
          caps['fault_injection']['available'] is False)


# ============ HTTP 端点 ============

def test_capabilities_endpoint():
    if flask is None:
        return
    client = state_server.app.test_client()
    resp = client.get('/api/capabilities')
    check('返回 200', resp.status_code == 200, str(resp.status_code))
    data = resp.get_json()
    for field in ('environment', 'mode', 'openflow', 'probe', 'contract', 'ready'):
        check(f'含字段 {field}', field in data, str(list(data.keys())))


def test_read_only_blocks_fault_injection():
    """只读模式下注故障端点必须 403，且不能真的注进去。"""
    if flask is None:
        return
    path = write_tmp(json.dumps({'mode': 'read-only',
                                  'allow_fault_injection': False}))
    old = os.environ.get(env_config.CONFIG_ENV_VAR)
    os.environ[env_config.CONFIG_ENV_VAR] = path
    env_config.reset_cache()
    try:
        client = state_server.app.test_client()
        resp = client.post('/api/v1/test/inject-fault',
                           json={'fault_type': 'link_down', 'target': 's1-eth1'})
        check('只读模式返回 403', resp.status_code == 403, str(resp.status_code))
        data = resp.get_json()
        check('错误体里有 mode', data.get('mode') == 'read-only', str(data))

        # capabilities 也要如实反映
        caps = client.get('/api/capabilities').get_json()
        check('capabilities 显示只读', caps['read_only'] is True)
        check('capabilities 显示禁写', caps['write_operations'] is False)
    finally:
        if old is None:
            os.environ.pop(env_config.CONFIG_ENV_VAR, None)
        else:
            os.environ[env_config.CONFIG_ENV_VAR] = old
        env_config.reset_cache()
        os.unlink(path)


# ============ 校验基线注入 ============

def test_topology_expect_injection():
    """换环境时，校验基线从配置来。"""
    import state_model
    state = {
        'meta': {}, 'switches': {'0000000000000001': {'ports': []}},
        'hosts': {}, 'port_stats': {}, 'flows': {}, 'events': [], 'links': {},
    }
    # 用默认期望 → 交换机少了会 warning
    report = state_model.validate_state(state)
    completeness = [i for i in report['issues'] if i['category'] == 'completeness']
    check('默认基线报缺交换机',
          any('switches' in i['message'] for i in completeness), str(completeness))

    # 用自定义期望（只要 1 台交换机）→ 不再报
    report2 = state_model.validate_state(state, topology_expect={
        'switches': 1, 'hosts': 0, 'inter_switch_links': 0, 'host_switch_links': 0,
        'total_links': 0, 'expected_host_ips': []})
    completeness2 = [i for i in report2['issues'] if i['category'] == 'completeness']
    check('注入基线后不再报缺交换机',
          not any('switches' in i['message'] for i in completeness2),
          str(completeness2))


def test_dpid_mapping_from_config():
    """dpid_to_switch 从配置来 → 设备名不再靠末位推断。"""
    path = write_tmp(json.dumps({
        'dpid_to_switch': {'00000000000000aa': 'sw-core-1'}}))
    old = os.environ.get(env_config.CONFIG_ENV_VAR)
    os.environ[env_config.CONFIG_ENV_VAR] = path
    env_config.reset_cache()
    try:
        from actual_state_builder import ActualStateBuilder
        builder = ActualStateBuilder()
        check('映射从配置读到了',
              builder.dpid_to_switch.get('00000000000000aa') == 'sw-core-1',
              str(builder.dpid_to_switch))
    finally:
        if old is None:
            os.environ.pop(env_config.CONFIG_ENV_VAR, None)
        else:
            os.environ[env_config.CONFIG_ENV_VAR] = old
        env_config.reset_cache()
        os.unlink(path)


if __name__ == '__main__':
    import pytest
    sys.exit(pytest.main([__file__, '-v']))
