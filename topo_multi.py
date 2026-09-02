"""3 交换机 6 主机线性拓扑: 验证拓扑图自适应任意规模.

   h1,h2 -- s1 -- s2 -- s3 -- h5,h6
                     |       |
                    h3      h4
"""
from mininet.topo import Topo


class MultiTopo(Topo):
    """3 台交换机线性互联, 每台挂 2 台主机."""

    def build(self):
        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', protocols='OpenFlow13')

        hosts = [self.addHost(f'h{i}', ip=f'10.0.0.{i}/24')
                 for i in range(1, 7)]

        self.addLink(hosts[0], s1)   # h1
        self.addLink(hosts[1], s1)   # h2
        self.addLink(hosts[2], s2)   # h3
        self.addLink(hosts[3], s2)   # h4
        self.addLink(hosts[4], s3)   # h5
        self.addLink(hosts[5], s3)   # h6

        self.addLink(s1, s2, bw=100, delay='5ms')
        self.addLink(s2, s3, bw=100, delay='5ms')


topos = {'multi': (lambda: MultiTopo())}
