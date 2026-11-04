"""4 交换机全网状环境: 验证 LLDP 发现 + 圆形布局. 保持运行供观察."""
import sys
import time
from pathlib import Path

# 自解析项目根：不再硬编码某个账号的家目录（换机 / 换用户不用改代码）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.log import setLogLevel
from topo_mesh import MeshTopo

setLogLevel('warning')
net = Mininet(topo=MeshTopo(),
              controller=lambda n: RemoteController(n, ip='127.0.0.1', port=6653),
              switch=OVSSwitch, autoSetMacs=True, autoStaticArp=True)
net.start()
print('MESH_ENV_STARTED', flush=True)
time.sleep(5)
print('PINGALL:', net.pingAll(), flush=True)
while True:
    try:
        net.get('h1').cmd('ping -c 2 -W 1 10.0.0.4 > /dev/null 2>&1')
        net.get('h2').cmd('ping -c 2 -W 1 10.0.0.3 > /dev/null 2>&1')
    except Exception:
        pass
    time.sleep(10)
