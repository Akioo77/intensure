#!/usr/bin/env python3
"""load_test.py — P2-1 意图场景压测

给「100 意图规模下系统能不能扛住」一份**实测基线**，而不是算出来的估算。

模拟的工作负载与线上一致:
  - 每 CYCLE 秒一个采集周期（线上 STATS_INTERVAL=5）
  - 每周期: 1 次 network_state upsert + N 个 intent 各 1 次 consistency_check
  - 违例的 intent 额外触发一次 diagnoses 写入（最重的写路径）
  - state_history 按 30s 节流窗口写（summary 每 6 周期变一次）

采集指标:
  - 写延迟 p50/p95/p99、每周期吞吐、每周期耗时
  - 进程 CPU% / RSS
  - PG 连接数 / 锁等待 / 活跃事务
  - 热表体积增长

用法:
  python3 load_test.py --intents 100 --minutes 10
  python3 load_test.py --intents 100 --minutes 60 --violation-rate 0.2
  python3 load_test.py --intents 20 --minutes 2 --cleanup

产出: docs/load_test_results.md（性能基线报告）
"""
import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                    # demo_scripts/
sys.path.insert(0, os.path.dirname(_HERE))   # 项目根（db_writer 在这）

try:
    import psutil
except ImportError:
    psutil = None

import db_writer

try:
    import psycopg2
    from psycopg2.extras import Json
except ImportError:
    psycopg2 = None
    Json = None

# 策略由「实现模块」产生, 插表要用 implementation_user
# (intensure_user / assurance_user 对 implementation.policies 只有 SELECT)
IMPLEMENTATION_DSN = os.environ.get(
    "IMPLEMENTATION_DB_DSN",
    "postgresql://implementation_user:implementation_dev_pwd"
    "@127.0.0.1:5432/intensure_dev",
)


def direct_connect(dsn):
    """直连指定 DSN。psycopg2 出错会抛出, 不像 query() 那样吞异常。"""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 未安装")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn

HOT_TABLES = [
    "intensure.network_state",
    "intensure.state_history",
    "assurance.consistency_checks",
    "assurance.diagnoses",
    "implementation.policies",
]


def pct(values, p):
    """取分位数（values 非空时）。"""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


class Sampler:
    """周期性采样: 进程资源 + PG 状态 + 表体积。"""

    def __init__(self, hot_tables):
        self.hot_tables = hot_tables
        self.proc = psutil.Process(os.getpid()) if psutil else None
        self.samples = []
        self.w = db_writer.get_writer()

    def sample(self, cycle):
        rec = {"cycle": cycle, "ts": time.time()}

        if self.proc:
            try:
                rec["cpu_pct"] = round(self.proc.cpu_percent(interval=None), 1)
                rec["rss_mb"] = round(self.proc.memory_info().rss / 1048576, 1)
            except Exception:
                pass

        try:
            rows = self.w.query(
                "SELECT count(*) FILTER (WHERE backend_type='client backend') AS conns, "
                "count(*) FILTER (WHERE wait_event_type='Lock') AS lock_waits, "
                "count(*) FILTER (WHERE state='active in transaction') AS active_tx "
                "FROM pg_stat_activity WHERE datname=current_database()")
            if rows:
                rec["pg_conns"] = rows[0].get("conns")
                rec["lock_waits"] = rows[0].get("lock_waits")
                rec["active_tx"] = rows[0].get("active_tx")
        except Exception as e:
            rec["pg_err"] = str(e)[:80]

        sizes = {}
        for t in self.hot_tables:
            try:
                r = self.w.query(
                    "SELECT pg_total_relation_size('%s'::regclass) AS sz" % t)
                if r:
                    sizes[t] = r[0]["sz"]
            except Exception:
                pass
        if sizes:
            rec["table_bytes"] = sizes

        self.samples.append(rec)
        return rec


