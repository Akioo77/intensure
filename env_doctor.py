#!/usr/bin/env python3
"""env_doctor.py — 环境体检（v3.21.0）

一条命令回答：**当前这个环境，我们这个采集层到底能不能正常干活？缺什么？**

为什么需要它
------------
最终验收环境还不确定（三个人可能各配各的 Mininet，也可能接真实 OpenFlow 网络）。
与其在赶演示的时候靠试错发现问题，不如接进去先跑一次体检。

用法:
    python3 env_doctor.py                      # 自动找状态（HTTP 服务 or 状态文件）
    python3 env_doctor.py --url http://127.0.0.1:8080
    python3 env_doctor.py --state-file /tmp/network_state.json
    python3 env_doctor.py --config env.json
    python3 env_doctor.py --json               # 机器可读输出

退出码:
    0 = 关键项全通过
    1 = 有失败项
    2 = 采集层没跑起来，没东西可体检
"""
import argparse
import json
import os
import shutil
import sys
import urllib.request
from importlib.util import find_spec

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capabilities  # noqa: E402
import env_config  # noqa: E402

DEFAULT_STATE_FILE = '/tmp/network_state.json'
DEFAULT_URL = 'http://127.0.0.1:8080'


def _opener():
    """绕开代理的 urllib opener。

    VM 的 /etc/environment 设了 http_proxy 但没设 no_proxy → 连 127.0.0.1
    都会被代理劫持（curl 那边 v3.19.0 已经加 --noproxy，Python 侧要同样处理，
    reporter.py 里也是这么做的）。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))

OK = '\033[32m✓\033[0m'
WARN = '\033[33m⚠\033[0m'
FAIL = '\033[31m✗\033[0m'


class Check:
    """一条体检结论。"""

    def __init__(self, name, ok, detail='', level='error'):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.level = level          # error | warn | info

    @property
    def symbol(self):
        if self.ok:
            return OK
        return WARN if self.level == 'warn' else FAIL

    def to_dict(self):
        return {'name': self.name, 'ok': self.ok, 'detail': self.detail,
                'level': self.level}


def fetch_state(url=None, state_file=None):
    """拿当前状态。优先 HTTP（服务在跑），失败再回退状态文件。

    返回 (state_dict, source_description) 或 (None, 原因)
    """
    http_error = None
    if url:
        try:
            with _opener().open(f'{url.rstrip("/")}/api/state', timeout=3) as r:
                return json.loads(r.read().decode('utf-8')), f'HTTP {url}'
        except Exception as exc:
            http_error = f'{exc.__class__.__name__}'

    path = state_file or DEFAULT_STATE_FILE
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                state = json.load(fh)
        except Exception as exc:
            return None, f'状态文件读不了（{exc.__class__.__name__}）'
        note = f'文件 {path}'
        if http_error:
            note += f'（HTTP 不可达：{http_error}）'
        return state, note
    if http_error:
        return None, f'HTTP 不可达（{http_error}），也没有状态文件'
    return None, '没找到状态（服务没起？状态文件不存在？）'


def state_age_seconds(state, state_file=None):
    """状态有多旧（秒）。拿 meta.last_update，拿不到就看文件 mtime。"""
    meta = (state or {}).get('meta') or {}
    last_update = meta.get('last_update')
    if isinstance(last_update, (int, float)) and last_update > 0:
        import time
        return max(0.0, time.time() - last_update)
    if state_file and os.path.isfile(state_file):
        import time
        return max(0.0, time.time() - os.path.getmtime(state_file))
    return None


def probe_driver_available(driver):
    """探测驱动在当前机器上能不能用。"""
    if driver == env_config.PROBE_MININET:
        if find_spec('mininet') is None:
            return False, 'mininet 没装（VM 内 apt install mininet）'
        return True, 'mininet 可用'
    if driver == env_config.PROBE_ICMP:
        if shutil.which('ping') is None:
            return False, '系统里没有 ping 命令'
        return True, 'ICMP 可用'
    return False, f'未知驱动 {driver}'


def run_checks(state, cfg, age_sec=None):
    """跑所有体检项。"""
    checks = []

    # --- 状态新鲜度：状态文件在但很旧 = 采集层其实没在跑 ---
    if age_sec is None:
        checks.append(Check('状态新鲜度', True, '未知', level='warn'))
    elif age_sec < 60:
        checks.append(Check('状态新鲜度', True,
                            f'更新于 {age_sec:.0f} 秒前'))
    else:
        checks.append(Check('状态新鲜度', False,
                            f'状态已经 {age_sec / 60:.0f} 分钟没更新 —— '
                            f'采集层可能没在跑（起一下：bash start_all.sh）',
                            level='warn'))

    # --- 配置 ---
    checks.append(Check(
        '环境配置已加载', True,
        f"{cfg.environment}（来源：{cfg.source}）"))

    checks.append(Check(
        '只读模式状态', True,
        f"mode={cfg.mode}"
        + ('，写操作已禁用' if cfg.is_read_only else '，允许写操作（注故障）'),
        level='info'))

    # --- 采集 ---
    switches = state.get('switches') or {}
    hosts = state.get('hosts') or {}
    links = state.get('links') or {}
    expect = cfg.topology_expect

    exp_sw = expect.get('switches')
    checks.append(Check(
        '交换机已连接',
        bool(switches) and (exp_sw is None or len(switches) >= int(exp_sw)),
        f"{len(switches)} 台已连接"
        + (f"（期望 {exp_sw}）" if exp_sw is not None else '')))

    exp_host = expect.get('hosts')
    checks.append(Check(
        '主机已发现',
        exp_host is None or len(hosts) >= int(exp_host),
        f"发现 {len(hosts)} 台"
        + (f"（期望 {exp_host}）" if exp_host is not None else '')))

    # state['links'] 只记「交换机之间」的链路（LLDP 发现的），
    # 主机连交换机的链路不在里面 —— 所以比对 inter_switch_links。
    exp_link = expect.get('inter_switch_links')
    checks.append(Check(
        '交换机间链路已发现',
        exp_link is None or len(links) >= int(exp_link),
        f"发现 {len(links)} 条交换机间链路"
        + (f"（期望 {exp_link}）" if exp_link is not None else '')))

    # 拓扑发现方式：只有"本该有交换机间链路却没发现"才算退化。
    # 1 交换机的拓扑本来就没有交换机间链路，不是问题（v3.21.0 修：之前会误报）
    expect_inter = expect.get('inter_switch_links')
    wants_inter = bool(expect_inter) and int(expect_inter) > 0
    if links:
        checks.append(Check('拓扑发现方式', True,
                            f'LLDP 邻居学习正常（{len(links)} 条交换机间链路）',
                            level='info'))
    elif wants_inter:
        checks.append(Check(
            '拓扑发现方式', False,
            f'退化：期望 {expect_inter} 条交换机间链路，发现 0 条 —— '
            f'真实设备可能没开 LLDP，拓扑图会是空的',
            level='warn'))
    else:
        checks.append(Check(
            '拓扑发现方式', True,
            '本环境无交换机间链路（单交换机拓扑），正常', level='info'))

    # --- 可选能力 ---
    probe_ok, probe_detail = probe_driver_available(cfg.probe_driver)
    checks.append(Check(
        f'探测驱动（{cfg.probe_driver}）',
        probe_ok, probe_detail,
        level='warn'))

    fault_ok = cfg.allow_fault_injection
    fault_detail = ('可用（mode=test）' if fault_ok
                    else '已禁用（只读模式或配置关闭）—— 接真实网络时这是对的')
    checks.append(Check(
        '故障注入', fault_ok or True, fault_detail, level='info'))

    # --- 契约 ---
    partner = cfg.assurance_expected_version
    if not partner:
        try:
            import assurance_bridge
            partner = assurance_bridge.DEFAULT_EXPECTED_VERSION
        except Exception:
            partner = 'unknown'
    checks.append(Check(
        '契约版本',
        True,
        f"我方路径版本 {capabilities.CONTRACT_PATH_VERSION} / 期望对方 {partner}",
        level='info'))

    return checks


def print_report(state, cfg, checks, source):
    print('=' * 68)
    print('intensure 环境体检（env_doctor v3.21.0）')
    print('=' * 68)
    print(f"环境      : {cfg.environment}")
    print(f"模式      : {cfg.mode}"
          + ('（只读，禁写操作）' if cfg.is_read_only else ''))
    print(f"配置来源  : {cfg.source}")
    print(f"状态来源  : {source}")
    print('-' * 68)
    for c in checks:
        print(f"  {c.symbol} {c.name:<22} {c.detail}")
    print('-' * 68)

    failed = [c for c in checks if not c.ok and c.level == 'error']
    warned = [c for c in checks if not c.ok and c.level == 'warn']
    if failed:
        print(f"  ✗ {len(failed)} 项失败："
              + '、'.join(c.name for c in failed))
    if warned:
        print(f"  ⚠ {len(warned)} 项告警："
              + '、'.join(c.name for c in warned))
    if not failed and not warned:
        print('  ✓ 全部通过')
    elif not failed:
        print('  ✓ 关键项全通过（有告警，不影响启动）')
    print('=' * 68)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description='intensure 环境体检')
    parser.add_argument('--url', default=None,
                        help=f'采集层 REST 地址（默认试 {DEFAULT_URL}）')
    parser.add_argument('--state-file', default=None,
                        help=f'状态文件（默认 {DEFAULT_STATE_FILE}）')
    parser.add_argument('--config', default=None,
                        help='环境配置文件路径')
    parser.add_argument('--json', action='store_true',
                        help='输出 JSON（给脚本用）')
    args = parser.parse_args()

    cfg = (env_config.EnvConfig.load(args.config) if args.config
           else env_config.get_config())

    url = args.url
    if url is None and not args.state_file:
        url = DEFAULT_URL
    state, source = fetch_state(url=url, state_file=args.state_file)

    if state is None:
        if args.json:
            print(json.dumps({'error': source, 'environment': cfg.environment,
                              'mode': cfg.mode}, ensure_ascii=False))
        else:
            print('=' * 68)
            print('intensure 环境体检（env_doctor v3.21.0）')
            print('=' * 68)
            print(f'环境      : {cfg.environment}')
            print(f'模式      : {cfg.mode}')
            print(f'配置来源  : {cfg.source}')
            print('-' * 68)
            print(f'  {FAIL} 采集层没跑起来 —— {source}')
            print('      体检需要先有状态。试试：')
            print('        bash start_all.sh     # 一键起 4 组件')
            print('=' * 68)
        return 2

    age = state_age_seconds(state, args.state_file or DEFAULT_STATE_FILE)
    checks = run_checks(state, cfg, age_sec=age)

    if args.json:
        print(json.dumps({
            'environment': cfg.environment,
            'mode': cfg.mode,
            'config_source': cfg.source,
            'state_source': source,
            'state_age_seconds': age,
            'capabilities': capabilities.build_capabilities(state, cfg),
            'checks': [c.to_dict() for c in checks],
            'failed': [c.name for c in checks if not c.ok and c.level == 'error'],
            'warnings': [c.name for c in checks if not c.ok and c.level == 'warn'],
        }, ensure_ascii=False, indent=2))
        return 1 if any(not c.ok and c.level == 'error' for c in checks) else 0

    return print_report(state, cfg, checks, source)


if __name__ == '__main__':
    sys.exit(main())
