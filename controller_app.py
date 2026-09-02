"""SDN 控制器：状态采集核心

事件钩子（os-ken OpenFlow 事件）:
  - EventOFPSwitchFeatures   → 交换机连接
  - EventOFPPortDescStatsReply → 端口描述（名称、状态、速度）
  - EventOFPPacketIn         → 包进入 → 主机追踪
  - EventOFPPortStatsReply   → 端口计数器（rx/tx 字节、丢包）
  - EventOFPFlowStatsReply   → 流表条目
  - EventOFPStateChange      → 交换机连接/断开

状态输出:
  - NETWORK_STATE (模块级 dict, 内存共享)
  - /tmp/network_state.json (每 5 秒 dump, 用于跨进程访问)
"""
import eventlet
eventlet.monkey_patch()  # 必须在 os_ken 之前

import json
import os
import time

import state_history

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER,
)
from os_ken.controller.handler import set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet as pkt_lib
from os_ken.lib.packet import ethernet, arp, ipv4
from os_ken.lib.packet import lldp


# ============ 全局状态（内存 + JSON 文件双写）============
NETWORK_STATE = {
    'meta': {
        'start_time': time.time(),
        'last_update': time.time(),
        'update_count': 0,
        'controller_pid': os.getpid(),
    },
    'switches': {},     # dpid_hex -> {ports, connected_at, ...}
    'hosts': {},        # mac -> {ipv4[], dpid, port, ...}
    'port_stats': {},   # dpid_hex -> [{port_no, rx_bytes, tx_bytes, ...}, ...]
    'flows': {},        # dpid_hex -> [{priority, match, actions, ...}, ...]
    'events': [],       # 最近事件（FIFO, 上限 500）
    'links': {},        # "dpid:port" -> {peer_dpid, peer_port, peer_name, last_seen}  (LLDP 发现)
}

STATE_FILE = '/tmp/network_state.json'
STATS_INTERVAL = 5   # 秒
LLDP_INTERVAL = 3    # 秒: LLDP 发送周期
LINK_TIMEOUT = 30    # 秒: 链路条目超时

# OpenFlow 常量
OFPPS_LINK_DOWN = 1 << 0
OFPP_LOCAL = 0xfffffffe


def _to_json_safe(obj):
    """递归把 set/tuple 转成 list, bytes 转 str."""
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(v) for v in obj]
    if isinstance(obj, set):
        return sorted([_to_json_safe(v) for v in obj])
    if isinstance(obj, bytes):
        try:
            return obj.decode()
        except Exception:
            return repr(obj)
    return obj


