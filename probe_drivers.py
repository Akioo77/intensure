"""probe_drivers.py —— 探测驱动适配器（多协议接入的最后一块）

## 为什么需要这层
------------------------------------------------------------------------
`env.json` 里一直有个 `probe.driver` 配置项，但**运行期没人读它** ——
探测一直是写死的 Mininet 进程内 ping。文档里写「两个分支代码里已有」是不准确的。

真实园区里能用的探测手段至少有四种：

    mininet   进 Mininet 主机命名空间发 ping（实验室环境）
    icmp      直接 ping（目标主机自身或任意可达地址）
    snmp      SNMPv2c GetRequest —— 工业界判断设备存活的standard做法
    cli       设备厂商 CLI（华为/H3C/Cisco 各不相同，需要模板）

这一层把「怎么测」和「测什么」分开：
- **测什么**（源/目标）→ `probe.source` / `probe.destination`
- **怎么测**（驱动）→ `probe.driver`

## 统一输出
------------------------------------------------------------------------
所有驱动都返回 `ProbeResult`，字段与 `intensure.probe_results` 表对齐：

    reachable   布尔 —— 目标是否可达
    loss_pct    丢包率（%）
    latency_ms  时延（ms），拿不到就是 None
    method      驱动名（会写进 DB 的 probe_method 列）
    detail      诊断细节（出错原因、原始输出片段）

## 约定
------------------------------------------------------------------------
- 驱动**不自己造 socket**：能走 ExecTarget 的都走（跨机场景要在目标上执行）
- SNMP 是唯一例外（它是 UDP 协议，不走 shell）
- 每个驱动都要能回答「我在这儿能用吗」（`available()`）——
  `env_doctor.py` 靠它做体检，缺依赖时要说清楚缺什么
"""
from __future__ import annotations

import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 驱动名常量（与 env_config.PROBE_MININET / PROBE_ICMP 保持一致）
DRIVER_MININET = 'mininet'
DRIVER_ICMP = 'icmp'
DRIVER_SNMP = 'snmp'
DRIVER_CLI = 'cli'

SUPPORTED_DRIVERS = (DRIVER_MININET, DRIVER_ICMP, DRIVER_SNMP, DRIVER_CLI)


@dataclass
class ProbeResult:
    """一次探测的结果。"""

    reachable: bool
    loss_pct: float = 100.0
    latency_ms: Optional[float] = None
    method: str = ''
    detail: str = ''
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'reachable': self.reachable,
            'loss_pct': self.loss_pct,
            'latency_ms': self.latency_ms,
            'method': self.method,
            'detail': self.detail,
            **self.extra,
        }


class ProbeDriver:
    """探测驱动基类。"""

    name = 'base'

    # 驱动自己的配置在 probe 段里的子键（snmp / cli），None = 直接用 probe 段
    config_section: Optional[str] = None

    def __init__(self, cfg: Optional[Dict[str, Any]] = None,
                 target=None):
        self.cfg = dict(cfg or {})
        self._target = target

    def opt(self, key: str, default: Any = None) -> Any:
        """读一个配置项。

        兼容两种传法：
          - 直接传子配置（{'host': ...}）→ 从顶层取
          - 传整段 probe（{'snmp': {'host': ...}}）→ 从子段取
        这样工厂传什么都能用，不会出现「配置写了但驱动读不到」的静默失效。
        """
        if key in self.cfg:
            return self.cfg[key]
        if self.config_section:
            sub = self.cfg.get(self.config_section)
            if isinstance(sub, dict) and key in sub:
                return sub[key]
        return default

    @property
    def target(self):
        """执行目标（要下命令的机器）。"""
        if self._target is None:
            import exec_target
            self._target = exec_target.target_from_env_config()
        return self._target

    def describe(self) -> str:
        return self.name

    def available(self) -> Tuple[bool, str]:
        """当前环境能不能用这个驱动 → (能不能, 原因/说明)。"""
        return True, '可用'

    def probe(self, source: str, destination: str,
              timeout: float = 6.0) -> ProbeResult:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_ping(text: str) -> Tuple[float, Optional[float]]:
        from exec_target import parse_ping_output
        return parse_ping_output(text)


# ====================================================================
# ICMP —— 直接 ping
# ====================================================================

