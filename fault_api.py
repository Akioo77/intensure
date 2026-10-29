"""故障注入后端（不依赖 Mininet net 对象）。

## 为什么不用 FaultInjector

`fault_injector.FaultInjector` 需要 Mininet 的 `net` 对象（**Mininet 进程内**才有），
而 `state_server.py` 是**独立进程**，拿不到 net。
所以这里改用底层命令（`ip link` / `tc qdisc` / `ovs-ofctl` / `ovs-vsctl`）实现，
语义与 `fault_cli.sh` 一致 —— 那条路已经被实测验证过。

## 权限

`ip link set` / `tc qdisc` / `ovs-ofctl mod-port` 都需要 root。
VM 里是**免密 sudo**，所以统一用 `sudo -n` 前缀（`-n` = 不弹密码，失败就报错，
绝不阻塞等输入 —— 那是把 HTTP 请求挂死的经典方式）。

## 与其它模块的边界

本模块只做「把故障打进去」，**不做**一致性判定、不写 `assurance.*` 表。
判定与落库由 `assurance_mvp.py` 负责 —— 职责分离，和
`docs/partner_合并文档.md` 第二节的口径一致。
"""
import logging
import re
import subprocess
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 故障类型别名 → 内部实现名。
# 别名存在的意义：演示脚本 04 用的是业务语义名（high_latency/packet_loss/flow_missing），
# 而底层实现名是网络语义名（delay/loss/flow_race）。两边都收。
FAULT_ALIASES = {
    'link_down': 'link_down',
    'link_up': 'link_up',
    'high_latency': 'delay',
    'latency': 'delay',
    'delay': 'delay',
    'packet_loss': 'loss',
    'loss': 'loss',
    'rate_limit': 'rate_limit',
    'rate': 'rate_limit',
    'burst': 'burst',
    'reorder': 'reorder',
    'vlan_mismatch': 'vlan_mismatch',
    'flow_missing': 'flow_race',
    'flow_race': 'flow_race',
    'clear': 'clear_all',
    'clear_all': 'clear_all',
}

# 一次命令的超时（秒）。不给上限的话，ovs-ofctl 在异常拓扑上可能挂住，
# 而它跑在 HTTP 请求线程里 —— 一个挂住的请求就能拖垮整个 state_server。
CMD_TIMEOUT = 20

_IFACE_RE = re.compile(r'^(?P<sw>[a-zA-Z0-9_]+)-eth(?P<port>\d+)$')


def _num(v) -> str:
    """格式化成 tc 友好的数字：1.0 → '1'，1.5 → '1.5'。

    tc 对 '1.0mbit' 这种写法不认（会报 unknown parameter），
    所以整数速率要去掉小数尾巴。
    """
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


class FaultInjectionError(Exception):
    """故障注入失败（参数错 / 接口找不到 / 命令失败）。"""


