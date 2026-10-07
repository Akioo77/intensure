"""OVS 详细日志采集器（独立模块）.

目的：捕获 ovs-vswitchd 内部日志，提供端口状态变化的归因依据。
     比如：链路 down → up 的 39 秒间隔，是谁触发的？是 OpenFlow 控制器？
     还是 ovs-ofctl 命令？还是 OVS 自己 LLDP 重发现？

不影响现有代码：
  - fault_injector.py（链路注入保持原样）
  - controller_app.py（采集逻辑保持原样）
  - state_server.py（只新增 1 个 endpoint: GET /api/ovs/log）
  - run_all.py（启动方式不变，只在末尾附加 OVSLogger）
  - live_env.py / demo_full.py / integration_demo.py 全部不受影响

使用方式 A（作为模块被 state_server.py 引入）：
  from ovs_logger import get_instance
  logger = get_instance()
  logger.start()
  # 查询：
  logs = logger.get_logs(since=..., until=...)

使用方式 B（独立运行自检）：
  python3 ovs_logger.py
  # 跑 10 秒，输出统计 + 最近几条日志

设计原则：
  1. 完全独立，不依赖其他 intensure 模块
  2. 用 subprocess + sudo tail 采集（不修改 OVS 文件权限）
  3. 内存环形 buffer（最多 5000 条 ≈ 1MB）
  4. 线程安全（锁保护 buffer）
  5. 优雅启停（关闭时自动恢复 OVS 默认日志级别）
"""
import os
import sys
import time
import threading
import subprocess
import re
from collections import deque