class IcmpDriver(ProbeDriver):
    """直接 ping（从接入目标主机自身发起）。

    真实园区场景：采集机与被测设备同网段时这就是最直接的探测。
    """

    name = DRIVER_ICMP

    def available(self) -> Tuple[bool, str]:
        import shutil
        if shutil.which('ping'):
            return True, 'ping 可用'
        return False, '系统里没有 ping 命令'

    def probe(self, source: str, destination: str,
              timeout: float = 6.0) -> ProbeResult:
        if not destination:
            return ProbeResult(False, method=self.name, detail='没配 destination')
        r = self.target.run(['ping', '-c', '3', '-W', '1', destination],
                            timeout=timeout)
        loss, latency = self._parse_ping(r.output)
        ok = r.ok and loss < 100.0
        return ProbeResult(reachable=ok, loss_pct=loss, latency_ms=latency,
                            method=self.name,
                            detail='' if ok else (r.stderr or r.output or 'ping 失败')[:200])


# ====================================================================
# Mininet —— 进主机命名空间 ping
# ====================================================================

class MininetDriver(ProbeDriver):
    """进 Mininet 主机命名空间 ping（实验室环境）。

    源用**主机名**（如 h1），适配层负责解析成命名空间 PID 并 `mnexec -a <pid>`。
    """

    name = DRIVER_MININET

    def available(self) -> Tuple[bool, str]:
        import importlib.util
        import shutil
        if importlib.util.find_spec('mininet') is None:
            return False, 'mininet 没装（VM 内 apt install mininet）'
        if shutil.which('mnexec') is None:
            return False, 'mnexec 不在 PATH 里（Mininet 自带）'
        return True, 'mininet + mnexec 可用'

    def probe(self, source: str, destination: str,
              timeout: float = 6.0) -> ProbeResult:
        if not destination:
            return ProbeResult(False, method=self.name, detail='没配 destination')
        # source 是主机名（h1）；空 → 从目标主机自身发起
        loss, latency = self.target.ping(source, destination, count=3, timeout=timeout)
        return ProbeResult(reachable=(loss < 100.0), loss_pct=loss,
                            latency_ms=latency, method=self.name,
                            detail='' if loss < 100.0 else '目标不可达')


# ====================================================================
# CLI —— 设备厂商命令行探测
# ====================================================================

class CliDriver(ProbeDriver):
    """设备厂商 CLI 探测（模板驱动）。

    配置（env.json 的 probe.cli）：

        "cli": {
          "command": "ping -c 3 {dst}",        // {dst} {src} 会被替换
          "loss_regex": "([\\\\d.]+)% packet loss",
          "latency_regex": "avg[^=]*= ([\\\\d.]+)"
        }

    没有配正则时，按「命令成功即可达」处理。
    """

    name = DRIVER_CLI
    config_section = 'cli'

    def available(self) -> Tuple[bool, str]:
        if not self.opt('command'):
            return False, "probe.cli.command 没配（设备 CLI 探测需要命令模板）"
        return True, 'CLI 模板已配置'

    def probe(self, source: str, destination: str,
              timeout: float = 10.0) -> ProbeResult:
        template = str(self.opt('command') or '')
        if not template:
            return ProbeResult(False, method=self.name, detail='没配 CLI 命令模板')
        cmd = template.replace('{dst}', destination).replace('{src}', source or '')
        r = self.target.run(cmd.split(), timeout=timeout)
        out = r.output
        loss = 100.0
        latency = None
        m = re.search(str(self.opt('loss_regex') or r'([\d.]+)%\s*packet\s*loss'),
                      out)
        if m:
            loss = float(m.group(1))
        m2 = re.search(str(self.opt('latency_regex') or r'avg[^=]*=\s*([\d.]+)'), out)
        if m2:
            latency = float(m2.group(1))
        reachable = r.ok and loss < 100.0
        return ProbeResult(reachable=reachable, loss_pct=loss, latency_ms=latency,
                            method=self.name,
                            detail='' if reachable else out[:200])


# ====================================================================
# SNMP —— SNMPv2c GetRequest（纯标准库，无第三方依赖）
# ====================================================================

# --- BER/DER 编码 ---

def _ber_len(length: int) -> bytes:
    """BER 长度编码（短格式 / 长格式）。"""
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(raw)]) + raw


def _ber_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _ber_len(len(value)) + value


