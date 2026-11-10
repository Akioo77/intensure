"""exec_target.py —— 「在目标设备上执行动作」的统一入口。

为什么需要这层
--------------
现在仓库里所有「要在设备上动手」的代码都是直接 `subprocess` 跑本机命令：

    fault_api.py     sudo tc qdisc / ovs-ofctl mod-port / ip link
    live_env.py      mnexec -a <pid> ping        （还依赖 Mininet Python API）
    ovs_logger.py    读 /var/log/openvswitch/ovs-vswitchd.log

这些代码隐含一个假设：**那台跑着 OVS 的机器就是我们自己这台**。
这个假设在两种场景下不成立：

  1. 联调：Mininet 是**别人**起的（对方 VM / 对方笔记本），我们只负责采集
  2. 目标：最终服务整个园区网络，交换机是真实设备、分布在不同机架

但要注意 —— **采集链路本身已经网络化了**：`controller_app.py` 走 OpenFlow/LLDP，
`/api/intensure/v1/*` 走 HTTP，DB 走 DSN，这些跨机都不需要改。
真正需要适配的只有「要落到某台设备上执行的动作」—— 也就是本模块负责的三类：
**探测、注故障、读设备日志**。

三种实现
--------
  LocalExec  本机 subprocess（现状默认，行为完全不变）
  SshExec    通过 ssh 在目标主机上执行（联调对方起的 Mininet 用这个）
  AgentExec  生产网禁 SSH 时用远端 HTTP agent（device_agent.py）

设计约束
--------
* 默认行为必须与改造前**逐字节一致** —— 否则现有 216 例测试与演示脚本全废。
* 远端命令要正确转义 —— 走 ssh 时参数用 shlex.quote 拼成远端 shell 串。
* 所有远端调用都要有超时 —— ssh 卡住不能拖垮 state_server 的请求线程。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# 一次命令的默认超时（秒）。不给上限的话，ovs-ofctl 在异常拓扑上可能挂住，
# 而它跑在 HTTP 请求线程里 —— 一个挂住的请求就能拖垮整个 state_server。
DEFAULT_TIMEOUT = 20.0

# ping 输出解析
_LOSS_RE = re.compile(r'(\d+(?:\.\d+)?)% packet loss')
# ⚠️ 旧写法 `avg[^=]*= ([\d.]+)` 取的是 **min** 不是 avg ——
#    "rtt min/avg/max/mdev = 0.045/0.052/0.061/0.007 ms" 会匹配到 0.045（min），
#    而字段名叫 latency_ms，口径不对。这里按 min/avg/max 顺序取**第 2 个**（avg）。
_RTT_RE = re.compile(
    r'rtt\s+min/avg/max(?:/mdev)?\s*=\s*([\d.]+)/([\d.]+)(?:/([\d.]+))?')
# 老格式兜底（部分 busybox ping 没有 mdev）
_RTT_FALLBACK_RE = re.compile(r'avg[^=]*=\s*([\d.]+)')


def parse_ping_output(text: str) -> Tuple[float, Optional[float]]:
    """从 ping 输出里解析 (丢包率 %, 平均 RTT ms)。

    RTT 取 avg（不是 min）—— 口径要与字段名 latency_ms 对得上。
    解析不出来时：丢包率给 100（按不可达处理），RTT 给 None。
    """
    out = text or ''
    m = _LOSS_RE.search(out)
    loss = float(m.group(1)) if m else 100.0
    m2 = _RTT_RE.search(out)
    if m2:
        return loss, float(m2.group(2))
    m3 = _RTT_FALLBACK_RE.search(out)
    return loss, (float(m3.group(1)) if m3 else None)



@dataclass
class CommandResult:
    """一条命令的执行结果。"""

    returncode: int
    stdout: str = ''
    stderr: str = ''
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """stdout + stderr 合并（有些工具把有用信息写在 stderr）。"""
        return (self.stdout or '') + (self.stderr or '')


class ExecTarget:
    """执行目标基类。

    子类必须实现 `run()`；其余方法有默认实现（基于 run()）。
    """

    kind = 'base'

    # ------------------------------------------------------------------
    # 必须实现
    # ------------------------------------------------------------------
    def run(self, argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 通用能力（基于 run）
    # ------------------------------------------------------------------
    def describe(self) -> str:
        """人类可读的目标描述（写进日志、API 响应、事件记录）。"""
        return self.kind

    def ping(self, src: str, dst: str, count: int = 3,
             timeout: float = 6.0) -> Tuple[float, Optional[float]]:
        """从 src 发起 ping 到 dst。

        src 语义：
          - 非空且能定位到 Mininet 主机命名空间（'h1'）→ 进那个命名空间 ping
          - 空 / 定位不到 → 从**目标主机自身**发起 ping（真实设备场景）

        返回 (loss_pct, latency_ms)；latency 为 None 表示没拿到 RTT。
        失败一律按 100% 丢包处理 —— 探测循环不能因为一次失败就崩。
        """
        pid = self.resolve_source_pid(src) if src else None
        if pid:
            argv = ['mnexec', '-a', str(pid), 'ping', '-c', str(count), '-W', '1', dst]
        else:
            argv = ['ping', '-c', str(count), '-W', '1', dst]
        try:
            r = self.run(argv, timeout=timeout)
            # 对方的 Mininet 可能是 **root 起的**（start_all.sh 就是 sudo python3 跑的），
            # 那进它的网络命名空间需要 root —— 第一次 mnexec 会报
            # "/proc/<pid>/ns/net: Permission denied"。
            # 注意：自己起的 Mininet（当前用户）不需要 sudo，所以是「失败再补 sudo」。
            if (not r.ok and pid
                    and 'permission denied' in (r.stderr or '').lower()):
                r = self.run(['sudo', '-n'] + argv, timeout=timeout)
        except Exception as e:            # 超时 / ssh 断了等
            logger.warning('[exec] ping 失败（%s → %s）: %s', src or '(本机)', dst, e)
            return (100.0, None)
        if not r.ok and not r.output:
            return (100.0, None)
        loss, latency = parse_ping_output(r.output)
        if not r.ok and loss == 0.0:
            # 命令本身失败了（但输出里碰巧有 0% packet loss）→ 按不可达处理
            loss = 100.0
        return (loss, latency)

    def read_file(self, path: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """读目标上的一个文件（远端走 cat）。"""
        r = self.run(['cat', path], timeout=timeout)
        return r.stdout if r.ok else ''

    def tail_file(self, path: str, sudo: bool = False) -> Optional[subprocess.Popen]:
        """持续跟踪目标上的文件（日志跟随）。远端走 ssh + tail -F。

        sudo=True 时用 `sudo -n`（OVS 日志目录是 root 权限，且不能弹密码）。

        返回 None 表示不支持（调用方要能降级，不要崩）。
        """
        return None

    def resolve_source_pid(self, src: str) -> Optional[int]:
        """把源主机名解析成目标主机上的命名空间 PID（公开：调用方也可能需要）。

        Mininet 的主机进程 cmdline 是 `bash --norc --noediting -is mininet:h1`，
        所以用 pgrep 按 `mininet:<name>` 找。真实设备没有这个模式 → 返回 None。

        ⚠️ 模式必须**锚定结尾**（`mininet:h1$`）：
           宽松写法 `pgrep -f mininet:h1` 会把**执行这条命令的 shell 自己**也匹配上
           （它的命令行里就含 "mininet:h1"）→ 拿到错误的 PID →
           mnexec 进错命名空间 → ping 永远 100% 丢包。
           踩过：跨机接入时 ping 一直 100% 丢包，根因就是这个自匹配。
        """
        if not src:
            return None
        if isinstance(src, int):          # 已经是 PID
            return src
        pattern = f'mininet:{src}$'
        try:
            r = self.run(['pgrep', '-f', pattern], timeout=5.0)
        except Exception:
            return None
        if not r.ok:
            return None
        for line in (r.stdout or '').split():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None

    # 兼容旧名（内部使用）
    _resolve_source_pid = resolve_source_pid


class LocalExec(ExecTarget):
    """本机执行 —— 改造前的行为，一字不差。"""

    kind = 'local'

    def describe(self) -> str:
        return 'local'

    def run(self, argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
        cmd = list(argv)
        t0 = time.monotonic()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124, stdout='',
                                 stderr=f'命令超时（{timeout}s）: {" ".join(cmd)}',
                                 duration_ms=(time.monotonic() - t0) * 1000)
        except FileNotFoundError as e:
            return CommandResult(returncode=127, stdout='', stderr=str(e),
                                 duration_ms=(time.monotonic() - t0) * 1000)
        return CommandResult(returncode=r.returncode, stdout=r.stdout or '',
                             stderr=r.stderr or '',
                             duration_ms=(time.monotonic() - t0) * 1000)

    def tail_file(self, path: str, sudo: bool = False) -> Optional[subprocess.Popen]:
        cmd = ['tail', '-F', '-n', '+1', '--', path]
        if sudo:
            cmd = ['sudo', '-n'] + cmd
        try:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
        except Exception as e:
            logger.warning('[exec] tail %s 失败: %s', path, e)
            return None


class SshExec(ExecTarget):
    """通过 ssh 在目标主机上执行。

    联调场景：Mininet 是对方起的，我们用 SSH 过去打命令。
    真实园区场景：生产网常禁 SSH → 换 AgentExec（本轮未实现）。

    注意远端也要能免密 sudo（命令里带 `sudo -n`，拿不到就立刻失败，
    不会卡在密码提示上）。
    """

    kind = 'ssh'

    def __init__(self, host: str, user: str = '', port: int = 22,
                 key: str = '', connect_timeout: int = 5):
        if not host:
            raise ValueError('SshExec 需要 host')
        self.host = host
        self.user = user or ''
        self.port = int(port or 22)
        self.key = os.path.expanduser(key) if key else ''
        self.connect_timeout = int(connect_timeout or 5)

    def describe(self) -> str:
        who = f'{self.user}@' if self.user else ''
        return f'ssh://{who}{self.host}:{self.port}'

    def _ssh_argv(self, remote_cmd: str) -> List[str]:
        argv = ['ssh',
                '-o', 'BatchMode=yes',            # 禁止交互式密码提示（挂了也是超时失败）
                '-o', f'ConnectTimeout={self.connect_timeout}',
                '-o', 'StrictHostKeyChecking=accept-new']
        if self.key:
            argv += ['-i', self.key]
        if self.port and self.port != 22:
            argv += ['-p', str(self.port)]
        dest = f'{self.user}@{self.host}' if self.user else self.host
        argv += [dest, '--', remote_cmd]
        return argv

    def run(self, argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
        cmd = list(argv)
        # 参数逐个 quote 后拼成远端 shell 串 —— 交给远端 shell 解析
        remote_cmd = ' '.join(shlex.quote(a) for a in cmd)
        full = self._ssh_argv(remote_cmd)
        t0 = time.monotonic()
        try:
            r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124, stdout='',
                                 stderr=f'ssh 命令超时（{timeout}s）: {remote_cmd}',
                                 duration_ms=(time.monotonic() - t0) * 1000)
        except FileNotFoundError as e:
            return CommandResult(returncode=127, stdout='', stderr=str(e),
                                 duration_ms=(time.monotonic() - t0) * 1000)
        return CommandResult(returncode=r.returncode, stdout=r.stdout or '',
                             stderr=r.stderr or '',
                             duration_ms=(time.monotonic() - t0) * 1000)

    def tail_file(self, path: str, sudo: bool = False) -> Optional[subprocess.Popen]:
        prefix = 'sudo -n ' if sudo else ''
        remote_cmd = f'{prefix}tail -F -n +1 -- {shlex.quote(path)}'
        try:
            return subprocess.Popen(self._ssh_argv(remote_cmd),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
        except Exception as e:
            logger.warning('[exec] ssh tail %s 失败: %s', path, e)
            return None


# ----------------------------------------------------------------------
# AgentExec —— 生产网禁 SSH 时的执行通道
# ----------------------------------------------------------------------

class AgentExec(ExecTarget):
    """通过设备侧 HTTP agent 在目标主机上执行命令。

    什么时候用
    ----------
    园区生产网通常**禁 SSH**（设备不开放 SSH 登录，只有受限带外管理网），
    而 SSH 一旦被禁，`SshExec` 就完全用不了。此时在设备上跑
    `device_agent.py`，采集层改走 HTTP。

    与 SshExec 的差别
    -----------------
    - 命令不是拼成 shell 串发过去，而是 **argv 数组**发过去 → 没有 shell 注入面
    - 设备侧有**命令白名单 + 只读开关 + 路径白名单 + token 认证**
      （详见 device_agent.py 顶部说明）→ 白名单外的命令由设备侧拒绝
    - 本地侧不持有任何设备凭据，只有 agent 的 token

    ⚠️ 本地端的 token 不做「回显」——出错信息里只说「认证失败」，
       不把 token 拼进异常消息（异常可能进日志、进 API 响应）。
    """

    kind = 'agent'

    def __init__(self, host: str, port: int = 9099, token: str = '',
                 scheme: str = 'http', timeout: float = DEFAULT_TIMEOUT,
                 verify_tls: bool = True):
        if not host:
            raise ValueError('AgentExec 需要 host')
        self.host = host
        self.port = int(port or 9099)
        self.token = token or ''
        self.scheme = (scheme or 'http').lower()
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.verify_tls = bool(verify_tls)

    def describe(self) -> str:
        return f'agent://{self.host}:{self.port}'

    @property
    def base_url(self) -> str:
        return f'{self.scheme}://{self.host}:{self.port}'

    def _request(self, path: str, payload: Optional[Dict[str, object]] = None,
                 method: str = 'POST', timeout: float = DEFAULT_TIMEOUT,
                 stream: bool = False):
        """发一个请求给 agent。返回 urllib 的 response 对象。"""
        import urllib.error
        import urllib.request

        url = f'{self.base_url}{path}'
        data = None
        headers = {'Accept': 'application/json'}
        if self.token:
            headers['X-Intensure-Token'] = self.token
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json; charset=utf-8'

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        # 本地地址不该走代理（VM 里常设 http_proxy 却没设 no_proxy → 劫持本机请求）
        handlers: List = [urllib.request.ProxyHandler({})]
        if self.scheme == 'https' and not self.verify_tls:
            handlers.append(urllib.request.HTTPSHandler(context=_unverified_ctx()))
        opener = urllib.request.build_opener(*handlers)
        return opener.open(req, timeout=timeout)

    def run(self, argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
        cmd = list(argv)
        t0 = time.monotonic()
        payload = {'argv': cmd, 'timeout': float(timeout)}
        # ⚠️ HTTP 超时必须**大于**命令超时，否则客户端会先断开，
        #    拿不到 agent 侧的结论（agent 正在超时处理中）——
        #    结果是「客户端超时」盖掉了「命令超时」这个真实原因，
        #    而且会打断 agent 的响应写入（BrokenPipe）。
        #    宽限 5 秒：足够 agent 回收子进程并返回 124。
        http_timeout = float(timeout) + 5.0
        try:
            with self._request('/agent/exec', payload, 'POST', http_timeout) as resp:
                body = resp.read().decode('utf-8')
            data = json.loads(body)
        except urllib.error.HTTPError as e:
            # 设备侧拒了（白名单 / 只读 / 认证）—— 把原因带回调用方，
            # 但**不要**把 token 拼进来。
            try:
                detail = json.loads(e.read().decode('utf-8')).get('error', '')
            except Exception:
                detail = ''
            msg = f'agent 拒绝执行（HTTP {e.code}）'
            if detail:
                msg += f': {detail}'
            return CommandResult(returncode=126, stdout='', stderr=msg,
                                 duration_ms=(time.monotonic() - t0) * 1000)
        except urllib.error.URLError as e:
            # URLError 包着 socket.timeout 时，说明是 HTTP 层超时，
            # 而 agent 侧多半已经在收尾（命令超时）→ 按 124 报，别报「不可达」。
            import socket
            reason = getattr(e, 'reason', None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return CommandResult(returncode=124, stdout='',
                                     stderr=f'agent 请求超时（{http_timeout}s）',
                                     duration_ms=(time.monotonic() - t0) * 1000)
            logger.warning('[exec] agent 不可达 (%s): %s', self.describe(), reason)
            return CommandResult(returncode=127, stdout='',
                                 stderr=f'agent 不可达（{self.describe()}）: {reason}',
                                 duration_ms=(time.monotonic() - t0) * 1000)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124, stdout='',
                                 stderr=f'agent 请求超时（{timeout}s）',
                                 duration_ms=(time.monotonic() - t0) * 1000)
        except Exception as e:
            return CommandResult(returncode=1, stdout='',
                                 stderr=f'{e.__class__.__name__}: {e}',
                                 duration_ms=(time.monotonic() - t0) * 1000)
        return CommandResult(
            returncode=int(data.get('returncode', 1)),
            stdout=data.get('stdout', '') or '',
            stderr=data.get('stderr', '') or '',
            duration_ms=float(data.get('duration_ms', 0) or 0),
        )

    def tail_file(self, path: str, sudo: bool = False) -> Optional[subprocess.Popen]:
        """远端跟文件 —— agent 用 HTTP 长连接下发。

        ⚠️ 与 SshExec/LocalExec 不同，这里返回的**不是 subprocess.Popen**，
           而是一个 _AgentTail 适配器（暴露 .pid / .stdout / .terminate / .wait），
           以满足 ovs_logger 对 tail 进程的接口要求。
        """
        if not path:
            return None
        try:
            resp = self._request(
                f'/agent/tail?path={urllib.parse.quote(path)}&follow=1',
                None, 'GET', self.timeout, stream=True)
        except Exception as e:
            logger.warning('[exec] agent tail %s 失败: %s', path, e)
            return None
        return _AgentTail(resp, path)


class _AgentTail:
    """模拟 subprocess.Popen 的最小接口，供 ovs_logger 用。

    ovs_logger 只需要四样东西：
        .pid          → 一个假的（非 0 即可）
        .stdout       → 有 readline() 的对象
        .terminate()  → 停止
        .wait(timeout)→ 等待结束
    """

    def __init__(self, resp, path: str):
        self._resp = resp
        self._path = path
        self._buf = ''
        self._eof = False
        self.pid = -1                 # 不是真实 PID；agent 那边的进程在设备上

    def _fill(self) -> bool:
        """再读一批数据进缓冲。返回 False 表示流结束。"""
        if self._eof:
            return False
        try:
            chunk = self._resp.readline()
        except Exception:
            self._eof = True
            return False
        if not chunk:
            self._eof = True
            return False
        try:
            obj = json.loads(chunk.decode('utf-8'))
            line = obj.get('line', '')
        except Exception:
            line = chunk.decode('utf-8', 'replace').rstrip('\n')
        self._buf += line + '\n'
        return True

    class _Stdout:
        def __init__(self, owner):
            self._owner = owner

        def readline(self) -> str:
            owner = self._owner
            while '\n' not in owner._buf:
                if not owner._fill():
                    # 流结束：把剩余内容作为最后一行返回（没有换行也算一行）
                    rest = owner._buf
                    owner._buf = ''
                    return rest
            line, owner._buf = owner._buf.split('\n', 1)
            return line + '\n'

        def read(self) -> str:
            owner = self._owner
            while owner._fill():
                pass
            rest = owner._buf
            owner._buf = ''
            return rest

        def __iter__(self):
            while True:
                line = self.readline()
                if not line:
                    return
                yield line

    @property
    def stdout(self):
        if not hasattr(self, '_stdout'):
            self._stdout = _AgentTail._Stdout(self)
        return self._stdout

    def terminate(self):
        try:
            self._resp.close()
        except Exception:
            pass
        self._eof = True

    def kill(self):
        self.terminate()

    def wait(self, timeout=None) -> int:
        self.terminate()
        return 0


def _unverified_ctx():
    """给 https + 不校验证书时用的 SSL context。"""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ----------------------------------------------------------------------
# 工厂
# ----------------------------------------------------------------------

def build_target(cfg: Optional[Dict[str, object]] = None) -> ExecTarget:
    """按配置造一个 ExecTarget。

    配置格式（env.json 的 "target" 段）：
        {
          "type": "local",            // local | ssh | agent
          "host": "",                 // ssh / agent 的目标
          "user": "",                 // ssh 用户
          "port": 22,                 // ssh 端口（agent 默认 9099）
          "key": "",                  // ssh 私钥路径
          "connect_timeout": 5,       // ssh 连接超时（秒）
          "token": "",                // agent 认证 token
          "scheme": "http",           // agent 协议：http / https
          "verify_tls": true          // agent https 时是否校验证书
        }

    读不出来 / type 未知 → 退回 LocalExec（保持改造前的行为）。
    """
    cfg = dict(cfg or {})
    t = str(cfg.get('type') or 'local').strip().lower()
    if t == 'local':
        return LocalExec()
    if t == 'ssh':
        host = str(cfg.get('host') or '').strip()
        if not host:
            logger.warning('[exec] target.type=ssh 但没配 host —— 退回 local')
            return LocalExec()
        return SshExec(host=host,
                       user=str(cfg.get('user') or '').strip(),
                       port=int(cfg.get('port') or 22),
                       key=str(cfg.get('key') or '').strip(),
                       connect_timeout=int(cfg.get('connect_timeout') or 5))
    if t == 'agent':
        host = str(cfg.get('host') or '').strip()
        if not host:
            logger.warning('[exec] target.type=agent 但没配 host —— 退回 local')
            return LocalExec()
        # token 优先取配置，其次取环境变量（环境变量更安全：不落进配置文件）
        token = str(cfg.get('token') or '').strip() \
            or os.environ.get('INTENSURE_AGENT_TOKEN', '').strip()
        # ⚠️ 端口用 agent_port（默认 9099），不是 ssh 的 port（默认 22）——
        #    target 段是深合并的，port 永远存在，不能拿它当 agent 端口。
        return AgentExec(host=host,
                         port=int(cfg.get('agent_port') or 9099),
                         token=token,
                         scheme=str(cfg.get('scheme') or 'http').strip(),
                         timeout=float(cfg.get('timeout') or DEFAULT_TIMEOUT),
                         verify_tls=bool(cfg.get('verify_tls', True)))
    logger.warning('[exec] 未知 target.type=%s —— 退回 local', t)
    return LocalExec()


def target_from_env_config() -> ExecTarget:
    """从 env.json / $INTENSURE_ENV_FILE 读 target 配置并造 ExecTarget。

    读配置失败 → 退回 LocalExec（不因为配置问题让整个服务起不来）。
    """
    try:
        import env_config
        cfg = env_config.get_config()
        return build_target(cfg.target)
    except Exception as e:
        logger.warning('[exec] 读 target 配置失败，用 local: %s', e)
        return LocalExec()