def seed_policies(n):
    """插入 n 条测试策略。

    用 implementation_user 直连 —— implementation.policies 的写权限只给它,
    我方 intensure_user / assurance_user 只有 SELECT。异常直接抛出, 不静默。
    """
    conn = direct_connect(IMPLEMENTATION_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM implementation.policies "
                        "WHERE intent_id LIKE 'loadtest-%'")
            ids = []
            for i in range(n):
                iid = "loadtest-%03d" % i
                spec = {
                    "source": {"cidr": "10.0.0.%d/32" % ((i % 4) + 1)},
                    "destination": {"cidr": "10.0.0.%d/32" % ((i % 4) + 1)},
                }
                cur.execute(
                    "INSERT INTO implementation.policies "
                    "(intent_id, intent_type, policy_spec, status, deployed_by) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (iid, "ACCESS_CONTROL",
                     Json(spec) if Json else spec, "active",
                     "loadtest-deployer"))
                ids.append(iid)
            # 验证真的插进去了
            cur.execute("SELECT count(*) FROM implementation.policies "
                        "WHERE intent_id LIKE 'loadtest-%'")
            got = cur.fetchone()[0]
            if got != n:
                raise RuntimeError(
                    "策略插入数量不符: 期望 %d 实际 %d" % (n, got))
    finally:
        conn.close()
    return ids


