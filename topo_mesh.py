"""4 交换机全网状拓扑 (K4, 6 条链路) + 4 主机.

   s1 --- s2
   |  \\ /  |
   |  / \\  |
   s3 --- s4
   每台交换机挂 1 台主机 (h1..h4)
"""
from mininet.topo import Topo


class MeshTopo(Topo):
    def build(self):
        sws = [self.addSwitch(f's{i}', protocols='OpenFlow13')
               for i in range(1, 5)]
        hosts = [self.addHost(f'h{i}', ip=f'10.0.0.{i}/24')
                 for i in range(1, 5)]

        for i, h in enumerate(hosts):
            self.addLink(h, sws[i])

        # K4 全互联: 6 条交换机间链路
        for i in range(4):
            for j in range(i + 1, 4):
                self.addLink(sws[i], sws[j], bw=100, delay='2ms')


topos = {'mesh': (lambda: MeshTopo())}
