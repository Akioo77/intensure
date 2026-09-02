"""快速验证: 端口状态 down → up 恢复 + 确认 Step2 警告来源."""
import sys
import time

sys.path.insert(0, '/home/sunxiaoxuan.guest/intensure')

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel
from topo_simple import SimpleTopo
from state_collector import StateCollector
from fault_injector import FaultInjector

setLogLevel('warning')

net = Mininet(topo=SimpleTopo(),
              controller=lambda n: RemoteController(n, ip='127.0.0.1', port=6653),
              switch=OVSSwitch, autoSetMacs=True, autoStaticArp=True)
net.start()

c = StateCollector('http://127.0.0.1:8080')
for _ in range(40):
    s = c.get_state()
    if len(s.get('switches', {})) >= 2:
        break
    time.sleep(0.5)
time.sleep(2)


def summarize(tag):
    state = c.get_state()
    print(f"\n=== {tag} ===")
    for dpid, sw in state.get('switches', {}).items():
        for p in sw.get('ports', []):
            name = p.get('name', '')
            st = p.get('state', 0)
            flag = '⬇DOWN' if (st & 1) else 'UP'
            print(f"  {dpid} {name:12s} port_no={p.get('port_no')} state={st} {flag}")


summarize("初始状态 (看哪些端口 down)")
print("\n事件记录:", [e['type'] for e in c.get_events()[-10:]])

fi = FaultInjector(net)
fi.link_down('s1', 's2')
time.sleep(2)
summarize("link_down 后")
print("\n事件记录:", [e['type'] for e in c.get_events()[-6:]])

fi.link_up('s1', 's2')
time.sleep(3)
summarize("link_up 恢复后")
print("\n事件记录:", [e['type'] for e in c.get_events()[-6:]])

fi.clear_all()
net.stop()
print("\nDONE")
