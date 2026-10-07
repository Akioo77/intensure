"""保障模块 MVP 脚本（Phase 1）

职责:
- 每 N 秒（默认 5s）从 DB 读 intensure.network_state（当前实际状态）
- 每 N 秒从 DB 读 implementation.policies（期望状态）
- 对每个 policy 做一致性检查
- 写入 assurance.consistency_checks（变化时 + 每 60s 心跳）
- 检测到违例：写入 assurance.diagnoses（含 evidence/affected_devices/confidence）
- 维护 assurance.intent_states 状态机轨迹
- 心跳到 shared.module_health

Phase 1 简化:
- 一致性检查只看 reachability（host 是否在 active hosts 表里）
- 不做诊断的多步推理（直接基于 ActualState 的 down_ports/down_links 推断）

被调方:
- demo E2E（起 controller + seed policies + 跑本脚本 + 注入故障）
"""
import os
import sys
import time
import json

# === 加 intensure 目录到 path（db_writer.py 在这里） ===
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from db_writer import get_assurance_writer, DSN_ASSURANCE  # noqa: E402

# === 配置 ===
CHECK_INTERVAL = float(os.environ.get('ASSURANCE_INTERVAL', '5'))
CHECK_TYPE = os.environ.get('ASSURANCE_CHECK_TYPE', 'reachability')
HEARTBEAT_INTERVAL = float(os.environ.get('ASSURANCE_HEARTBEAT', '30'))
VERSION = '3.6-mvp'

w = get_assurance_writer()


# ============ 状态机 ============

