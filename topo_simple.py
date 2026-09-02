"""简单 4 主机 2 交换机线性拓扑。

   h1 --- s1 --- s2 --- h2
          |      |
          h3     h4

用法:
    sudo mn --custom topo_simple.py --topo simple \\
        --controller remote,ip=127.0.0.1,port=6653 \\
        --switch ovsk,protocols=OpenFlow13
"""
from mininet.topo import Topo


class SimpleTopo(Topo):
    """线性拓扑：4 主机 + 2 交换机，全连通（任意两个主机可达）。"""

    def build(self):
        # 2 台交换机（OpenFlow 1.3）
        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')

        # 4 台主机（同一子网 10.0.0.0/24）
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        h3 = self.addHost('h3', ip='10.0.0.3/24')
        h4 = self.addHost('h4', ip='10.0.0.4/24')

        # 主机到交换机（低时延）
        self.addLink(h1, s1, bw=100, delay='1ms')
        self.addLink(h3, s1, bw=100, delay='1ms')
        self.addLink(h2, s2, bw=100, delay='1ms')
        self.addLink(h4, s2, bw=100, delay='1ms')

        # 交换机之间（较高时延，便于观察）
        self.addLink(s1, s2, bw=100, delay='5ms')


# Mininet 注册
topos = {'simple': (lambda: SimpleTopo())}
