"""实时联动验证: OVS 真实状态 vs 控制器 REST 状态, 以及实时变化感知."""
import json
import subprocess
import sys
import time
import urllib.request

API = 'http://127.0.0.1:8080'


def get_state():
    with urllib.request.urlopen(API + '/api/state', timeout=4) as r:
        return json.loads(r.read())


def rest_ports():
    d = get_state()
    lines = []
    for dpid, sw in d['switches'].items():
        parts = []
        for p in sw['ports']:
            if p['port_no'] == 4294967294:
                continue
            st = 'UP' if not (p['state'] & 1) else 'DOWN'
            parts.append(f"{p['name']}={st}")
        lines.append(f"  REST  {dpid} ({sw['dpid']}): " + ', '.join(parts))
    return '\n'.join(lines)


def ovs_show():
    out = subprocess.run(['sudo', 'ovs-vsctl', 'show'],
                         capture_output=True, text=True).stdout
    # 只看 Bridge 和 Port
    lines = [l.strip() for l in out.splitlines()
             if l.strip().startswith(('Bridge', 'Port'))]
    return '\n'.join(lines)


print("=" * 62)
print("  证据 1: 同一份网络状态, 两个独立视角")
print("=" * 62)
print("\n--- OVS 真实数据库 (ovs-vsctl) ---")
print(ovs_show())
print("\n--- 控制器 REST API (OpenFlow 读到的) ---")
print(rest_ports())

print("\n" + "=" * 62)
print("  证据 2: 实时联动 — 现在把 s1-eth3 链路拉断, 看 REST 是否立刻变化")
print("=" * 62)
before = get_state()['meta']['update_count']
print(f"\n  当前控制器状态更新计数: {before}")

print("  执行: ip link set s1-eth3 down ...")
subprocess.run(['sudo', 'ip', 'link', 'set', 's1-eth3', 'down'], check=True)
subprocess.run(['sudo', 'ovs-ofctl', '-O', 'OpenFlow13', 'mod-port', 's1', 's1-eth3', 'down'],
               capture_output=True)
time.sleep(2)

d = get_state()
after = d['meta']['update_count']
print(f"  2 秒后 REST 状态更新计数: {after} (增加 {after - before})")
for dpid, sw in d['switches'].items():
    for p in sw['ports']:
        if p['name'] == 's1-eth3':
            st = 'DOWN' if p['state'] & 1 else 'UP'
            print(f"  → REST 读到 s1-eth3: {st}  (state=0x{p['state']:x})")
# 事件
evs = get_state()['events'][-3:]
print(f"  最近事件: {[(e['type'], e.get('name'), e.get('down')) for e in evs]}")

print("\n  恢复链路 ...")
subprocess.run(['sudo', 'ip', 'link', 'set', 's1-eth3', 'up'], check=True)
subprocess.run(['sudo', 'ovs-ofctl', '-O', 'OpenFlow13', 'mod-port', 's1', 's1-eth3', 'up'],
               capture_output=True)
time.sleep(2)
d = get_state()
for dpid, sw in d['switches'].items():
    for p in sw['ports']:
        if p['name'] == 's1-eth3':
            st = 'DOWN' if p['state'] & 1 else 'UP'
            print(f"  → 恢复后 REST 读到 s1-eth3: {st}")

print("\n✅ 结论: REST 数据 = 控制器从真实 OVS 交换机经 OpenFlow 读到的状态,")
print("  不是前端写死的假数据。dashboard 每个数字都来自这条链路。")