def cleanup(w, since_ts=None):
    """清理压测产生的行。"""
    try:
        conn = direct_connect(IMPLEMENTATION_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM implementation.policies "
                            "WHERE intent_id LIKE 'loadtest-%'")
        finally:
            conn.close()
    except Exception as e:
        print("  [warn] 清理策略失败: %s" % e)
    w.query("DELETE FROM assurance.consistency_checks WHERE intent_id LIKE 'loadtest-%'")
    w.query("DELETE FROM assurance.diagnoses WHERE intent_id LIKE 'loadtest-%'")
    if since_ts:
        w.query("DELETE FROM intensure.state_history WHERE captured_at > %s",
                (since_ts,))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intents", type=int, default=100)
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--cycle", type=float, default=5.0, help="采集周期秒（线上=5）")
    ap.add_argument("--violation-rate", type=float, default=0.1,
                    help="判为违例的 intent 比例（触发诊断写入）")
    ap.add_argument("--cleanup", action="store_true", help="结束后清理压测数据")
    ap.add_argument("--out", default="docs/load_test_results.md")
    args = ap.parse_args()

    intensure_w = db_writer.get_writer()
    assurance_w = db_writer.get_assurance_writer()

    print("=" * 64)
    print("P2-1 意图场景压测")
    print("=" * 64)
    print("  意图数: %d   时长: %d 分钟   周期: %ds"
          % (args.intents, args.minutes, args.cycle))
    print("  违例率: %.0f%%" % (args.violation_rate * 100))
    print("")

    ids = seed_policies(args.intents)
    print("  ✅ 已插入 %d 条测试策略" % len(ids))

    sampler = Sampler(HOT_TABLES)
    base = sampler.sample(0)
    before_sizes = dict(base.get("table_bytes", {}))

    total_writes = 0
    total_checks = 0
    total_diag = 0
    errors = 0
    latencies = []
    cycle_times = []
    start = time.time()
    end = start + args.minutes * 60
    cycle = 0

    try:
        while time.time() < end:
            cycle += 1
            c_start = time.time()

            # 1) network_state upsert（每周期 1 次）
            t0 = time.time()
            try:
                intensure_w.write_network_state({
                    "meta": {"update_count": cycle, "loadtest": True},
                    "switches": {"0000000000000001": {"ports": [
                        {"port_no": 1, "name": "s1-eth1", "state": 0}]}},
                    "hosts": {}, "links": {}, "events": [],
                })
                latencies.append((time.time() - t0) * 1000)
                total_writes += 1
            except Exception:
                errors += 1

            # 2) 每 intent 一次 consistency_check（违例的再写一条 diagnoses）
            t1 = time.time()
            n_violated = int(args.intents * args.violation_rate)
            for i, iid in enumerate(ids):
                violated = i < n_violated
                try:
                    assurance_w.write_consistency_check(
                        intent_id=iid,
                        check_type="reachability",
                        expected_state={"reachable": True},
                        actual_state={"reachable": not violated},
                        result="violated" if violated else "pass",
                        violation_types=["PORT_DOWN"] if violated else [],
                        evidence={"down_ports": ["s1:eth1"]} if violated else {},
                        is_heartbeat=False, duration_ms=None,
                    )
                    total_checks += 1
                    if violated:
                        assurance_w.write_diagnosis(
                            intent_id=iid,
                            root_causes=[{"type": "port_failure",
                                         "confidence": 0.85}],
                            confidence=0.85,
                            affected_devices=["s1"],
                            evidence={"down_ports": ["s1:eth1"]},
                        )
                        total_diag += 1
                except Exception:
                    errors += 1
            latencies.append((time.time() - t1) * 1000)

            # 3) state_history：summary 每 6 周期变一次 = 30s 节流窗口
            if cycle % 6 == 0:
                try:
                    intensure_w.write_state_history_if_needed(
                        {"meta": {"cycle": cycle}},
                        {"switches": 1, "hosts": 0, "epoch": cycle // 6},
                    )
                except Exception:
                    errors += 1

            cycle_times.append((time.time() - c_start) * 1000)
            rec = sampler.sample(cycle)

            if cycle % 5 == 0 or cycle == 1:
                el = time.time() - start
                tot = total_writes + total_checks + total_diag
                print("  周期 %4d | 写入 %7d | 周期 %7.1fms | CPU %5.1f%% | "
                      "RSS %6.1fMB | PG连接 %3d | 锁等待 %3d | 已用 %5.1fmin"
                      % (cycle, tot, cycle_times[-1], rec.get("cpu_pct", 0),
                         rec.get("rss_mb", 0), rec.get("pg_conns", 0),
                         rec.get("lock_waits", 0), el / 60))

            sleep_for = args.cycle - (time.time() - c_start)
            if sleep_for > 0:
                time.sleep(sleep_for)

    except KeyboardInterrupt:
        print("")
        print("  收到中断，提前结束")
    finally:
        if args.cleanup:
            cleanup(intensure_w, start)
            print("  🧹 压测数据已清理")

    elapsed = time.time() - start
    after_sizes = dict(sampler.samples[-1].get("table_bytes", {})) if sampler.samples else {}

    cpus = [s.get("cpu_pct") for s in sampler.samples if "cpu_pct" in s]
    rsss = [s.get("rss_mb") for s in sampler.samples if "rss_mb" in s]
    conns = [s.get("pg_conns") for s in sampler.samples if "pg_conns" in s]
    locks = [s.get("lock_waits") for s in sampler.samples if "lock_waits" in s]
    total = total_writes + total_checks + total_diag

    print("")
    print("=" * 64)
    print("压测结果")
    print("=" * 64)
    print("  实际时长: %.1f 分钟   周期数: %d" % (elapsed / 60, cycle))
    print("  总写入:   %d (network_state %d / checks %d / diagnoses %d)"
          % (total, total_writes, total_checks, total_diag))
    print("  错误:     %d" % errors)
    if elapsed > 0:
        print("  吞吐:     %.1f 写/秒" % (total / elapsed))
    if latencies:
        print("  写延迟:   p50 %.2fms  p95 %.2fms  p99 %.2fms  max %.2fms"
              % (pct(latencies, 50), pct(latencies, 95), pct(latencies, 99),
                 max(latencies)))
    if cycle_times:
        print("  周期耗时: p50 %.0fms  max %.0fms"
              % (pct(cycle_times, 50), max(cycle_times)))
    if cpus:
        print("  CPU:      平均 %.1f%%  峰值 %.1f%%" % (sum(cpus) / len(cpus), max(cpus)))
    if rsss:
        print("  RSS:      平均 %.1fMB  峰值 %.1fMB" % (sum(rsss) / len(rsss), max(rsss)))
    if conns:
        print("  PG连接:   峰值 %d" % max(conns))
    if locks:
        print("  锁等待:   峰值 %d  累计 %d" % (max(locks), sum(locks)))

    if before_sizes:
        print("")
        print("  表体积增长:")
        for t, b in before_sizes.items():
            a = after_sizes.get(t, b)
            print("    %-38s %8.2fMB -> %8.2fMB (+%.2fMB)"
                  % (t, b / 1048576, a / 1048576, (a - b) / 1048576))

    # ---------- 报告 ----------
    r = []
    r.append("# P2-1 意图场景压测 · 性能基线")
    r.append("")
    r.append("> 实测时间：%s" % time.strftime("%Y-%m-%d %H:%M"))
    r.append("> 工具：`demo_scripts/load_test.py`")
    r.append("")
    r.append("## 一、测试配置")
    r.append("")
    r.append("| 项 | 值 |")
    r.append("|---|---|")
    r.append("| 意图数 | %d |" % args.intents)
    r.append("| 时长 | %d 分钟（实际 %.1f 分钟）|" % (args.minutes, elapsed / 60))
    r.append("| 采集周期 | %ds（线上 STATS_INTERVAL=5）|" % args.cycle)
    r.append("| 违例率 | %.0f%%（触发 diagnoses 写入）|" % (args.violation_rate * 100))
    r.append("| 周期数 | %d |" % cycle)
    r.append("")
    r.append("## 二、写入量与吞吐")
    r.append("")
    r.append("| 项 | 值 |")
    r.append("|---|---|")
    r.append("| network_state upsert | %d |" % total_writes)
    r.append("| consistency_check | %d |" % total_checks)
    r.append("| diagnoses | %d |" % total_diag)
    r.append("| **合计** | **%d** |" % total)
    r.append("| 吞吐 | %.1f 写/秒 |" % (total / elapsed if elapsed else 0))
    r.append("| 错误 | %d |" % errors)
    r.append("")
    r.append("## 三、延迟")
    r.append("")
    if latencies:
        r.append("| 指标 | 值 |")
        r.append("|---|---|")
        r.append("| 写延迟 p50 | %.2f ms |" % pct(latencies, 50))
        r.append("| 写延迟 p95 | %.2f ms |" % pct(latencies, 95))
        r.append("| 写延迟 p99 | %.2f ms |" % pct(latencies, 99))
        r.append("| 写延迟 max | %.2f ms |" % max(latencies))
    if cycle_times:
        r.append("| 单周期耗时 p50 | %.0f ms |" % pct(cycle_times, 50))
        r.append("| 单周期耗时 max | %.0f ms |" % max(cycle_times))
    r.append("")
    r.append("## 四、资源与数据库")
    r.append("")
    if cpus:
        r.append("- 进程 CPU：平均 %.1f%%，峰值 %.1f%%" % (sum(cpus) / len(cpus), max(cpus)))
    if rsss:
        r.append("- 进程 RSS：平均 %.1f MB，峰值 %.1f MB" % (sum(rsss) / len(rsss), max(rsss)))
    if conns:
        r.append("- PG 连接数峰值：%d" % max(conns))
    if locks:
        r.append("- 锁等待：峰值 %d，累计 %d" % (max(locks), sum(locks)))
    r.append("")
    r.append("## 五、表体积增长")
    r.append("")
    if before_sizes:
        r.append("| 表 | 起始 | 结束 | 增长 |")
        r.append("|---|---|---|---|")
        for t, b in before_sizes.items():
            a = after_sizes.get(t, b)
            r.append("| `%s` | %.2f MB | %.2f MB | +%.2f MB |"
                     % (t, b / 1048576, a / 1048576, (a - b) / 1048576))
    r.append("")
    r.append("## 六、采样明细")
    r.append("")
    r.append("| 周期 | CPU% | RSS MB | PG连接 | 锁等待 | 活跃事务 |")
    r.append("|---|---|---|---|---|---|")
    step = max(1, len(sampler.samples) // 20 or 1)
    for s in sampler.samples[::step]:
        r.append("| %s | %s | %s | %s | %s | %s |"
                 % (s.get("cycle"), s.get("cpu_pct", "-"), s.get("rss_mb", "-"),
                    s.get("pg_conns", "-"), s.get("lock_waits", "-"),
                    s.get("active_tx", "-")))
    r.append("")

    out = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", args.out))
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(r))
    print("")
    print("  报告已写: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