class FaultApi:
    """按接口名直接注入故障的后端（无 Mininet 依赖）。

    Usage:
        api = FaultApi()
        api.inject('link_down', 's1-eth1')
        api.inject('clear', 's1')
    """

    def __init__(self):
        self._active: List[Dict[str, object]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def inject(self, fault_type: str, target: str,
               params: Optional[Dict[str, object]] = None,
               mode: str = 'test') -> Dict[str, object]:
        """注入一个故障。

        Args:
            fault_type: 故障类型（支持别名，见 FAULT_ALIASES）
            target: 目标。两种写法：
                - 具体接口名（'s1-eth1'）
                - 交换机名（'s1'）→ 取该交换机所有端口
            params: 额外参数（delay_ms / percent / rate_mbps / vlan_id / rounds ...）
            mode: 标注调用来源，默认 'test'（测试接口，与生产模块解耦）
        """
        params = dict(params or {})
        key = FAULT_ALIASES.get(str(fault_type).strip().lower())
        if key is None:
            raise FaultInjectionError(
                f"未知故障类型: {fault_type}（可选: {', '.join(sorted(FAULT_ALIASES))}）")

        if key == 'clear_all':
            return self.clear_all(target)

        if not target:
            raise FaultInjectionError("缺少 target（接口名如 s1-eth1，或交换机名如 s1）")

        intfs = self._resolve_interfaces(target, params)
        if not intfs:
            raise FaultInjectionError(f"找不到匹配 '{target}' 的接口")

        handler = getattr(self, f'_do_{key}')
        applied = handler(intfs, params)

        record = {
            'fault_type': key,
            'requested_type': fault_type,
            'target': target,
            'interfaces': applied,
            'params': params,
            'mode': mode,
        }
        with self._lock:
            self._active = [f for f in self._active
                            if not (f['fault_type'] == key
                                    and set(f['interfaces']) & set(applied))]
            self._active.append(record)
        logger.info(f"💥 fault injected: {key} on {applied} (mode={mode})")
        return record

    def clear_all(self, target: str = '') -> Dict[str, object]:
        """清除所有故障：链路恢复 up + 撤 TC + 撤 VLAN tag。

        target 为空 → 全网清理；给了 → 只清该交换机/接口。
        """
        intfs = self._resolve_interfaces(target) if target else self._all_interfaces()
        cleaned = []
        for intf in intfs:
            sw = intf.split('-eth')[0]
            # 链路必须恢复（这步失败是真问题 → 报错）
            self._run(['ip', 'link', 'set', intf, 'up'])
            self._run(['ovs-ofctl', '-O', 'OpenFlow13', 'mod-port', sw, intf, 'up'])
            # 下面两步是「清理」语义 —— 没有东西可清是**正常**的，
            # 不能因为「本来就没 qdisc」就让整个清理失败（踩过：
            # tc qdisc del 报 "Cannot delete qdisc with handle of zero"）。
            self._run(['tc', 'qdisc', 'del', 'dev', intf, 'root'], check=False)
            self._run(['ovs-vsctl', 'clear', 'port', intf, 'tag'], check=False)
            cleaned.append(intf)
        with self._lock:
            if target:
                self._active = [f for f in self._active
                                if not (set(f['interfaces']) & set(cleaned))]
            else:
                self._active = []
        logger.info(f"✅ cleared faults on {cleaned}")
        return {'fault_type': 'clear_all', 'interfaces': cleaned,
                'mode': 'test', 'cleared': len(cleaned)}

    def active_faults(self) -> List[Dict[str, object]]:
        """当前活跃故障列表。"""
        with self._lock:
            return list(self._active)

    # ------------------------------------------------------------------
    # 各类故障实现
    # ------------------------------------------------------------------
    def _do_link_down(self, intfs: List[str], params: Dict) -> List[str]:
        for intf in intfs:
            sw = intf.split('-eth')[0]
            self._run(['ip', 'link', 'set', intf, 'down'])
            self._run(['ovs-ofctl', '-O', 'OpenFlow13', 'mod-port', sw, intf, 'down'])
        return intfs

    def _do_link_up(self, intfs: List[str], params: Dict) -> List[str]:
        for intf in intfs:
            sw = intf.split('-eth')[0]
            self._run(['ip', 'link', 'set', intf, 'up'])
            self._run(['ovs-ofctl', '-O', 'OpenFlow13', 'mod-port', sw, intf, 'up'])
        return intfs

    def _do_delay(self, intfs: List[str], params: Dict) -> List[str]:
        delay_ms = float(params.get('delay_ms', params.get('ms', 100)))
        jitter_ms = float(params.get('jitter_ms', 0))
        for intf in intfs:
            cmd = ['tc', 'qdisc', 'replace', 'dev', intf, 'root',
                   'netem', 'delay', f'{_num(delay_ms)}ms']
            if jitter_ms:
                cmd += [f'{_num(jitter_ms)}ms']
            self._run(cmd)
        return intfs

    def _do_loss(self, intfs: List[str], params: Dict) -> List[str]:
        percent = float(params.get('percent', params.get('pct', 10)))
        for intf in intfs:
            self._run(['tc', 'qdisc', 'replace', 'dev', intf, 'root',
                       'netem', 'loss', f'{_num(percent)}%'])
        return intfs

    def _do_rate_limit(self, intfs: List[str], params: Dict) -> List[str]:
        rate_mbps = float(params.get('rate_mbps', params.get('rate', 10)))
        burst_kb = int(params.get('burst_kb', 32))
        for intf in intfs:
            # ⚠️ 参数必须**拆成独立 argv 元素**。
            #    写成 f'rate {rate}mbit' 会把整个串当成一个参数，
            #    tc 报 `unknown parameter "rate 1.0mbit"`（踩过）。
            self._run(['tc', 'qdisc', 'replace', 'dev', intf, 'root',
                       'tbf', 'rate', f'{_num(rate_mbps)}mbit',
                       'burst', f'{burst_kb}kb', 'latency', '50ms'])
        return intfs

    def _do_burst(self, intfs: List[str], params: Dict) -> List[str]:
        rate_mbps = float(params.get('rate_mbps', 100))
        limit_kb = int(params.get('limit_kb', 16))
        for intf in intfs:
            self._run(['tc', 'qdisc', 'replace', 'dev', intf, 'root',
                       'netem', 'rate', f'{_num(rate_mbps)}mbit',
                       'limit', f'{limit_kb}kb'])
        return intfs

    def _do_reorder(self, intfs: List[str], params: Dict) -> List[str]:
        # netem 的 reorder **必须配 delay**，否则报 "reordering not possible
        # without specifying some delay"；gap 单位是**包数**不是时间。
        gap_percent = int(params.get('gap_percent', 25))
        delay_ms = float(params.get('delay_ms', 10))
        gap_packets = int(params.get('gap_packets', 20))
        for intf in intfs:
            self._run(['tc', 'qdisc', 'replace', 'dev', intf, 'root',
                       'netem', 'delay', f'{_num(delay_ms)}ms',
                       'reorder', f'{gap_percent}%', 'gap', str(gap_packets)])
        return intfs

    def _do_vlan_mismatch(self, intfs: List[str], params: Dict) -> List[str]:
        # ⚠️ 这版 ovs-ofctl **没有 mod-vlan 命令**，用 OVS 原生等价实现：
        #    发方端口打 tag，收方期望 untagged → 收不到，即 VLAN 不匹配。
        vlan_id = int(params.get('vlan_id', 999))
        applied = []
        for intf in intfs:
            self._run(['ovs-vsctl', 'set', 'port', intf, f'tag={vlan_id}'])
            applied.append(intf)
        return applied

    def _do_flow_race(self, intfs: List[str], params: Dict) -> List[str]:
        """流表竞态：并发改流表，制造「同一时刻两条流表变更」。"""
        rounds = int(params.get('rounds', 5))
        for intf in intfs:
            sw = intf.split('-eth')[0]
            m = _IFACE_RE.match(intf)
            port = m.group('port') if m else '1'

            def churn():
                subprocess.run(
                    ['sudo', '-n', 'ovs-ofctl', '-O', 'OpenFlow13', 'del-flows',
                     '--priority', '100', sw],
                    capture_output=True, timeout=CMD_TIMEOUT)
                subprocess.run(
                    ['sudo', '-n', 'ovs-ofctl', '-O', 'OpenFlow13', 'add-flow', sw,
                     'table=0,priority=100,idle_timeout=20,'
                     f'in_port={port},actions=NORMAL'],
                    capture_output=True, timeout=CMD_TIMEOUT)

            for _ in range(rounds):
                threads = [threading.Thread(target=churn) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        return intfs

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _resolve_interfaces(self, target: str,
                            params: Optional[Dict] = None) -> List[str]:
        """target → 接口名列表。

        - target 是具体接口（s1-eth1）→ 就是它自己
        - target 是交换机名（s1）→ 该交换机所有端口
        - params.port 指定了端口号 → 只取那一个
        """
        params = params or {}
        all_ports = self._all_switch_ports()

        if _IFACE_RE.match(target):
            # ⚠️ all_ports 是 {switch: [ports]} 字典，不能直接拿它做端口 `in` 判断
            #    （踩过：把 's1-eth1' 当字典 key 查 → 永远查不到 → 「接口不存在」）
            m = _IFACE_RE.match(target)
            ports_of_sw = all_ports.get(m.group('sw'), [])
            if target not in ports_of_sw:
                raise FaultInjectionError(
                    f"接口不存在: {target}（{m.group('sw')} 现有: "
                    f"{', '.join(ports_of_sw) or '无'}）")
            return [target]

        ports = all_ports.get(target, [])
        if not ports:
            raise FaultInjectionError(
                f"找不到交换机 '{target}' 的端口（现有: {', '.join(sorted(all_ports)) or '无'}）")

        want = params.get('port')
        if want is not None:
            wanted = f'{target}-eth{want}'
            if wanted not in ports:
                raise FaultInjectionError(f"{target} 上没有端口 {want}")
            return [wanted]
        return ports

    def _all_interfaces(self) -> List[str]:
        """所有交换机端口的**扁平列表**。

        ⚠️ 不要直接迭代 `_all_switch_ports()` —— 那是 {switch: [ports]} 字典，
           迭代它拿到的是 switch **名**（'s1'）而不是端口（'s1-eth1'），
           清理会「只清了 1 个假接口」（踩过）。

        ⚠️ 这里用 `self._all_switch_ports()` 而不是 `FaultApi._all_switch_ports()` ——
           后者绕过了实例覆盖，测试没法 mock（单测抓到的）。
        """
        ports = self._all_switch_ports()
        out: List[str] = []
        for sw in sorted(ports):
            out.extend(ports[sw])
        return out

    @staticmethod
    def _all_switch_ports() -> Dict[str, List[str]]:
        """扫描当前所有交换机端口 → {switch: [iface, ...]}。

        只认 `sN-ethM` 这种命名 —— Mininet 的命名约定。
        """
        out = FaultApi._run(['ip', '-br', 'link', 'show'], check=False)
        ports: Dict[str, List[str]] = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            name = line.split()[0].split('@')[0]
            m = _IFACE_RE.match(name)
            if m:
                ports.setdefault(m.group('sw'), []).append(name)
        for sw in ports:
            ports[sw].sort(key=lambda n: int(_IFACE_RE.match(n).group('port')))
        return ports

    @staticmethod
    def _run(cmd: List[str], check: bool = True) -> str:
        """跑一条特权命令。失败时抛 FaultInjectionError。"""
        full = ['sudo', '-n'] + cmd
        try:
            r = subprocess.run(full, capture_output=True, text=True,
                               timeout=CMD_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise FaultInjectionError(f"命令超时（{CMD_TIMEOUT}s）: {' '.join(cmd)}") from e
        if check and r.returncode != 0:
            raise FaultInjectionError(
                f"命令失败（rc={r.returncode}）: {' '.join(cmd)}\n"
                f"stderr: {r.stderr.strip()[:300]}")
        return r.stdout


# 模块级单例（HTTP 请求间共享活跃故障状态）
FAULT_API = FaultApi()
