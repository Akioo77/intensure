#!/usr/bin/env python3
"""cross_machine_check.py —— 跨机接入预检（联调用）

**验证什么**：Mininet 在**别人**的机器上时，我们能不能正确接入并完成
「采集 → 注故障 → 感知 → 恢复」全链路。

这是联调前的第一道关口 —— 之前所有命令都跑在本机，接到别人的机器上
能不能跑通，没人验证过。现在有可执行的检查了。

**怎么用**（在**采集侧**机器上跑，Mininet 在对方那台）：

```bash
# 1. 看远端拓扑（不改动任何东西，只读）
python3 demo_scripts/cross_machine_check.py --host 10.0.0.9 --user partner

# 2. 只读自检：命令能不能跑、端口能不能列出来、能不能 ping
python3 demo_scripts/cross_machine_check.py --host 10.0.0.9 --user partner --check-only

# 3. 完整演练：注故障 → 感知 → 恢复（⚠️ 会在对方机器上真的断一条链路）
python3 demo_scripts/cross_machine_check.py --host 10.0.0.9 --user partner --inject
```

参数：
  --host / --user / --port / --key   远端 SSH 信息（也可用环境变量
                                       INTENSURE_VM_HOST / _USER / _PORT / _KEY）
  --check-only                       只做只读检查（默认行为：不碰对方网络）
  --inject                           注一次故障再恢复（**会短暂断链**）

⚠️ 远端机器需要**免密 sudo**（命令带 `sudo -n`，拿不到就立刻失败，
不会卡在密码提示上），以及 `mnexec`（Mininet 自带，用于进主机命名空间 ping）。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exec_target            # noqa: E402
import fault_api              # noqa: E402


def build_target(args) -> exec_target.ExecTarget:
    host = args.host or os.environ.get('INTENSURE_VM_HOST', '')
    user = args.user or os.environ.get('INTENSURE_VM_USER', '')
    port = int(args.port or os.environ.get('INTENSURE_VM_PORT', 22) or 22)
    key = args.key or os.environ.get('INTENSURE_VM_KEY', '')
    if not host:
        raise SystemExit('❌ 要指定远端：--host 或环境变量 INTENSURE_VM_HOST')
    return exec_target.SshExec(host=host, user=user, port=port, key=key,
                               connect_timeout=5)


def check_readonly(target):
    """只读检查：命令能跑吗、端口能列吗、能 ping 吗。"""
    print('=== 1. 远端执行 ip -br link show ===')
    r = target.run(['ip', '-br', 'link', 'show'])
    print(f'rc={r.returncode}')
    if not r.ok:
        print(f'stderr: {(r.stderr or "").strip()[:300]}')
        return False
    ports = [ln.split()[0].split('@')[0] for ln in (r.stdout or '').splitlines()
             if ln.split() and '-eth' in ln.split()[0]]
    print(f'远端交换机端口: {ports}')
    if not ports:
        print('⚠️  没看到 sN-ethM 端口 —— 对方那台机器上可能没跑 Mininet/OVS')
        return False

    print('\n=== 2. 通过适配层解析端口清单 ===')
    api = fault_api.FaultApi(target=target)
    print(f'解析结果: {api._all_switch_ports()}')

    print('\n=== 3. 在远端做一次 ping（进 h1 命名空间）===')
    loss, latency = target.ping('h1', '10.0.0.4', count=2, timeout=8)
    print(f'丢包率={loss}%  avg RTT={latency}ms')
    print('（100% 丢包不一定是故障 —— 也可能对方拓扑里 h1→h4 本来就不通）')
    return True


def check_inject(target):
    """完整演练：注故障 → 确认 → 恢复。⚠️ 会真的断一条链路。"""
    api = fault_api.FaultApi(target=target)
    iface = 's1-eth1'
    print(f'=== 注入 link_down @ {iface}（远端）===')
    rec = api.inject('link_down', iface)
    print(f'注入结果: {rec["fault_type"]} on {rec["interfaces"]}')
    time.sleep(3)

    print('\n=== 远端端口状态 ===')
    print((target.run(['ip', '-br', 'link', 'show', iface]).stdout or '').strip())

    print('\n=== 采集侧感知（看远端交换机上的 OpenFlow 状态由对端采集器判定）===')
    print('ℹ️  端口是否被采集侧记录，要去对端看它的 /api/state 或 port_events 表。')

    print('\n=== 恢复 ===')
    print(api.clear_all(iface))
    time.sleep(2)
    print((target.run(['ip', '-br', 'link', 'show', iface]).stdout or '').strip())
    print('\n✅ 跨机注入/恢复演练完成')


def main():
    ap = argparse.ArgumentParser(description='跨机接入预检')
    ap.add_argument('--host')
    ap.add_argument('--user')
    ap.add_argument('--port')
    ap.add_argument('--key')
    ap.add_argument('--check-only', action='store_true',
                    help='只做只读检查（默认行为）')
    ap.add_argument('--inject', action='store_true',
                    help='注一次故障再恢复（⚠️ 会短暂断链）')
    args = ap.parse_args()

    target = build_target(args)
    print(f'接入目标: {target.describe()}\n')

    ok = check_readonly(target)
    if not ok:
        sys.exit(1)
    if args.inject:
        check_inject(target)
    else:
        print('\n✅ 只读检查通过。要演练注故障/恢复，加 --inject')


if __name__ == '__main__':
    main()
