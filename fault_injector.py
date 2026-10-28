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
from typing import Optional

# ---- 日志（P1-2: 统一用 logging, 不再用 print）----
import logging  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402


logger = logging.getLogger(__name__)


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

    # ---------- 突发 / 乱序 / VLAN / 流表竞态 (P2-3) ----------
    def burst(self, src: str, dst: str, rate_mbps: float = 100,
              limit_kb: int = 16):
        """突发流量: 限一个平均速率 + 很小的队列上限。

        与 rate_limit 的区别: rate_limit 用 tbf (平滑整形, 突发被削平)；
        这里用 netem 的 rate + limit —— limit 很小, 突发流量会把队列打爆,
        表现是「平均速率够但一冲就丢包」, 对应真实网络里的 microburst。
        """
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        for intf in intfs:
            self._tc_apply(intf, f'netem rate {rate_mbps}mbit limit {limit_kb}kb')
        self._active_faults.append(('burst', src, dst,
                                    {'mbps': rate_mbps, 'limit_kb': limit_kb}))
        self._log(f"burst {rate_mbps}Mbps/limit {limit_kb}kb on {src} <-> {dst}")

    def reorder(self, src: str, dst: str, gap_percent: int = 25,
                delay_ms: float = 10, gap_packets: int = 20):
        """包乱序: netem reorder。

        两个坑(实测踩出来的):
        1. netem 的 reorder **必须和 delay 一起用**, 只写 reorder 会报
           "reordering not possible without specifying some delay"。
        2. `gap` 的单位是**包数**不是时间 —— 写 `gap 20ms` 报
           "Illegal \"gap\""。这里用 gap_packets(包数)。
        """
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        for intf in intfs:
            self._tc_apply(intf, f'netem delay {delay_ms}ms '
                                 f'reorder {gap_percent}% gap {gap_packets}')
        self._active_faults.append(('reorder', src, dst,
                                    {'gap%': gap_percent, 'delay_ms': delay_ms,
                                     'gap_packets': gap_packets}))
        self._log(f"reorder {gap_percent}% on {src} <-> {dst}")

    def vlan_mismatch(self, src: str, dst: str, vlan_id: int = 999):
        """VLAN 不匹配: 给 src 侧交换机端口打一个 VLAN tag。

        场景: 收包方期望无 tag(untagged), 发包方打了 tag -> 收不到。
        实现: ovs-ofctl mod-vlan <switch> <port> <vlan>
        清理: ovs-ofctl --may-mod-vlan del-vlan <switch> <port>
        """
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        applied = []
        for intf in intfs:
            node = intf.node
            sw = getattr(node, 'name', '')
            if not sw.startswith('s'):
                continue  # 只对交换机口有意义
            # ⚠️ 这个 ovs-ofctl 版本**没有 mod-vlan 命令**（报 unknown command）。
            #   用 OVS 原生的 `ovs-vsctl set port <port> tag=<vlan>` 等价实现:
            #   发方端口打 tag, 收方期望 untagged -> 收不到, 即 VLAN 不匹配。
            node.cmd(f'ovs-vsctl set port {intf.name} tag={vlan_id}')
            applied.append((sw, intf.name))
        if not applied:
            self._log(f"⚠ no switch port found on {src} <-> {dst}")
            return
        self._active_faults.append(('vlan_mismatch', src, dst,
                                    {'vlan': vlan_id, 'ports': applied}))
        self._log(f"vlan_mismatch vlan={vlan_id} on {src} <-> {dst}")

    def flow_race(self, src: str, dst: str, rounds: int = 5):
        """流表竞态: 并发改流表, 制造「同一时刻两条流表变更」。

        现实来源: 控制器和人工/其它控制器同时改流表, 出现短暂不一致。
        这里用两个线程同时 ovs-ofctl del-flows + add-flow, 反复 rounds 次。
        """
        intfs = self._find_intfs(src, dst)
        if not intfs:
            self._log(f"⚠ no link found {src} <-> {dst}")
            return
        sw = None
        for intf in intfs:
            name = getattr(intf.node, 'name', '')
            if name.startswith('s'):
                sw = intf.node
                break
        if sw is None:
            self._log(f"⚠ no switch found on {src} <-> {dst}")
            return

        # ⚠️ 不能用 node.cmd(): Mininet 的 Node 只有一个 shell,
        #    并发调用会撞 `assert self.shell and not self.waiting`。
        #    这里直接起 subprocess 跑 ovs-ofctl —— 既是真并发(两个独立进程),
        #    也才真的制造得出「同时改流表」的竞态。
        bridge = sw.name
        port = intfs[0].name.split('-')[-1]
        if port.startswith('eth'):
            port = port[3:]

        def churn():
            subprocess.run(
                ['ovs-ofctl', '-O', 'OpenFlow13', 'del-flows',
                 '--priority', '100', bridge],
                capture_output=True)
            subprocess.run(
                ['ovs-ofctl', '-O', 'OpenFlow13', 'add-flow', bridge,
                 'table=0,priority=100,idle_timeout=20,'
                 'in_port=%s,actions=NORMAL' % port],
                capture_output=True)

        for _ in range(rounds):
            threads = [threading.Thread(target=churn) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self._active_faults.append(('flow_race', src, dst, {'rounds': rounds}))
        self._log(f"flow_race {rounds} rounds on {src} <-> {dst}")

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
                # 清 VLAN tag (P2-3: vlan_mismatch 打的 tag 要单独撤)
                try:
                    sw_name = getattr(node, 'name', '')
                    if sw_name.startswith('s'):
                        node.cmd(f'ovs-vsctl clear port {intf.name} tag '
                                 '2>/dev/null || true')
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
        logger.info(f"  💥 fault: {msg}")

    @property
    def active_faults(self) -> list:
        return list(self._active_faults)


# ============ CLI: 不依赖 Mininet 的快速测试 ============
if __name__ == '__main__':
    logger.info("FaultInjector 是 Mininet 上下文中的辅助类.")
    logger.info("用法: from fault_injector import FaultInjector; fi = FaultInjector(net)")
    logger.info("      fi.link_down('s1', 's2')")
