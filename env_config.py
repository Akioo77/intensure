"""env_config.py — 环境配置层（v3.21.0）

为什么要这层
------------
采集层原先把「本地 Mininet 实验环境」写死了 7 处：拓扑文件、IP 段、
交换机命名、探测方式（mnexec）、注故障方式（tc 打本机网卡）、状态校验基线
（EXPECTED_TOPOLOGY 写死 2 交换机 4 主机）、端口名格式（sN-ethM）。

结果是：换一次环境（共享环境 / 真实 OpenFlow 网络 / 别人配的 Mininet）就要
改一轮代码，而且往往是在赶演示的时候改。

这层把「环境差异」从代码搬到配置文件：
  - `env.json`（标准库就能读，CI 也能跑）
  - `env.yaml`（若装了 PyYAML 也能读，装了没装都行）
  - 路径可用环境变量 `INTENSURE_ENV_FILE` 覆盖

**关键性质：配置缺失时用内置默认值，而默认值 == 改造前的行为。**
所以开这个功能不会改变任何现有行为，回归测试照常全绿。

配置示例见仓库根目录 `env.json`。
"""
import json
import os
from typing import Any, Dict, List, Optional

# 项目根目录（本文件所在目录）
_HERE = os.path.dirname(os.path.abspath(__file__))

# 配置文件查找顺序：显式路径 → 环境变量 → env.json → env.yaml
CONFIG_ENV_VAR = 'INTENSURE_ENV_FILE'
CONFIG_FILENAMES = ('env.json', 'env.yaml')

# 模式：
#   active     —— 允许本模块做写操作（注故障等），当前实验环境用这个
#   read-only  —— 显式只读：任何写操作端点一律拒绝（接真实网络时用这个）
MODE_ACTIVE = 'active'
MODE_READ_ONLY = 'read-only'

# 探测驱动
PROBE_MININET = 'mininet'
PROBE_ICMP = 'icmp'
PROBE_SNMP = 'snmp'
PROBE_CLI = 'cli'

# 内置默认值 —— 与改造前代码里的硬编码常量一一对应
DEFAULT_CONFIG: Dict[str, Any] = {
    # 环境名（给人看的标签，进 capabilities 输出）
    'environment': 'mininet-lab',

    # 模式：active | read-only
    'mode': MODE_ACTIVE,

    # 是否允许控制器向交换机下发流表规则。
    # ⚠️ 这是接真实网络时最关键的开关：controller_app 握手时装 table-miss
    #    （所有未匹配包送控制器）、学到 MAC 后装学习规则 —— 它本质上是个
    #    **L2 学习交换机**，指向真实园区交换机 = 接管其转发路径。
    # 默认 True = 改造前行为（本机实验环境照常跑）。
    'flow_rule_write': True,

    # 期望拓扑 —— 校验 completeness 用。
    # 默认对应 start_all.sh / demo_db_pipeline.sh 起的 SingleSwitchTopo(k=4)
    # （1 交换机 4 主机）。2 交换机那套用 env.topo_simple.json。
    # ⚠️ state['links'] 只记交换机之间的链路（LLDP 发现），主机连交换机的
    #    链路不在里面 —— 所以「交换机间链路数」看 inter_switch_links。
    'topology_expect': {
        'switches': 1,
        'hosts': 4,
        'inter_switch_links': 0,
        'host_switch_links': 4,
        'total_links': 4,
        'expected_host_ips': ['10.0.0.1', '10.0.0.2', '10.0.0.3', '10.0.0.4'],
    },

    # dpid(hex) → 设备名。留空 = 用 dpid 末位推断（Mininet 跨机天然一致）
    'dpid_to_switch': {},

    # 真实端口名 → 契约短标签。真实设备是 GigabitEthernet0/1 这种长名
    'port_label_map': {},

    # 探测参数
    'probe': {
        'driver': PROBE_MININET,
        'source': '10.0.0.1',
        'destination': '10.0.0.4',
    },

    # 接入目标 —— 「要在哪台设备上执行动作」（探测 / 注故障 / 读设备日志）
    #
    # type=local：就在本机执行（改造前的默认行为，演示/自测照旧）
    # type=ssh  ：通过 ssh 在目标主机上执行 —— 联调时 Mininet 是对方起的，
    #             或者采集机与设备不在同一台机器
    #
    # ⚠️ 采集本身（OpenFlow / LLDP / 状态 / 契约 HTTP / DB）本来就是跨机的，
    #    不受这个配置影响。这里只管「要在设备上动手」的那部分。
    'target': {
        'type': 'local',
        'host': '',
        'user': '',
        'port': 22,
        'key': '',
        'connect_timeout': 5,
    },

    # 控制器写状态文件、REST API 读状态文件 —— 两者必须指向同一个路径。
    # 换机器部署时把它指到共享盘，或让控制器与 API 同机部署。
    'state_file': '/tmp/network_state.json',

    # 是否允许注故障（真实网络必须 false）
    'allow_fault_injection': True,

    # 期望的对方契约版本（覆盖 assurance_bridge 的默认值）
    'assurance_expected_version': None,
}

