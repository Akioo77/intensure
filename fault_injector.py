"""故障注入器：在 Mininet 网络中模拟各种故障。

支持故障类型:
  - link_down    关闭链路两端 (ip link + ovs-ofctl mod-port)
  - link_up      恢复链路
  - delay        加时延 (tc netem delay)
  - loss         加丢包 (tc netem loss)
  - rate_limit   限速 (tc tbf)

每种故障都有 list / clear 接口, 便于 demo 演示。

⚠ 本版使用 Mininet 底层命令 (node.cmd) 实现, 不依赖
   Mininet.config / Intf.config 的高层参数 (该 Mininet 版本不支持)。
"""
import time
from typing import Optional


class FaultInjector:
    """基于 Mininet Net 对象的故障注入器.

    Usage:
        net = Mininet(topo=...)
        net.start()
        f = FaultInjector(net)
        f.link_down('s1', 's2')
        f.delay('s1', 's2', delay_ms=100)
        f.clear_all()
    """

    def __init__(self, net):
        self.net = net
        self._active_faults = []  # [(type, src, dst, params), ...]

    # ---------- 链路 ----------
    def link_down(self, src: str, dst: str):
        """关闭 src <-> dst 之间的链路 (双向)."""
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        for intf in intfs:
            self._set_link(intf, down=True)
            self._active_faults.append(('link_down', src, dst,
                                         {'intf': intf.name}))
        self._log(f"link_down {src} <-> {dst}")

    def link_up(self, src: str, dst: str):
        """恢复 src <-> dst 链路."""
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        for intf in intfs:
            self._set_link(intf, down=False)
        self._active_faults = [f for f in self._active_faults
                                if not (f[0] == 'link_down'
                                        and f[1] == src and f[2] == dst)]
        self._log(f"link_up {src} <-> {dst}")

    def _set_link(self, intf, down: bool):
        """把单个接口 down/up. 交换机口额外用 ovs-ofctl mod-port 通知 OVS."""
        node = intf.node
        state = 'down' if down else 'up'
        # 1) 内核接口 up/down (影响 carrier)
        node.cmd(f'ip link set {intf.name} {state}')
        # 2) OVS 交换机口: 通知 OVS 端口状态 (触发 OFPPS_LINK_DOWN / 恢复)
        if getattr(node, 'name', '').startswith('s') and hasattr(node, 'vsctl'):
            node.cmd(f'ovs-ofctl -O OpenFlow13 mod-port {node.name} {intf.name} {state}')

    # ---------- 时延 / 丢包 / 限速 ----------
    def delay(self, src: str, dst: str, delay_ms: float,
              jitter_ms: float = 0):
        """在链路两个方向都加时延 (tc netem, 双向, 便于观察 RTT)."""
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        for intf in intfs:
            self._tc_apply(intf, f'netem delay {delay_ms}ms'
                                 f'{" " + str(jitter_ms) + "ms" if jitter_ms else ""}')
        self._active_faults.append(('delay', src, dst,
                                     {'ms': delay_ms}))
        self._log(f"delay {delay_ms}ms on {src} <-> {dst} (both directions)")

    def loss(self, src: str, dst: str, percent: float):
        """在链路两个方向加丢包率 (% 0-100)."""
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        for intf in intfs:
            self._tc_apply(intf, f'netem loss {percent}%')
        self._active_faults.append(('loss', src, dst, {'%': percent}))
        self._log(f"loss {percent}% on {src} <-> {dst} (both directions)")

    def rate_limit(self, src: str, dst: str, rate_mbps: float,
                   burst_kb: int = 32):
        """限制链路速率 (双向 tc tbf)."""
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        for intf in intfs:
            self._tc_apply(intf,
                           f'tbf rate {rate_mbps}mbit burst {burst_kb}kb '
                           f'latency 50ms')
        self._active_faults.append(('rate', src, dst,
                                     {'mbps': rate_mbps}))
        self._log(f"rate_limit {rate_mbps}Mbps on {src} <-> {dst} (both)")

    def _tc_apply(self, intf, qdisc_spec: str):
        """用 replace 应用 tc qdisc (幂等, 覆盖已有配置)."""
        node = intf.node
        node.cmd(f'tc qdisc replace dev {intf.name} root {qdisc_spec}')

    def clear_all(self):
        """清除所有故障 + 所有 TC 配置 + 恢复所有链路."""
        # 恢复所有链路 up
        for node in self.net.values():
            for intf in node.intfList():
                try:
                    self._set_link(intf, down=False)
                except Exception:
                    pass
                # 清 TC (忽略 "no such file" 类错误)
                try:
                    node.cmd(f'tc qdisc del dev {intf.name} root 2>/dev/null || true')
                except Exception:
                    pass
        self._active_faults.clear()
        self._log("cleared all faults")

    # ---------- 工具 ----------
    def _find_intfs(self, src: str, dst: str) -> Optional[list]:
        """在 net.links 里找 src<->dst 的 link, 返回两个接口对象."""
        for link in self.net.links:
            n1, n2 = link.intf1.node.name, link.intf2.node.name
            if (n1 == src and n2 == dst) or (n1 == dst and n2 == src):
                return [link.intf1, link.intf2]
        return None

    def _log(self, msg: str):
        print(f"  💥 fault: {msg}")

    @property
    def active_faults(self) -> list:
        return list(self._active_faults)


# ============ CLI: 不依赖 Mininet 的快速测试 ============
if __name__ == '__main__':
    print("FaultInjector 是 Mininet 上下文中的辅助类.")
    print("用法: from fault_injector import FaultInjector; fi = FaultInjector(net)")
    print("      fi.link_down('s1', 's2')")