def _ber_int(value: int) -> bytes:
    """INTEGER 的 BER 编码（tag 0x02 + 长度 + 值，符号位要补）。"""
    if value == 0:
        return _ber_tlv(0x02, b'\x00')
    raw = value.to_bytes((value.bit_length() + 8) // 8, 'big')
    return _ber_tlv(0x02, raw)


def _ber_oid(oid: str) -> bytes:
    """OID 的 BER 编码（第一个子标识符特殊处理）。"""
    parts = [int(p) for p in oid.strip('.').split('.')]
    if len(parts) < 2:
        raise ValueError(f'OID 不合法: {oid}')
    body = bytearray()
    body.append(parts[0] * 40 + parts[1])
    for p in parts[2:]:
        if p < 0:
            raise ValueError(f'OID 子标识符不能为负: {oid}')
        chunk = bytearray([p & 0x7F])
        p >>= 7
        while p:
            chunk.insert(0, (p & 0x7F) | 0x80)
            p >>= 7
        body += chunk
    return _ber_tlv(0x06, bytes(body))


def _ber_sequence(*items: bytes) -> bytes:
    return _ber_tlv(0x30, b''.join(items))


def build_snmp_get(oid: str, community: str = 'public', req_id: int = 1) -> bytes:
    """构造一个 SNMPv2c GetRequest 报文。"""
    varbind = _ber_sequence(_ber_oid(oid), _ber_tlv(0x05, b''))   # Null 值
    pdu = _ber_tlv(0xA0,                                  # GetRequest
                   _ber_int(req_id) +
                   _ber_int(0) +                        # error-status
                   _ber_int(0) +                        # error-index
                   _ber_sequence(varbind))
    msg = _ber_sequence(_ber_int(1),                      # version = v2c
                        _ber_tlv(0x04, community.encode()),  # community
                        pdu)
    return msg


def parse_snmp_response(data: bytes) -> Dict[str, Any]:
    """解析 SNMP 响应 → {'oid':..., 'value': int|str, 'error': str|None}。

    只支持最常见的几种值类型（Integer / Counter / Gauge / TimeTicks /
    OctetString / OID），够用了 —— 我们要的是「设备是否应答 + 取一个计数值」。
    """
    def read_tlv(buf: bytes, pos: int) -> Tuple[int, bytes, int]:
        if pos >= len(buf):
            raise ValueError('报文截断')
        tag = buf[pos]
        pos += 1
        first = buf[pos]
        pos += 1
        if first < 0x80:
            length = first
        else:
            n = first & 0x7F
            length = int.from_bytes(buf[pos:pos + n], 'big')
            pos += n
        return tag, buf[pos:pos + length], pos + length

    try:
        tag, body, _ = read_tlv(data, 0)
        if tag != 0x30:
            raise ValueError('不是 SNMP message')
        pos = 0
        _, version, pos = read_tlv(body, pos)
        _, community, pos = read_tlv(body, pos)
        tag, pdu_body, _ = read_tlv(body, pos)
        if tag != 0xA2:                       # GetResponse
            raise ValueError(f'不是 GetResponse（tag=0x{tag:02X}）')
        pos = 0
        _, req_id, pos = read_tlv(pdu_body, pos)
        _, err_status, pos = read_tlv(pdu_body, pos)
        _, err_index, pos = read_tlv(pdu_body, pos)
        # varbind list
        _, varbinds, _ = read_tlv(pdu_body, pos)
        vpos = 0
        _, vb, _ = read_tlv(varbinds, vpos)
        vpos = 0
        _, oid_tlv, vpos = read_tlv(vb, vpos)
        val_tag, val_bytes, vpos = read_tlv(vb, vpos)
        value: Any = None
        if val_tag in (0x02, 0x41, 0x42, 0x43, 0x46):   # Integer/Counter/Gauge/TimeTicks
            value = int.from_bytes(val_bytes, 'big', signed=(val_tag == 0x02))
        elif val_tag == 0x04:                            # OctetString
            value = val_bytes.decode('utf-8', 'replace')
        elif val_tag == 0x06:                            # OID
            value = val_bytes.hex()
        err_status = int.from_bytes(err_status, 'big')
        if err_status:
            return {'oid': oid_tlv.hex(), 'value': None,
                    'error': f'agent 返回 error-status={err_status}'}
        return {'oid': oid_tlv.hex(), 'value': value, 'error': None}
    except Exception as e:
        return {'oid': '', 'value': None, 'error': f'响应解析失败: {e}'}


class SnmpDriver(ProbeDriver):
    """SNMPv2c 探测 —— 设备是否应答 + 取一个 OID 的值。

    语义：**能应答 = 设备存活**（工业界判断设备可达的standard做法）。
    探测结果里的 latency 是这次 SNMP 请求的往返时延。

    配置（env.json 的 probe.snmp）：

        "snmp": {
          "host": "10.0.0.2",
          "port": 161,
          "community": "public",
          "oid": "1.3.6.1.2.1.1.3.0",     // 默认 sysUpTime
          "timeout": 3
        }
    """

    name = DRIVER_SNMP
    config_section = 'snmp'

    DEFAULT_OID = '1.3.6.1.2.1.1.3.0'      # sysUpTime.0

    def available(self) -> Tuple[bool, str]:
        host = str(self.opt('host') or '')
        if not host:
            return False, "probe.snmp.host 没配（SNMP 探测需要设备地址）"
        return True, f'SNMP {host}:{self.opt("port", 161)}'

    def probe(self, source: str, destination: str,
              timeout: float = 6.0) -> ProbeResult:
        host = str(self.opt('host') or destination or '')
        if not host:
            return ProbeResult(False, method=self.name,
                                detail='没配 snmp.host 也没有 destination')
        port = int(self.opt('port', 161) or 161)
        community = str(self.opt('community') or 'public')
        oid = str(self.opt('oid') or self.DEFAULT_OID)
        sock_timeout = float(self.opt('timeout', timeout) or timeout)

        req = build_snmp_get(oid, community, req_id=int(time.time()) % 65535)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(sock_timeout)
        t0 = time.monotonic()
        try:
            sock.sendto(req, (host, port))
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            return ProbeResult(False, loss_pct=100.0, method=self.name,
                                detail=f'SNMP 超时（{sock_timeout}s，无应答）')
        except OSError as e:
            return ProbeResult(False, loss_pct=100.0, method=self.name,
                                detail=f'SNMP 发送失败: {e}')
        finally:
            sock.close()
        latency = (time.monotonic() - t0) * 1000

        parsed = parse_snmp_response(data)
        if parsed.get('error'):
            return ProbeResult(False, loss_pct=100.0, latency_ms=round(latency, 3),
                                method=self.name, detail=str(parsed['error']))
        return ProbeResult(reachable=True, loss_pct=0.0,
                            latency_ms=round(latency, 3), method=self.name,
                            detail='SNMP 应答正常',
                            extra={'snmp_oid': oid, 'snmp_value': parsed.get('value')})


# ====================================================================
# 工厂
# ====================================================================

DRIVER_CLASSES = {
    DRIVER_MININET: MininetDriver,
    DRIVER_ICMP: IcmpDriver,
    DRIVER_SNMP: SnmpDriver,
    DRIVER_CLI: CliDriver,
}


def build_driver(name: str, cfg: Optional[Dict[str, Any]] = None,
                 target=None) -> ProbeDriver:
    """按名字造驱动。未知名字 → 退回 icmp（至少能测点什么）。"""
    key = (name or '').strip().lower()
    cls = DRIVER_CLASSES.get(key, IcmpDriver)
    return cls(cfg=cfg, target=target)


def driver_from_env_config(target=None) -> ProbeDriver:
    """从 env.json 读 probe 配置并造驱动。

    驱动拿到的配置里会**同时**带上整段 probe 和自己的子段（snmp / cli），
    驱动内部用 `opt()` 取值 —— 两种形状都能用，不会出现
    「配置写了但驱动读不到」的静默失效（这个坑实测踩过一次）。

    读配置失败 → 退回 mininet 驱动（保持改造前行为）。
    """
    try:
        import env_config
        cfg = env_config.get_config()
        probe_cfg = dict(cfg.probe or {})
        driver_name = str(cfg.probe_driver or DRIVER_MININET)
        return build_driver(driver_name, probe_cfg, target=target)
    except Exception as e:
        logger.warning('[probe] 读配置失败，用 mininet 驱动: %s', e)
        return build_driver(DRIVER_MININET, {}, target=target)


def list_drivers() -> List[Dict[str, Any]]:
    """所有驱动 + 当前环境下能不能用（给 /api/capabilities 用）。

    ⚠️ 必须把**真实子配置**传给驱动 —— 之前一律用 `cls(cfg={})` 空配置造，
    于是 SnmpDriver 永远报「probe.snmp.host 没配」、CliDriver 永远报
    「probe.cli.command 没配」，哪怕 env.json 里已经配好了。
    （而 env_doctor 传了真实配置 → 同一个驱动两个工具给出矛盾结论。）
    """
    sub_cfgs: Dict[str, Dict[str, Any]] = {}
    try:
        import env_config
        cfg = env_config.get_config()
        sub_cfgs[DRIVER_SNMP] = dict(cfg.probe_snmp or {})
        sub_cfgs[DRIVER_CLI] = dict(cfg.probe_cli or {})
    except Exception as e:
        logger.warning('[probe] 读配置失败，驱动可用性按未配置判定: %s', e)

    out = []
    for name, cls in DRIVER_CLASSES.items():
        try:
            driver = cls(cfg=sub_cfgs.get(name, {}))
            ok, reason = driver.available()
        except Exception as e:
            ok, reason = False, f'检查失败: {e}'
        out.append({'name': name, 'available': ok, 'reason': reason})
    return out