class OVSLogger:
    """OVS 详细日志采集器."""

    LOG_FILE = '/var/log/openvswitch/ovs-vswitchd.log'
    BUFFER_SIZE = 5000

    # OVS 日志格式: 2026-09-07T20:14:46.696Z|00007|ofproto|INFO|...
    LOG_PATTERN = re.compile(
        r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z)\|'
        r'(?P<seq>\d+)\|(?P<module>\w+)\|(?P<level>\w+)\|(?P<msg>.*)$'
    )

    # 要调整日志级别的 OVS 模块（联调关心的）
    # ofp-msgs 在某些 OVS 版本里不存在，启动时会报错，所以不放这里
    DEFAULT_MODULES = ['ofproto', 'netdev', 'dpif', 'bridge']

    def __init__(self,
                 log_file=LOG_FILE,
                 buffer_size=BUFFER_SIZE,
                 sudo=True,
                 auto_verbosity=True):
        """
        参数:
            log_file: OVS 日志文件路径
            buffer_size: 内存 buffer 最大条数
            sudo: 是否用 sudo 读日志（默认 True，因为 /var/log/openvswitch/ 是 root 权限）
            auto_verbosity: start() 时是否自动把 OVS 日志级别提升到 dbg
        """
        self.log_file = log_file
        self.buffer_size = buffer_size
        self.sudo = sudo
        self.auto_verbosity = auto_verbosity
        self._buffer = deque(maxlen=buffer_size)
        self._lock = threading.Lock()
        self._proc = None
        self._reader_thread = None
        self._running = False
        self._started_at = None

    # ============================================================
    # 生命周期
    # ============================================================

    def start(self):
        """启动采集.

        返回:
            bool: 是否成功启动
        """
        if self._running:
            print("⚠ OVSLogger: 已在运行")
            return True

        # 1. 提升 OVS 日志级别（可选）
        if self.auto_verbosity:
            self._set_ovs_verbosity('dbg')

        # 2. 启动 tail subprocess
        # 用 '--' 分隔选项和参数（防 log_file 含特殊字符被当成选项）
        cmd = ['tail', '-F', '-n', '+1', '--', self.log_file]
        if self.sudo:
            cmd = ['sudo'] + cmd

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # 行缓冲
            )
        except FileNotFoundError as e:
            print(f"❌ OVSLogger: tail 命令找不到: {e}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"❌ OVSLogger: 启动 tail 失败: {e}", file=sys.stderr)
            return False

        # 3. 启动 reader 线程
        self._running = True
        self._started_at = time.time()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name='OVSLogger-reader')
        self._reader_thread.start()

        print(f"✅ OVSLogger: 已启动 (PID {self._proc.pid}, 监控 {self.log_file})")
        return True

    def stop(self, restore_verbosity=True):
        """停止采集.

        参数:
            restore_verbosity: 是否恢复 OVS 默认日志级别（INFO）
        """
        if not self._running:
            return

        self._running = False

        # 1. 杀 subprocess
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            except Exception:
                pass

        # 2. 恢复 OVS 日志级别（可选）
        if restore_verbosity:
            self._set_ovs_verbosity('info')

        print("✅ OVSLogger: 已停止")

    # ============================================================
    # 查询接口（给 state_server.py 调用）
    # ============================================================

    def get_logs(self, since=0, until=None, module=None, level=None,
                 keyword=None, limit=500):
        """按时间窗口 + 模块 + 关键词过滤查询日志.

        参数:
            since: 起始时间戳（秒，float）
            until: 结束时间戳（秒），None = 现在
            module: 模块名过滤（'ofproto' / 'netdev' / 'dpif' 等）
            level: 日志级别过滤（'DBG' / 'INFO' / 'WARN' / 'ERR'）
            keyword: 关键词过滤（msg 里包含）
            limit: 最多返回条数

        返回:
            list of dict, 每个 dict 含 ts/ts_str/seq/module/level/msg
        """
        if until is None:
            until = time.time() + 60

        with self._lock:
            snapshot = list(self._buffer)

        result = []
        for entry in snapshot:
            ts = entry.get('ts') or 0
            if not (since <= ts <= until):
                continue
            if module and entry.get('module') != module:
                continue
            if level and entry.get('level') != level:
                continue
            if keyword and keyword.lower() not in entry.get('msg', '').lower():
                continue
            result.append(entry)

        return result[-limit:]

    def get_stats(self):
        """获取采集器统计（给 dashboard 显示用）.

        返回:
            dict: running, buffer_size, buffer_capacity, log_file, pid, started_at, uptime_s
        """
        with self._lock:
            return {
                'running': self._running,
                'buffer_size': len(self._buffer),
                'buffer_capacity': self.buffer_size,
                'log_file': self.log_file,
                'pid': self._proc.pid if self._proc else None,
                'started_at': self._started_at,
                'uptime_s': (time.time() - self._started_at) if self._started_at else 0,
            }

    def correlate_with_link_events(self, link_events, time_window=5.0):
        """把 link events 和 OVS 日志做关联（归因用）.

        参数:
            link_events: list of port_link_status 事件
            time_window: 每个 link event 前后 ± N 秒

        返回:
            list of dict, 每个 dict 含 link_event + ovs_logs (前后 ± N 秒的 OVS 日志)
        """
        results = []
        for ev in link_events:
            ts = ev.get('time', 0)
            logs = self.get_logs(
                since=ts - time_window,
                until=ts + time_window,
                limit=50,
            )
            results.append({
                'link_event': ev,
                'ovs_logs': logs,
                'time_window_s': time_window,
            })
        return results

    # ============================================================
    # 内部
    # ============================================================

    def _reader_loop(self):
        """后台线程: 从 tail subprocess 读行，解析进 buffer."""
        if not self._proc:
            return
        try:
            for line in iter(self._proc.stdout.readline, ''):
                if not self._running:
                    break
                parsed = self._parse_line(line)
                if parsed:
                    with self._lock:
                        self._buffer.append(parsed)
        except Exception as e:
            if self._running:
                print(f"⚠ OVSLogger: reader loop 异常: {e}", file=sys.stderr)
            self._running = False

    def _parse_line(self, line):
        """解析单行 OVS 日志.

        返回:
            dict 或 None
        """
        line = line.rstrip('\n').rstrip('\r')
        if not line:
            return None
        m = self.LOG_PATTERN.match(line)
        if not m:
            return None
        # 转换时间戳
        # 重要: OVS 日志时间戳是 UTC ('...Z' 后缀)，
        # 必须 replace(tzinfo=timezone.utc) 后再 .timestamp()，
        # 否则在 UTC+8 时区下偏 8 小时
        try:
            from datetime import datetime, timezone
            ts = datetime.strptime(
                m.group('ts'), '%Y-%m-%dT%H:%M:%S.%fZ'
            ).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            ts = None
        return {
            'ts': ts,
            'ts_str': m.group('ts'),
            'seq': int(m.group('seq')),
            'module': m.group('module'),
            'level': m.group('level'),
            'msg': m.group('msg'),
        }

    def _set_ovs_verbosity(self, level):
        """调整 OVS 日志级别.

        level: 'off' | 'emer' | 'err' | 'warn' | 'info' | 'dbg'
        """
        valid_levels = {'off', 'emer', 'err', 'warn', 'info', 'dbg'}
        if level not in valid_levels:
            print(f"⚠ OVSLogger: 无效级别 {level}", file=sys.stderr)
            return
        for mod in self.DEFAULT_MODULES:
            cmd = ['sudo', 'ovs-appctl', 'vlog/set', f'{mod}:{level}']
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=3, text=True)
                if result.returncode != 0:
                    print(f"⚠ OVSLogger: 调整 {mod} 级别失败: {result.stderr.strip()}",
                          file=sys.stderr)
            except subprocess.TimeoutExpired:
                print(f"⚠ OVSLogger: 调整 {mod} 级别超时", file=sys.stderr)
            except Exception as e:
                print(f"⚠ OVSLogger: 调整 {mod} 级别异常: {e}", file=sys.stderr)