def update_intent_state(intent_id: str, status: str):
    """写/更新 intent_states（当前状态）。"""
    try:
        conn = w._get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO assurance.intent_states (intent_id, current_status, last_checked_at, last_status_at)
                VALUES (%s, %s, now(), now())
                ON CONFLICT (intent_id) DO UPDATE SET
                    current_status = EXCLUDED.current_status,
                    last_checked_at = now(),
                    last_status_at = CASE WHEN assurance.intent_states.current_status != EXCLUDED.current_status
                                          THEN now() ELSE assurance.intent_states.last_status_at END
            """, (intent_id, status))
        return True
    except Exception as e:
        print(f'[WARN] update_intent_state failed: {e}', flush=True)
        return False


# ============ 一致性检查 ============

def extract_actual_state_hosts(network_state_payload: dict) -> dict:
    """从 network_state.payload 里提取 (host_ip → port_up_bool) dict。"""
    hosts_info = {}
    hosts = network_state_payload.get('hosts', {})
    for mac, host in hosts.items():
        ipv4_list = host.get('ipv4', []) or host.get('ips', [])
        # 即使 ipv4 为空，也记录（按 MAC），便于后续按 mac 检查
        port_no = host.get('port')
        info = {
            'mac': mac,
            'dpid': host.get('dpid'),
            'port': port_no,
            'ipv4_list': ipv4_list,
            'present': port_no is not None,
        }
        # 按 ip 索引
        if ipv4_list:
            for ip in ipv4_list:
                # 取第一个 IPv4（去掉 /mask）
                ip_clean = ip.split('/')[0] if isinstance(ip, str) else ip
                hosts_info[ip_clean] = info
        # 始终也按 MAC 索引（key 前缀区分）
        hosts_info[f'mac:{mac}'] = info
    return hosts_info


def extract_actual_down_ports(network_state_payload: dict) -> list:
    """从 network_state.payload 提取所有 down 的端口。

    intensure v3.6 没有直接算 down_ports，这里我们自己从 switches.ports 算。
    Phase 1 简化：只从单个 dpid 的 ports 表里拿 state bit 0 (LINK_DOWN) 为 1 的。
    """
    out = []
    for dpid, sw in network_state_payload.get('switches', {}).items():
        # 设备名从 dpid 末位推断
        device = f's{int(dpid, 16)}' if dpid else 'unknown'
        for p in sw.get('ports', []):
            if p.get('port_no') == 4294967294:  # OFPP_LOCAL
                continue
            if p.get('state', 0) & 1:  # OFPPS_LINK_DOWN
                port_name = p.get('name') or f'p{p.get("port_no")}'
                # 提取 ethN
                eth = port_name.split('-')[-1] if '-' in port_name else port_name
                out.append(f'{device}:{eth}')
    return out


def extract_actual_down_links(network_state_payload: dict) -> list:
    """从 network_state.links 计算 down 的链路。

    Phase 1 简化：links 是 LLDP 发现的所有链路。
    如果两端端口之一是 down 的，整条链路视为 down。
    """
    # 先计算 down 端口集合
    down_port_keys = set()  # (dpid, port_no)
    for dpid, sw in network_state_payload.get('switches', {}).items():
        for p in sw.get('ports', []):
            if p.get('port_no') == 4294967294:
                continue
            if p.get('state', 0) & 1:
                down_port_keys.add((dpid, p.get('port_no')))

    out = []
    seen = set()
    links = network_state_payload.get('links', {})
    for key, link in links.items():
        if not isinstance(link, dict):
            continue
        src_dpid = link.get('src_dpid') or link.get('dpid_a') or ''
        src_port = link.get('src_port') or link.get('port_a')
        dst_dpid = link.get('dst_dpid') or link.get('peer_dpid') or link.get('dpid_b') or ''
        dst_port = link.get('dst_port') or link.get('peer_port') or link.get('port_b')

        a_down = (src_dpid, src_port) in down_port_keys
        b_down = (dst_dpid, dst_port) in down_port_keys
        if not (a_down or b_down):
            continue

        # 设备名
        try:
            sw_a = f's{int(src_dpid, 16)}' if src_dpid else '?'
        except (ValueError, TypeError):
            sw_a = '?'
        try:
            sw_b = f's{int(dst_dpid, 16)}' if dst_dpid else '?'
        except (ValueError, TypeError):
            sw_b = '?'

        ep_a = f'{sw_a}:{src_port}'
        ep_b = f'{sw_b}:{dst_port}'

        pair = frozenset([ep_a, ep_b])
        if pair in seen:
            continue
        seen.add(pair)
        out.append(f'{ep_a}-{ep_b}')

    return out


def check_one_policy(policy: dict, actual_state: dict) -> dict:
    """对单个 policy 做一致性检查。

    输入:
      policy: {intent_id, intent_type, policy_spec, ...}
      actual_state: network_state.payload
    返回:
      {
        result: 'pass' / 'violated' / 'error',
        violation_types: [...],
        evidence: {...},
        actual: {...},
        expected: {...}
      }
    """
    intent_id = policy.get('intent_id', '')
    policy_spec = policy.get('policy_spec', {}) or {}
    intent_type = policy.get('intent_type', '')

    src = policy_spec.get('source', {})
    dst = policy_spec.get('destination', {})

    src_ip = (src.get('cidr') or '').split('/')[0] if isinstance(src.get('cidr'), str) else None
    dst_ip = (dst.get('cidr') or '').split('/')[0] if isinstance(dst.get('cidr'), str) else None

    hosts = extract_actual_state_hosts(actual_state)
    down_ports = extract_actual_down_ports(actual_state)
    down_links = extract_actual_down_links(actual_state)

    expected = {
        'intent_id': intent_id,
        'intent_type': intent_type,
        'src': src_ip,
        'dst': dst_ip,
    }

    actual = {
        'src_present': hosts.get(src_ip, {}).get('present', False) if src_ip else None,
        'dst_present': hosts.get(dst_ip, {}).get('present', False) if dst_ip else None,
        'down_ports': down_ports,
        'down_links': down_links,
    }

    violation_types = []
    evidence = {}

    # 检查 1: src/dst host 是否在（按 IP 匹配）
    # 重要：主机需要学到 IP 以后才能查到。Mininet 启动后需要 ARP/ping 才能填 ipv4。
    # Phase 1 策略：
    #   - 如果 hosts 表里所有 host 都没 ipv4（刚启动，ARP 未触发），跳过此检查（不算违例）
    #   - 如果 hosts 表里有 ipv4（采集已稳定），则严格匹配
    any_ip_known = any(info.get('ipv4_list') for info in hosts.values())

    if any_ip_known:
        if src_ip:
            src_found = src_ip in hosts
            if not src_found:
                for h_info in hosts.values():
                    if src_ip in (h_info.get('ipv4_list') or []):
                        src_found = True
                        break
            if not src_found:
                violation_types.append('SOURCE_NOT_FOUND')
                evidence['missing_source'] = src_ip
        if dst_ip:
            dst_found = dst_ip in hosts
            if not dst_found:
                for h_info in hosts.values():
                    if dst_ip in (h_info.get('ipv4_list') or []):
                        dst_found = True
                        break
            if not dst_found:
                violation_types.append('DEST_NOT_FOUND')
                evidence['missing_destination'] = dst_ip

    # 检查 2: 是否有端口 down
    if down_ports:
        violation_types.append('PORT_DOWN')
        evidence['down_ports'] = down_ports
    if down_links:
        violation_types.append('LINK_DOWN')
        evidence['down_links'] = down_links

    # 检查 3: 特定端口是否在期望路径上 down
    if src_ip:
        for host_ip, info in hosts.items():
            if host_ip == src_ip and not info.get('present'):
                violation_types.append('SOURCE_HOST_UNREACHABLE')
                break

    if violation_types:
        result = 'violated'
    else:
        result = 'pass'

    return {
        'result': result,
        'violation_types': violation_types,
        'evidence': evidence,
        'actual': actual,
        'expected': expected,
    }


def diagnose_violation(intent_id: str, check_result: dict) -> dict:
    """基于一致性检查结果，生成根因诊断。

    简单规则（Phase 1）：
    - down_ports 非空 → 端口故障（port_failure）
    - down_links 非空 → 链路故障（link_failure）
    - confidence: 证据越明确越高
    - affected_devices: 从 down_ports/down_links 提取
    """
    evidence = check_result.get('evidence', {})
    down_ports = evidence.get('down_ports', [])
    down_links = evidence.get('down_links', [])

    root_causes = []
    affected_devices = set()
    confidence = 0.5

    if down_links:
        # 从 link "s1:eth3-s2:eth3" 拆设备
        for link in down_links:
            for ep in link.split('-'):
                device = ep.split(':')[0]
                if device:
                    affected_devices.add(device)
        root_causes.append({
            'type': 'link_failure',
            'confidence': 0.8 if len(down_links) > 0 else 0.0,
            'candidate_reasons': [
                'physical_cable_disconnect',
                'switch_port_failure',
                'admin_shutdown',
            ],
            'evidence': {'down_links': down_links},
        })
        confidence = max(confidence, 0.8)

    if down_ports:
        # 从 "s1:eth3" 拆设备
        for port in down_ports:
            device = port.split(':')[0]
            if device:
                affected_devices.add(device)
        root_causes.append({
            'type': 'port_failure',
            'confidence': 0.85 if len(down_ports) > 0 else 0.0,
            'candidate_reasons': [
                'cable_disconnect',
                'optical_module_failure',
                'admin_shutdown',
            ],
            'evidence': {'down_ports': down_ports},
        })
        confidence = max(confidence, 0.85)

    if not root_causes:
        # 没有明确证据
        root_causes.append({
            'type': 'unknown',
            'confidence': 0.4,
            'candidate_reasons': ['insufficient_evidence'],
            'evidence': evidence,
        })
        confidence = 0.4

    return {
        'intent_id': intent_id,
        'root_causes': root_causes,
        'confidence': confidence,
        'affected_devices': sorted(list(affected_devices)),
        'evidence': evidence,
    }


# ============ 主循环 ============

def run_one_cycle():
    """跑一轮一致性检查 + 检查。"""
    # 1. 读当前网络状态（intensure 采集的）
    state_row = w.query_one('SELECT payload FROM intensure.network_state LIMIT 1')
    if not state_row:
        print('[WARN] network_state 还没数据', flush=True)
        return None
    actual_state = state_row['payload'] or {}

    # 2. 读所有 active policies
    policies = w.query("""
        SELECT intent_id, intent_type, policy_spec
        FROM implementation.policies
        WHERE status = 'active'
          AND effective_to IS NULL
        ORDER BY effective_from
    """)

    if not policies:
        # 还没 seed policies — 安静
        return {'policies_checked': 0, 'violations': 0}

    # 3. 对每个 policy 做检查
    violations = []
    for policy in policies:
        intent_id = policy.get('intent_id', '')
        check = check_one_policy(policy, actual_state)
        result = check['result']
        violation_types = check['violation_types']
        evidence = check['evidence']

        # 决定是否写库（变化或心跳）
        should_write, is_heartbeat = w.should_write_consistency(
            intent_id=intent_id, result=result,
            is_diagnosing=(result == 'violated'),
        )

        if should_write:
            w.write_consistency_check(
                intent_id=intent_id,
                check_type=CHECK_TYPE,
                expected_state=check['expected'],
                actual_state=check['actual'],
                result=result,
                violation_types=violation_types,
                evidence=evidence,
                is_heartbeat=is_heartbeat,
                duration_ms=None,
            )

            if result == 'violated':
                # 违例 → 写诊断
                diag = diagnose_violation(intent_id, check)
                diag_id = w.write_diagnosis(
                    intent_id=intent_id,
                    root_causes=diag['root_causes'],
                    confidence=diag['confidence'],
                    affected_devices=diag['affected_devices'],
                    evidence=diag['evidence'],
                )
                update_intent_state(intent_id, 'DIAGNOSING')
                violations.append({
                    'intent_id': intent_id,
                    'confidence': diag['confidence'],
                    'affected_devices': diag['affected_devices'],
                    'diag_id': diag_id,
                })
            elif result == 'pass':
                update_intent_state(intent_id, 'ACTIVE')
        # else: skipped (no change + within heartbeat window)

    return {
        'policies_checked': len(policies),
        'violations': violations,
    }


def main():
    print('=' * 60)
    print('🛡️  保障模块 MVP v1 (Phase 1)')
    print('=' * 60)
    print(f'   check_interval: {CHECK_INTERVAL}s')
    print(f'   DSN: {DSN_ASSURANCE}')
    print()

    cycle = 0
    last_heartbeat = 0.0
    while True:
        cycle += 1
        t0 = time.time()
        try:
            result = run_one_cycle()
            if result is None:
                time.sleep(CHECK_INTERVAL)
                continue

            if result['violations']:
                v = result['violations']
                vsummary = ', '.join([f"{x['intent_id']}(conf={x['confidence']}, devs={x['affected_devices']})" for x in v])
                print(f'CYCLE_{cycle:04d} policies={result["policies_checked"]} '
                      f'VIOLATIONS={len(v)} [{vsummary}] '
                      f'took={int((time.time()-t0)*1000)}ms',
                      flush=True)
            else:
                print(f'CYCLE_{cycle:04d} policies={result["policies_checked"]} '
                      f'violations=0 took={int((time.time()-t0)*1000)}ms',
                      flush=True)
        except Exception as e:
            print(f'CYCLE_{cycle:04d} ERROR: {e}', flush=True)

        # 心跳（Phase 1 不启用，环境变量 INTENSURE_HEARTBEAT=1 启用）
        if os.environ.get('INTENSURE_HEARTBEAT') == '1':
            now = time.time()
            if (now - last_heartbeat) > HEARTBEAT_INTERVAL:
                try:
                    # 用 db_writer 的 write_heartbeat 但模块名='assurance'
                    # 直接 SQL 写（避免修改 db_writer 共享函数）
                    conn = w._get_conn()
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO shared.module_health (module_name, status, version, metadata)
                            VALUES ('assurance', 'healthy', %s, '{}'::jsonb)
                            ON CONFLICT (module_name) DO UPDATE SET
                                last_heartbeat = now(),
                                status = EXCLUDED.status,
                                version = EXCLUDED.version
                        """, (VERSION,))
                    last_heartbeat = now
                except Exception as e:
                    print(f'  WARN: heartbeat failed: {e}', flush=True)

        time.sleep(CHECK_INTERVAL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nCtrl+C, exiting...', flush=True)
        sys.exit(0)