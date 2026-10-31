"""capabilities.py — 环境能力自检（v3.21.0）

回答一个问题：**当前这个环境，我们这个采集层到底能干什么？**

为什么要它
----------
最终验收环境不确定（三个人可能各配各的 Mininet，也可能是真实 OpenFlow 网络）。
「环境不确定」的代价如果靠代码里的 if 分支来消化，就是每换一次环境改一轮代码。

所以换个思路：把环境能力变成**运行时可查询的事实**。
`GET /api/capabilities` 返回"当前环境支持什么、缺什么"，
`env_doctor.py` 用同一套逻辑做体检输出。

好处：
  - 换环境后跑一次就知道行不行、缺什么，不用试错
  - 演示/答辩时可以当场自证"我们是只读的、不影响现网"
  - 别的模块可以直接读这个端点决定要不要依赖我们
"""
from typing import Any, Dict, Optional

import env_config

# 我们的控制器实现的是 OpenFlow 1.3（controller_app 用 ofproto_v1_3）
OPENFLOW_PROTOCOL = 'OpenFlow 1.3'

# 契约路径版本（/api/intensure/v1/* 这一套）
CONTRACT_PATH_VERSION = '1.0'


def _count_ok(actual: int, expected: Optional[int]) -> bool:
    if expected is None:
        return True
    return actual >= int(expected)


def build_capabilities(state: Optional[dict] = None,
                       cfg: Optional[env_config.EnvConfig] = None,
                       fault_api_available: bool = True) -> Dict[str, Any]:
    """生成当前环境的能力描述。

    参数:
        state: 内部 NETWORK_STATE（没有就给空 dict）
        cfg: 环境配置（不给就取全局配置）
        fault_api_available: fault_api 模块能不能用（本机没装/没权限时为 False）
    """
    cfg = cfg or env_config.get_config()
    state = state or {}

    switches = state.get('switches') or {}
    hosts = state.get('hosts') or {}
    links = state.get('links') or {}

    expect = cfg.topology_expect
    exp_switches = expect.get('switches')
    exp_hosts = expect.get('hosts')
    # state['links'] 只记交换机之间的链路（LLDP 发现），主机链路不在里面
    exp_links = expect.get('inter_switch_links')

    links_found = len(links)
    probe = cfg.probe

    # 故障注入可用性：配置允许 + 模块可导入
    fault_allowed = cfg.allow_fault_injection
    fault_available = bool(fault_allowed and fault_api_available)
    if not fault_allowed:
        fault_reason = '配置为不允许（只读模式或 allow_fault_injection=false）'
    elif not fault_api_available:
        fault_reason = 'fault_api 模块不可用'
    else:
        fault_reason = '可用（mode=test，与生产模块解耦）'

    # 对方契约版本期望值
    partner_expected = cfg.assurance_expected_version
    if not partner_expected:
        try:
            import assurance_bridge
            partner_expected = assurance_bridge.DEFAULT_EXPECTED_VERSION
        except Exception:
            partner_expected = 'unknown'

    ready = bool(switches) and (exp_hosts is None or len(hosts) > 0)

    return {
        # ---- 环境与策略 ----
        'environment': cfg.environment,
        'mode': cfg.mode,
        'read_only': cfg.is_read_only,
        'write_operations': cfg.write_operations_allowed,
        'config_source': cfg.source,

        # ---- 采集能力 ----
        'openflow': {
            'protocol': OPENFLOW_PROTOCOL,
            # 控制器从不下发流表（controller_app 不发 OFPPFlowMod），只读
            'write_operations': 'none',
            'switches_connected': len(switches),
            'dpids': sorted(switches.keys()),
        },
        'topology_discovery': {
            # 有链路 = LLDP 邻居学习工作；没有 = 退化（真实设备不开 LLDP 时会这样）
            'method': 'lldp' if links_found else 'degraded',
            'links_discovered': links_found,
        },
        'hosts': {
            'discovered': len(hosts),
            'expected': exp_hosts,
            'ok': _count_ok(len(hosts), exp_hosts),
        },
        'topology': {
            'switches_discovered': len(switches),
            'switches_expected': exp_switches,
            'switches_ok': _count_ok(len(switches), exp_switches),
            'links_discovered': links_found,
            'links_expected': exp_links,
            'links_ok': _count_ok(links_found, exp_links),
        },

        # ---- 可选能力 ----
        'probe': {
            'driver': cfg.probe_driver,
            'source': probe.get('source'),
            'destination': probe.get('destination'),
        },
        'fault_injection': {
            'available': fault_available,
            'reason': fault_reason,
        },

        # ---- 契约 ----
        'contract': {
            'path_version': CONTRACT_PATH_VERSION,
            'partner_expected_version': partner_expected,
            'endpoints': [
                '/api/intensure/v1/state/current',
                '/api/intensure/v1/state/history',
                '/api/intensure/v1/port-events',
                '/api/intensure/v1/link-events',
                '/api/intensure/v1/probe-results',
            ],
        },

        'ready': ready,
    }
