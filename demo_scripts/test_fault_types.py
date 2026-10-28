#!/usr/bin/env python3
"""P2-3 实测: 4 种新故障在真实 Mininet 拓扑上。

用法(在 VM 内):
    cd /home/sunxiaoxuan.guest/intensure
    sudo python3 demo_scripts/test_fault_types.py

⚠️ 需要 mininet + ovs-ofctl + 能起 os-ken 控制器 —— CI 里跑不了, 属于集成测试。
单元测试套件(test_*.py)不包含它。

覆盖:
    burst         tc netem rate + 小 limit (队列被打爆)
    reorder       tc netem delay + reorder + gap(包数)
    vlan_mismatch ovs-vsctl set port tag=999
    flow_race     两个 subprocess 真并发改流表
    clear_all     qdisc / vlan / 链路 全部恢复
"""
import sys
import time

sys.path.insert(0, '/home/sunxiaoxuan.guest/intensure')

from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import SingleSwitchTopo
from mininet.log import setLogLevel, info

from fault_injector import FaultInjector

setLogLevel('warning')
results = []


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    print(('  [PASS] ' if cond else '  [FAIL] ') + name + ('  ' + str(detail) if detail else ''), flush=True)


# 控制器必须先起(否则基线 ping 不通, 流表也没人下发)
import subprocess
import os
ctrl = subprocess.Popen(
    ['osken-manager', 'controller_app'],
    env={**os.environ, 'PYTHONPATH': '/home/sunxiaoxuan.guest/intensure'},
    stdout=open('/tmp/_fi_ctrl.log', 'w'), stderr=subprocess.STDOUT)
time.sleep(4)

topo = SingleSwitchTopo(k=4)
net = Mininet(topo=topo,
              controller=lambda n: RemoteController(n, ip='127.0.0.1', port=6653),
              switch=OVSSwitch, autoSetMacs=True, autoStaticArp=True)
net.start()
fi = FaultInjector(net)
info('--- topology up ---')

h1 = net.get('h1')
h4 = net.get('h4')

# 基线: 互通
base_loss = h1.cmd('ping -c 3 -W 1 10.0.0.4')
check('基线 h1->h4 可达', ' 0% ' in base_loss or ' 0.00%' in base_loss or base_loss.strip().endswith('0% packet loss'), base_loss.strip().splitlines()[-1] if base_loss.strip() else '')

# ---------- 1. burst ----------
fi.burst('s1', 'h1', rate_mbps=100, limit_kb=8)
q = net.get('s1').cmd('tc qdisc show dev s1-eth1')
check('burst 生效(netem rate + limit)', 'netem' in q and 'limit' in q, q.strip()[:90])
check('burst 记进 active_faults', any(f[0] == 'burst' for f in fi.active_faults))
fi.clear_all()
q = net.get('s1').cmd('tc qdisc show dev s1-eth1')
check('clear_all 后 qdisc 已清', 'noqueue' in q or 'netem' not in q, q.strip()[:70])

# ---------- 2. reorder ----------
fi.reorder('s1', 'h1', gap_percent=25, delay_ms=10, gap_packets=20)
q = net.get('s1').cmd('tc qdisc show dev s1-eth1')
check('reorder 生效(netem delay+reorder+gap)', 'reorder' in q and 'gap' in q, q.strip()[:100])
check('reorder 记进 active_faults', any(f[0] == 'reorder' for f in fi.active_faults))
fi.clear_all()

# ---------- 3. vlan_mismatch ----------
fi.vlan_mismatch('s1', 'h1', vlan_id=999)
v = net.get('s1').cmd('ovs-vsctl list port s1-eth1')
check('vlan_mismatch 打了 tag', '999' in v, [l.strip() for l in v.splitlines() if l.strip().startswith('tag')])
check('vlan_mismatch 记进 active_faults', any(f[0] == 'vlan_mismatch' for f in fi.active_faults))
fi.clear_all()
v2 = net.get('s1').cmd('ovs-vsctl list port s1-eth1')
check('clear_all 后 VLAN tag 已撤', 'tag                 : []' in v2 or '999' not in v2, [l.strip() for l in v2.splitlines() if l.strip().startswith('tag')])

# ---------- 4. flow_race ----------
before_flows = net.get('s1').cmd('ovs-ofctl -O OpenFlow13 dump-flows s1')
fi.flow_race('s1', 'h1', rounds=3)
after_flows = net.get('s1').cmd('ovs-ofctl -O OpenFlow13 dump-flows s1 | grep -c "cookie"')
check('flow_race 执行后流表仍可读', after_flows.strip() != '', 'flows=%s' % after_flows.strip())
check('flow_race 记进 active_faults', any(f[0] == 'flow_race' for f in fi.active_faults))
fi.clear_all()

# ---------- 收尾: 链路恢复 ----------
time.sleep(1)
after = h1.cmd('ping -c 3 -W 1 10.0.0.4')
check('clear_all 后链路恢复正常', ' 0% ' in after or after.strip().endswith('0% packet loss'), after.strip().splitlines()[-1] if after.strip() else '')

net.stop()
try:
    ctrl.terminate()
except Exception:
    pass
ok = sum(1 for _, c, _ in results if c)
print('')
print('  PASSED: %d/%d' % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