# ============================================================
# 全局单例（供 state_server.py import 用，避免重复启动 tail）
# ============================================================

_INSTANCE = None
_LOCK = threading.Lock()


def get_instance():
    """获取全局单例."""
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = OVSLogger()
        return _INSTANCE


def start_global():
    """启动全局 OVSLogger（被 run_all.py 调用）."""
    logger = get_instance()
    return logger.start()


def stop_global():
    """停止全局 OVSLogger."""
    logger = get_instance()
    logger.stop()


# ============================================================
# CLI / 自检
# ============================================================

if __name__ == '__main__':
    print("=" * 60)
    print("🔍 OVSLogger 自检模式（跑 10 秒）")
    print("=" * 60)

    logger = OVSLogger()
    print(f"\n启动 OVSLogger...")
    if not logger.start():
        print("启动失败，退出")
        sys.exit(1)

    # OVSLogger 启动后, tail -F 会先 dump 所有历史日志（从 OVS 启动到现在的所有日志），
    # 然后跟新日志。历史日志的时间戳可能远早于 now，
    # 所以 since=now 会过滤掉历史日志——我们要看 buffer 里所有内容。
    since = 0  # 看所有历史日志
    print(f"\n监控中... 10 秒后输出统计")
    try:
        time.sleep(10)
    except KeyboardInterrupt:
        pass

    stats = logger.get_stats()
    print(f"\n📊 统计:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    # 显示 buffer 里最近的日志（buffer 最多 5000 条，看最后 10 条）
    all_logs = logger.get_logs(since=since)
    print(f"\n📜 buffer 里共 {len(all_logs)} 条 OVS 日志（最近 10 条）:")
    for log in all_logs[-10:]:
        print(f"  [{log['ts_str']}] [{log['module']:<10}] [{log['level']:<5}] "
              f"{log['msg'][:100]}")

    # 演示归因：找包含 'port' 或 'link' 关键词的日志
    port_logs = logger.get_logs(since=since, keyword='port')
    print(f"\n🔗 跟 port 相关的日志: {len(port_logs)} 条（最近 5 条）:")
    for log in port_logs[-5:]:
        print(f"  [{log['ts_str']}] [{log['module']}] {log['msg'][:100]}")

    # 额外演示：找 connmgr 模块的日志（看 OpenFlow 控制器连接）
    connmgr_logs = logger.get_logs(since=since, module='connmgr')
    print(f"\n🔌 connmgr 模块日志: {len(connmgr_logs)} 条（最近 5 条）:")
    for log in connmgr_logs[-5:]:
        print(f"  [{log['ts_str']}] {log['msg'][:120]}")

    logger.stop()
    print("\n✅ 自检完成")