def dump_state_to_file():
    """原子写：写到 .tmp 再 rename,避免读到半截文件."""
    try:
        snapshot = {
            'meta': dict(NETWORK_STATE['meta']),
            'switches': _to_json_safe(NETWORK_STATE['switches']),
            'hosts': _to_json_safe(NETWORK_STATE['hosts']),
            'port_stats': _to_json_safe(NETWORK_STATE['port_stats']),
            'flows': _to_json_safe(NETWORK_STATE['flows']),
            'events': _to_json_safe(NETWORK_STATE['events'][-100:]),
            'links': _to_json_safe(NETWORK_STATE['links']),
        }
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(snapshot, f, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass  # 写文件失败不应影响控制器


class StateCollectorController(app_manager.OSKenApp):
    """状态采集控制器."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mac_to_port = {}     # dpid -> {mac: port}  L2 学习
        self.datapaths = {}       # dpid -> datapath 对象
        self.inter_switch_ports = set()  # {(dpid_hex, port_no)} 交换机间互联口
        self._log_event('controller_started', pid=os.getpid())

    # ---------- 启动钩子 ----------
    def start(self):
        super().start()
        eventlet.spawn(self._stats_loop)
        eventlet.spawn(self._lldp_loop)
        dump_state_to_file()

    # ---------- 交换机连接 ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """交换机握手完成: 注册 datapath, 装 table-miss, 拉端口描述."""
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # table-miss: 所有未匹配包送控制器
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                           ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        datapath.send_msg(parser.OFPFlowMod(
            datapath=datapath, priority=0,
            match=match, instructions=inst))

        self.datapaths[datapath.id] = datapath
        dpid_hex = format(datapath.id, '016x')

        if dpid_hex not in NETWORK_STATE['switches']:
            NETWORK_STATE['switches'][dpid_hex] = {
                'dpid': dpid_hex,
                'connected_at': time.time(),
                'num_ports': 0,
                'ports': [],
                'disconnected_at': None,
            }
            self._log_event('switch_connected', dpid=dpid_hex)
        else:
            # 重新连接: 清除断开标记 (可能发生过拓扑切换/控制器重启)
            NETWORK_STATE['switches'][dpid_hex]['connected_at'] = time.time()
            NETWORK_STATE['switches'][dpid_hex]['disconnected_at'] = None

        self._mark_updated()
        # 拉端口描述 + 首次 stats
        datapath.send_msg(parser.OFPPortDescStatsRequest(datapath))
        eventlet.spawn_after(2, self._request_stats, datapath)

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        datapath = ev.datapath
        dpid_hex = format(datapath.id, '016x')
        if ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(datapath.id, None)
            if dpid_hex in NETWORK_STATE['switches']:
                NETWORK_STATE['switches'][dpid_hex]['disconnected_at'] = time.time()
            self._log_event('switch_disconnected', dpid=dpid_hex)
            self._mark_updated()

    # ---------- 端口描述 ----------
    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_handler(self, ev):
        datapath = ev.msg.datapath
        dpid_hex = format(datapath.id, '016x')
        ports = []
        for p in ev.msg.body:
            ports.append({
                'port_no': p.port_no,
                'hw_addr': p.hw_addr,
                'name': p.name.decode() if isinstance(p.name, bytes) else p.name,
                'state': p.state,           # OFPPS_LINK_DOWN=1 << 0, etc.
                'config': p.config,
                'curr_speed': p.curr_speed,
                'max_speed': p.max_speed,
            })
        if dpid_hex in NETWORK_STATE['switches']:
            NETWORK_STATE['switches'][dpid_hex]['ports'] = ports
            NETWORK_STATE['switches'][dpid_hex]['num_ports'] = len(ports)
            self._mark_updated()

    # ---------- 端口状态变化 (故障检测关键) ----------
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        """端口 up/down 变化: 更新内存状态 + 立即 dump."""
        msg = ev.msg
        dpid_hex = format(msg.datapath.id, '016x')
        desc = msg.desc
        new_state = desc.state
        port_name = (desc.name.decode()
                     if isinstance(desc.name, bytes) else desc.name)

        ports = NETWORK_STATE['switches'].get(dpid_hex, {}).get('ports', [])
        for p in ports:
            if p.get('port_no') == desc.port_no:
                old_state = p.get('state', 0)
                p['state'] = new_state
                p['config'] = desc.config
                # 只在链路状态翻转时记事件
                if (old_state & OFPPS_LINK_DOWN) != (new_state & OFPPS_LINK_DOWN):
                    self._log_event(
                        'port_link_status',
                        dpid=dpid_hex, port_no=desc.port_no,
                        name=port_name,
                        down=bool(new_state & OFPPS_LINK_DOWN),
                        reason=msg.reason,
                    )
                    # 端口 down → 立即移除相关 LLDP 链路 (不等超时)
                    if new_state & OFPPS_LINK_DOWN:
                        self._remove_links_on_port(dpid_hex, desc.port_no)
                break

        self._mark_updated()
        dump_state_to_file()

    # ---------- LLDP 拓扑发现 ----------
    def _lldp_loop(self):
        """周期向所有数据口发送 LLDP 包, 让对端发现本端."""
        while True:
            eventlet.sleep(LLDP_INTERVAL)
            for dp in list(self.datapaths.values()):
                dpid_hex = format(dp.id, '016x')
                ports = NETWORK_STATE['switches'].get(dpid_hex, {}).get('ports', [])
                for p in ports:
                    if p.get('port_no') == OFPP_LOCAL:
                        continue
                    if p.get('state', 0) & OFPPS_LINK_DOWN:
                        continue  # down 端口不发
                    try:
                        data = self._build_lldp(dpid_hex, p['port_no'], p['hw_addr'])
                        out = dp.ofproto_parser.OFPPacketOut(
                            datapath=dp, buffer_id=dp.ofproto.OFP_NO_BUFFER,
                            in_port=dp.ofproto.OFPP_CONTROLLER,
                            actions=[dp.ofproto_parser.OFPActionOutput(
                                p['port_no'], 0)],
                            data=data)
                        dp.send_msg(out)
                    except Exception:
                        pass
            self._cleanup_links()

    @staticmethod
    def _build_lldp(dpid_hex, port_no, port_hw_addr):
        """构造 LLDP 帧: chassis=dpid, port=port_no, 源 MAC=端口自身."""
        pkt = pkt_lib.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=0x88cc,
            dst=lldp.LLDP_MAC_NEAREST_BRIDGE,
            src=port_hw_addr))
        pkt.add_protocol(lldp.lldp(tlvs=[
            lldp.ChassisID(subtype=lldp.ChassisID.SUB_LOCALLY_ASSIGNED,
                           chassis_id=dpid_hex.encode('ascii')),
            lldp.PortID(subtype=lldp.PortID.SUB_LOCALLY_ASSIGNED,
                        port_id=str(port_no).encode('ascii')),
            lldp.TTL(ttl=120),
            lldp.End(),
        ]))
        pkt.serialize()
        return pkt.data

    def _handle_lldp(self, pkt, dpid_hex, in_port):
        """收到 LLDP: 记录本端口 → 对端(dpid, port) 的链路."""
        try:
            lldp_pkt = pkt.get_protocol(lldp.lldp)
            if lldp_pkt is None:
                return
            chassis = port = None
            for tlv in lldp_pkt.tlvs:
                if isinstance(tlv, lldp.ChassisID):
                    chassis = tlv.chassis_id
                elif isinstance(tlv, lldp.PortID):
                    port = tlv.port_id
            if chassis is None or port is None:
                return
            peer_dpid = chassis.decode()
            peer_port = int(port.decode())
            # 对端端口名 (从对端端口表反查)
            peer_name = None
            for p in NETWORK_STATE['switches'].get(peer_dpid, {}).get('ports', []):
                if p.get('port_no') == peer_port:
                    peer_name = p.get('name')
            key = f'{dpid_hex}:{in_port}'
            cur = NETWORK_STATE['links'].get(key)
            is_new = (cur is None or cur.get('peer_dpid') != peer_dpid
                      or cur.get('peer_port') != peer_port)
            NETWORK_STATE['links'][key] = {
                'peer_dpid': peer_dpid,
                'peer_port': peer_port,
                'peer_name': peer_name,
                'last_seen': time.time(),
            }
            if is_new:
                self._log_event('link_discovered', dpid=dpid_hex, port=in_port,
                                peer_dpid=peer_dpid, peer_port=peer_port)
            self._mark_updated()
        except Exception:
            pass

    def _cleanup_links(self):
        """清理超时未刷新的链路 (对端断开/端口 down)."""
        now = time.time()
        stale = [k for k, v in NETWORK_STATE['links'].items()
                 if now - v.get('last_seen', 0) > LINK_TIMEOUT]
        for k in stale:
            dpid, port = k.split(':')
            self._log_event('link_lost', dpid=dpid, port=int(port))
            del NETWORK_STATE['links'][k]
        if stale:
            self._mark_updated()

    def _remove_links_on_port(self, dpid_hex, port_no):
        """端口 down: 移除本端条目 + 对端指向本端的条目."""
        key = f'{dpid_hex}:{port_no}'
        NETWORK_STATE['links'].pop(key, None)
        for k, v in list(NETWORK_STATE['links'].items()):
            if v.get('peer_dpid') == dpid_hex and v.get('peer_port') == port_no:
                del NETWORK_STATE['links'][k]
        self._mark_updated()

    # ---------- PacketIn: L2 学习 + 主机追踪 ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid_hex = format(datapath.id, '016x')

        pkt = pkt_lib.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]
        # LLDP: 拓扑发现 (交换机间链路)
        if eth.ethertype == 0x88cc:
            self._handle_lldp(pkt, dpid_hex, in_port)
            return

        src, dst = eth.src, eth.dst

        # 主机追踪
        if src != 'ff:ff:ff:ff:ff:ff':
            if self._is_interface_mac(src):
                # 接口自身 MAC (IPv6 DAD/NS 等): 该端口是对端交换机来的
                # → 标记为交换机间互联口, 不作为主机接入位置
                self.inter_switch_ports.add((dpid_hex, in_port))
            else:
                self._track_host(src, pkt, dpid_hex, in_port)

        # L2 学习 + 安装 flow
        self.mac_to_port.setdefault(datapath.id, {})
        self.mac_to_port[datapath.id][src] = in_port
        out_port = self.mac_to_port[datapath.id].get(dst, ofproto.OFPP_FLOOD)

        actions = [parser.OFPActionOutput(out_port)]
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            inst = [parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions)]
            datapath.send_msg(parser.OFPFlowMod(
                datapath=datapath, priority=1,
                match=match, instructions=inst, idle_timeout=30))

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        datapath.send_msg(parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data))

    def _is_interface_mac(self, mac: str) -> bool:
        """判断 MAC 是否是某个交换机端口的 hw_addr (即交换机接口自身)."""
        for sw in NETWORK_STATE['switches'].values():
            for p in sw.get('ports', []):
                if p.get('hw_addr') == mac:
                    return True
        return False

    def _track_host(self, mac, pkt, dpid_hex, port):
        # 交换机间互联口不是主机接入位置 (泛洪帧会在这里出现)
        if (dpid_hex, port) in self.inter_switch_ports:
            return
        host = NETWORK_STATE['hosts'].get(mac)
        if host is None:
            host = {
                'mac': mac,
                'ipv4': [],
                'dpid': dpid_hex,
                'port': port,
                'first_seen': time.time(),
                'last_seen': time.time(),
            }
            NETWORK_STATE['hosts'][mac] = host
            self._log_event('host_discovered', mac=mac, dpid=dpid_hex, port=port)
        else:
            host['last_seen'] = time.time()
            if host['dpid'] != dpid_hex or host['port'] != port:
                old = (host['dpid'], host['port'])
                host['dpid'] = dpid_hex
                host['port'] = port
                self._log_event('host_moved',
                                mac=mac, old=old, new=(dpid_hex, port))

        # 提取 IPv4
        if pkt.get_protocol(ipv4.ipv4) is not None:
            ip = pkt.get_protocol(ipv4.ipv4).src
            if ip and ip not in host['ipv4']:
                host['ipv4'].append(ip)
        # 提取 ARP
        if pkt.get_protocol(arp.arp) is not None:
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt.src_ip and arp_pkt.src_ip not in host['ipv4']:
                host['ipv4'].append(arp_pkt.src_ip)
        self._mark_updated()

    # ---------- 周期 stats 轮询 ----------
    def _stats_loop(self):
        while True:
            eventlet.sleep(STATS_INTERVAL)
            for dp in list(self.datapaths.values()):
                self._request_stats(dp)
            dump_state_to_file()
            self._record_history()

    def _request_stats(self, datapath):
        parser = datapath.ofproto_parser
        try:
            datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0))
            datapath.send_msg(parser.OFPFlowStatsRequest(datapath))
        except Exception as e:
            self.logger.warning(f"stats request failed: {e}")

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_handler(self, ev):
        dpid_hex = format(ev.msg.datapath.id, '016x')
        stats = [{
            'port_no': s.port_no,
            'rx_packets': s.rx_packets,
            'tx_packets': s.tx_packets,
            'rx_bytes': s.rx_bytes,
            'tx_bytes': s.tx_bytes,
            'rx_errors': s.rx_errors,
            'tx_errors': s.tx_errors,
            'rx_dropped': s.rx_dropped,
            'tx_dropped': s.tx_dropped,
            'duration_sec': s.duration_sec,
        } for s in ev.msg.body]
        NETWORK_STATE['port_stats'][dpid_hex] = stats
        self._mark_updated()
        dump_state_to_file()

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_handler(self, ev):
        dpid_hex = format(ev.msg.datapath.id, '016x')
        flows = [{
            'priority': f.priority,
            'cookie': f.cookie,
            'packet_count': f.packet_count,
            'byte_count': f.byte_count,
            'duration_sec': f.duration_sec,
            'idle_timeout': f.idle_timeout,
            'hard_timeout': f.hard_timeout,
            'match': str(f.match),
            'instructions': str(getattr(f, 'instructions', None)),
        } for f in ev.msg.body]
        NETWORK_STATE['flows'][dpid_hex] = flows
        self._mark_updated()
        dump_state_to_file()

    def _record_history(self):
        """把当前状态写入历史缓冲 (汇总 + 验证摘要 + 端口计数)."""
        try:
            now = time.time()
            # 1) 汇总
            rx = tx = 0
            down = 0
            for stats in NETWORK_STATE['port_stats'].values():
                for s in stats:
                    if s.get('port_no') == 4294967294:  # OFPP_LOCAL
                        continue
                    rx += s.get('rx_bytes', 0)
                    tx += s.get('tx_bytes', 0)
            for sw in NETWORK_STATE['switches'].values():
                for p in sw.get('ports', []):
                    if p.get('port_no') != 4294967294 and (p.get('state', 0) & 1):
                        down += 1
            state_history.add_summary({
                'time': now,
                'switches': len(NETWORK_STATE['switches']),
                'hosts': len(NETWORK_STATE['hosts']),
                'flows': sum(len(f) for f in NETWORK_STATE['flows'].values()),
                'rx_bytes': rx,
                'tx_bytes': tx,
                'ports_down': down,
            })

            # 2) 验证摘要 (供"自愈前后"时间线)
            try:
                from state_model import validate_state
                report = validate_state(NETWORK_STATE)
                state_history.add_validation({
                    'time': now,
                    'ok': report['ok'],
                    'errors': report['by_level']['error'],
                    'warnings': report['by_level']['warning'],
                    'infos': report['by_level']['info'],
                    'issues': [i['message'] for i in report['issues'][:5]],
                })
            except Exception:
                pass

            # 3) 端口计数历史 (数据口)
            snapshot = []
            for dpid, stats in NETWORK_STATE['port_stats'].items():
                names = {p.get('port_no'): p.get('name')
                         for p in NETWORK_STATE['switches'].get(dpid, {}).get('ports', [])}
                for s in stats:
                    if s.get('port_no') == 4294967294:
                        continue
                    snapshot.append({
                        'time': now, 'dpid': dpid,
                        'port_no': s.get('port_no'),
                        'name': names.get(s.get('port_no')),
                        'rx_bytes': s.get('rx_bytes', 0),
                        'tx_bytes': s.get('tx_bytes', 0),
                        'rx_dropped': s.get('rx_dropped', 0),
                        'tx_dropped': s.get('tx_dropped', 0),
                    })
            state_history.add_port_stats(snapshot)
            state_history.dump()
        except Exception:
            pass

    # ---------- 工具 ----------
    def _log_event(self, event_type, **kwargs):
        NETWORK_STATE['events'].append({
            'time': time.time(),
            'type': event_type,
            **kwargs,
        })
        if len(NETWORK_STATE['events']) > 500:
            NETWORK_STATE['events'] = NETWORK_STATE['events'][-500:]

    def _mark_updated(self):
        NETWORK_STATE['meta']['last_update'] = time.time()
        NETWORK_STATE['meta']['update_count'] += 1
