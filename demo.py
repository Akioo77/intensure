"""端到端 demo: 启动 → 采集 → 验证 → 注入故障 → 再验证 → 恢复。

需要在不同 terminal 中:
  Terminal 1: cd ~/intensure && PYTHONPATH=. python3 run_all.py
  Terminal 2: cd ~/intensure && sudo python3 demo.py

或者用本脚本的 main() 自动 orchestrator 模式（实验性）。
"""
import os
import sys
import time
import subprocess
import json

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)


# ============ 颜色 (终端美化) ============
class C:
    GRN = '\033[92m'
    RED = '\033[91m'
    YLW = '\033[93m'
    BLU = '\033[94m'
    GRY = '\033[90m'
    BLD = '\033[1m'
    END = '\033[0m'


def ok(msg): print(f"  {C.GRN}✓{C.END} {msg}")
def fail(msg): print(f"  {C.RED}✗{C.END} {msg}")
def warn(msg): print(f"  {C.YLW}⚠{C.END} {msg}")
def info(msg): print(f"  {C.BLU}ℹ{C.END} {msg}")
def section(title):
    print(f"\n{C.BLD}{'=' * 60}\n  {title}\n{'=' * 60}{C.END}")


# ============ 步骤函数 ============
def wait_for_controller(url='http://127.0.0.1:8080', timeout=30):
    """等控制器 + REST 起来."""
    import urllib.request
    from state_collector import StateCollector
    start = time.time()
    while time.time() - start < timeout:
        try:
            c = StateCollector(url, timeout=2)
            health = c._get('/api/health')
            return c
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"controller not ready after {timeout}s")


def step1_basic_collection(c):
    """步骤 1: 基础状态采集 + 验证."""
    section("步骤 1: 基础状态采集")

    # 等交换机
    info("等待交换机连接...")
    if not c.wait_for_switches(2, timeout=30):
        fail("未检测到 2 个交换机")
        return False
    ok("检测到 2 个交换机")

    state = c.get_state()
    n_sw = len(state['switches'])
    n_host = len(state['hosts'])
    info(f"当前: {n_sw} 交换机, {n_host} 主机")

    # 跑 pingall 触发流量
    info("运行 Mininet pingall (触发流量)...")
    # 注意: 这个 demo 脚本需要从 Mininet CLI 调用
    # 这里我们假设 pingall 已在外层跑过

    # 验证
    info("跑状态验证...")
    report = c.validate()
    print(f"     {report['by_level']}")
    for issue in report['issues'][:5]:
        print(f"     [{issue['level']}] {issue['category']}: {issue['message']}")

    return report['ok']


def step2_after_traffic(c, baseline):
    """步骤 2: 流量后再次采集, 与基线对比."""
    section("步骤 2: 流量后验证")

    time.sleep(2)  # 等 stats 更新
    state = c.get_state()

    # 检查端口计数器是否增长
    info("检查端口计数器增量:")
    grew = 0
    for dpid, stats in state.get('port_stats', {}).items():
        base_stats = baseline.get('port_stats', {}).get(dpid, [])
        base_by_port = {s['port_no']: s for s in base_stats}
        for s in stats:
            base = base_by_port.get(s['port_no'], {})
            delta = s.get('rx_bytes', 0) - base.get('rx_bytes', 0)
            if delta > 0:
                grew += 1
    if grew > 0:
        ok(f"{grew} 个端口有流量通过")
    else:
        warn("端口计数器没增长, 可能没流量")

    # 检查流表条目
    total_flows = sum(len(f) for f in state.get('flows', {}).values())
    info(f"流表条目总数: {total_flows}")
    if total_flows > 0:
        ok("已安装流表规则")
    else:
        warn("流表为空 (可能还没触发 L3 通信)")

    # 验证
    report = c.validate(state, baseline=baseline)
    print(f"     {report['by_level']}")
    return report['ok']


def step3_inject_fault(c, fault_type='link_down'):
    """步骤 3: 注入故障."""
    section(f"步骤 3: 注入故障 ({fault_type})")
    info("⚠ 故障注入需要 Mininet 上下文, 此处仅打印提示")
    info("实际 demo 中, demo.py 会接管 Mininet net 实例")
    return True


def main():
    print(f"{C.BLD}{'=' * 60}")
    print("  SDN 状态采集 + 验证 演示")
    print(f"{'=' * 60}{C.END}")

    # 这部分是 collector 视角的 demo (在外部进程跑)
    # 完整 demo (含 Mininet) 见 demo_full.py

    info("连接到 controller...")
    try:
        c = wait_for_controller(timeout=15)
    except RuntimeError as e:
        fail(str(e))
        info("请先在另一个 terminal 启动: python3 run_all.py")
        return

    ok(f"已连接到 {c.base_url}")

    # 步骤 1: 基础采集
    if not step1_basic_collection(c):
        fail("基础采集失败")
        return

    # 步骤 2: 流量后
    baseline = c.get_state()
    info("等 pingall / 流量 5s...")
    time.sleep(5)
    step2_after_traffic(c, baseline)

    # 步骤 3: 提示故障注入
    step3_inject_fault(c)

    print()
    info("基础 demo 完成. 完整 demo (含 Mininet 集成) 见 demo_full.py")


if __name__ == '__main__':
    main()