# 配置解析失败时的安全兜底：强制只读。
# 理由：用户本想开只读（接真实网络），配置写坏了却"回退成能写"是事故。
# 配置坏了 → 宁可什么都不写。
_SAFE_FALLBACK = {
    'mode': MODE_READ_ONLY,
    'allow_fault_injection': False,
    # 配置坏了 → 宁可不写流表（接管转发路径比采不到数据危险得多）
    'flow_rule_write': False,
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 合并进 base（嵌套 dict 逐层合并）。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _strip_json_comments(text: str) -> str:
    """去掉 JSON 里的 // 注释（不在字符串内的才算）。

    手写配置很爱加注释，而标准 json 不支持 —— 注释必须能被忽略，
    否则用户写错一个注释整个配置就废了（还可能静默回退到默认值）。
    """
    out = []
    in_string = False
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        # 不在字符串里
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            # 跳到行尾（保留换行，避免把两行拼成一行）
            while i < n and text[i] != '\n':
                i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _parse_json(text: str) -> Dict[str, Any]:
    """解析 JSON（允许 // 注释，方便手写配置）。"""
    return json.loads(_strip_json_comments(text))


def _parse_yaml(text: str) -> Dict[str, Any]:
    """解析 YAML（需要 PyYAML；没装就报错，由调用方回退）。"""
    import yaml  # 延迟导入：没装 PyYAML 也不影响用 JSON
    return yaml.safe_load(text) or {}


class EnvConfig:
    """一份环境配置。"""

    def __init__(self, data: Optional[Dict[str, Any]] = None,
                 source: str = 'built-in defaults'):
        self._data = _deep_merge(DEFAULT_CONFIG, data or {})
        self.source = source

    # ------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[str] = None) -> 'EnvConfig':
        """加载配置。

        顺序：显式 path → $INTENSURE_ENV_FILE → env.json → env.yaml。

        ⚠️ **fail-safe**：显式指定的配置文件解析失败时，
        **不**静默回退到仓库里的 env.json（那可能把用户想要的只读模式
        变成可写模式）。改成用内置默认值 + 强制只读，并大声告警。
        配置文件写坏 ≠ 允许写操作。
        """
        explicit = []
        if path:
            explicit.append(path)
        env_path = os.environ.get(CONFIG_ENV_VAR)
        if env_path:
            explicit.append(env_path)

        candidates = explicit + [os.path.join(_HERE, name) for name in CONFIG_FILENAMES]

        for index, candidate in enumerate(candidates):
            if not candidate or not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, 'r', encoding='utf-8') as fh:
                    text = fh.read()
            except OSError:
                continue
            try:
                if candidate.endswith(('.yaml', '.yml')):
                    data = _parse_yaml(text)
                else:
                    data = _parse_json(text)
            except Exception as exc:  # 解析失败不该让整个服务起不来
                import logging
                logging.getLogger(__name__).error(
                    f'环境配置 {candidate} 解析失败（{exc}）—— '
                    f'改用内置默认值并强制只读模式（写操作已禁用）')
                return cls(_SAFE_FALLBACK, source=f'broken: {candidate}')
            if not isinstance(data, dict):
                logging.getLogger(__name__).error(
                    f'环境配置 {candidate} 顶层不是 dict —— '
                    f'改用内置默认值并强制只读模式（写操作已禁用）')
                return cls(_SAFE_FALLBACK, source=f'broken: {candidate}')
            return cls(data, source=candidate)
        return cls({}, source='built-in defaults')

    # ------------------------------------------------------------
    # 字段
    # ------------------------------------------------------------

    @property
    def data(self) -> Dict[str, Any]:
        """合并后的完整配置。"""
        return self._data

    @property
    def environment(self) -> str:
        return str(self._data.get('environment') or 'unknown')

    @property
    def mode(self) -> str:
        mode = str(self._data.get('mode') or MODE_ACTIVE).strip().lower()
        return mode if mode in (MODE_ACTIVE, MODE_READ_ONLY) else MODE_ACTIVE

    @property
    def is_read_only(self) -> bool:
        """是否处于显式只读模式。"""
        return self.mode == MODE_READ_ONLY

    @property
    def write_operations_allowed(self) -> bool:
        """是否允许本模块做写操作（注故障等）。

        只读模式下永远为 False —— 这是「采集层不改变现网」这条承诺的执行点。
        ⚠️ 注意（2026-11-03 订正）：这个开关管的是**我们自己暴露的写端点**，
        **管不到 OpenFlow 侧** —— controller_app 会下发 OFPFlowMod（见
        `flow_rule_write`）。原注释「OpenFlow 侧本来就不下发流表」是错的。
        """
        return not self.is_read_only and bool(self._data.get('allow_fault_injection', True))

    @property
    def flow_rule_write(self) -> bool:
        """是否允许控制器向交换机下发流表规则。

        🔴 **接真实网络时最关键的开关**（2026-11-03 加）。

        controller_app 握手时会装 **table-miss 规则**（priority 0，所有未匹配包送控制器），
        学到 MAC 之后装**学习规则**（priority 1，idle_timeout 30）——
        也就是说**它本质上是个 L2 学习交换机**。
        把它指向真实园区交换机，等于**接管那台交换机的转发路径** ——
        这比注故障严重得多（`allow_fault_injection` 只挡住了后者）。

        规则：
        - **只读模式永远为 False**，不能被配置覆盖
          （只读模式的语义就是「不写现网」）
        - active 模式取配置值，默认 True（= 改造前行为，演示环境不受影响）
        - 关掉之后：交换机不再把未匹配包送回控制器 → 没有 packet-in →
          **拿不到 MAC 级观测**（链路/端口状态仍可采，走 LLDP + port stats）
        """
        if self.is_read_only:
            return False
        return bool(self._data.get('flow_rule_write', True))

    @property
    def allow_fault_injection(self) -> bool:
        """是否允许注故障（与 write_operations_allowed 同义，语义更直白）。"""
        return self.write_operations_allowed

    @property
    def topology_expect(self) -> Dict[str, Any]:
        """期望拓扑（喂给 state_model 校验）。"""
        return dict(self._data.get('topology_expect') or {})

    @property
    def dpid_to_switch(self) -> Dict[str, str]:
        """dpid(hex) → 设备名映射（留空 = 按 dpid 末位推断）。"""
        return dict(self._data.get('dpid_to_switch') or {})

    @property
    def port_label_map(self) -> Dict[str, str]:
        """真实端口名 → 契约短标签。"""
        return dict(self._data.get('port_label_map') or {})

    @property
    def probe(self) -> Dict[str, Any]:
        """探测配置。"""
        return dict(self._data.get('probe') or {})

    @property
    def probe_driver(self) -> str:
        return str(self.probe.get('driver') or PROBE_MININET)

    @property
    def probe_source(self) -> str:
        """探测源。空 = 从目标主机自身发起（真实设备场景）。"""
        return str(self.probe.get('source') or '')

    @property
    def probe_destination(self) -> str:
        """探测目标。"""
        return str(self.probe.get('destination') or '')

    @property
    def probe_snmp(self) -> Dict[str, Any]:
        """SNMP 驱动的子配置（host / port / community / oid / timeout）。"""
        return dict(self.probe.get('snmp') or {})

    @property
    def probe_cli(self) -> Dict[str, Any]:
        """CLI 驱动的子配置（command 模板 + 两个正则）。"""
        return dict(self.probe.get('cli') or {})

    @property
    def target(self) -> Dict[str, Any]:
        """接入目标配置（type/host/user/port/key/connect_timeout）。

        决定「探测 / 注故障 / 读设备日志」这些动作在哪台机器上执行。
        采集链路（OpenFlow / 状态 / 契约 HTTP / DB）不受它影响。
        """
        return dict(self._data.get('target') or {})

    @property
    def state_file(self) -> str:
        """状态文件路径（控制器写、REST API 读）。"""
        return str(self._data.get('state_file') or '/tmp/network_state.json')

    @property
    def assurance_expected_version(self) -> Optional[str]:
        """期望的对方契约版本（None = 用 assurance_bridge 的默认值）。"""
        value = self._data.get('assurance_expected_version')
        return str(value) if value else None

    # ------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------

    def describe(self) -> Dict[str, Any]:
        """摘要（给 /api/capabilities 用，不含敏感信息）。"""
        return {
            'environment': self.environment,
            'mode': self.mode,
            'read_only': self.is_read_only,
            'write_operations_allowed': self.write_operations_allowed,
            'flow_rule_write': self.flow_rule_write,
            'config_source': self.source,
            'probe_driver': self.probe_driver,
            'target_type': str(self.target.get('type') or 'local'),
            'target_host': str(self.target.get('host') or ''),
            'state_file': self.state_file,
            'topology_expect': self.topology_expect,
            'has_dpid_mapping': bool(self.dpid_to_switch),
            'has_port_label_map': bool(self.port_label_map),
        }


# ============================================================
# 全局单例（懒加载 + 缓存）
# ============================================================

_config: Optional[EnvConfig] = None


def get_config(reload: bool = False) -> EnvConfig:
    """拿全局配置（第一次调用时才读文件）。"""
    global _config
    if _config is None or reload:
        _config = EnvConfig.load()
    return _config


def reset_cache() -> None:
    """清缓存（测试用）。"""
    global _config
    _config = None


def config_path_candidates() -> List[str]:
    """当前会按顺序尝试的配置文件路径（给 env_doctor 展示用）。"""
    out = []
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        out.append(env_path)
    out.extend(os.path.join(_HERE, name) for name in CONFIG_FILENAMES)
    return out
