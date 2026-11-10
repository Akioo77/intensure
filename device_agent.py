#!/usr/bin/env python3
"""device_agent.py —— 设备侧执行代理（AgentExec 的服务端）

为什么需要它
------------
生产网（园区）通常**禁 SSH**：设备不开放 SSH 登录，运维通道只有受限的
带外管理网。而采集层的「探测 / 注故障 / 读 OVS 日志」三类动作都需要
**在目标设备上执行命令**（`exec_target.py` 的职责）。

SSH 被禁掉时，替代方案是在设备上跑一个小 HTTP 服务：
采集层 → HTTP POST → agent → 执行命令 → 回传结果。

安全边界（这是本模块最重要的地方）
----------------------------------
一个「HTTP 端点执行命令」的服务如果做成**任意命令**，等于在网络上开了一个
远程 shell —— 这是最危险的形态。所以这里做了四层收敛：

1. **命令白名单**：只允许白名单里的**命令前缀**（`ovs-ofctl` / `ovs-vsctl` /
   `ip` / `tc` / `ping` / `cat` / `pgrep` / `tail` …）。不在白名单 → 拒绝。
   白名单按**第一个参数**匹配，所以 `ovs-ofctl` 的所有子命令都放行，
   但 `rm` / `curl` / `bash` / `python` 一律拒绝。

2. **认证**：请求必须带 `X-Intensure-Token` 头（或 Bearer），值与启动时
   `--token` / 环境变量 `INTENSURE_AGENT_TOKEN` 一致。**没配 token 就拒绝启动**
   —— 不允许「默认无认证」的 agent 跑在网络上。

3. **只读 / 写分离**：默认 `--read-only` 打开，只允许只读命令；
   注故障（`ovs-ofctl mod-port` / `ip link set` / `tc qdisc`）属于写操作，
   必须显式 `--allow-write` 才放行。对应采集层的 `mode: read-only`。

4. **路径限制**：`cat` / `tail` 的路径参数必须在允许的前缀里
   （默认 `/var/log`、`/etc`、`/proc`、`/sys`）。防止借 `cat` 读密钥文件。

另外：agent **不解析 shell**，命令是 argv 数组直接 `subprocess` 执行，
不经过 `/bin/sh` —— 所以没有 shell 注入面。

用法
----
设备上:
    python3 device_agent.py --host 0.0.0.0 --port 9099 --token <随机长串>

采集机侧（env.json 的 target 段）:
    {
      "type": "agent",
      "host": "10.0.0.51",
      "port": 9099,
      "token": "<同一个 token>",
      "allow_write": false
    }

⚠️ token 走环境变量 `INTENSURE_AGENT_TOKEN` 更安全（不落进配置文件）。

接口
----
    POST /agent/exec     {"argv": ["ovs-ofctl", "show"], "timeout": 20}
        → {"returncode": 0, "stdout": "...", "stderr": "", "duration_ms": 12}
    GET  /agent/health   → {"status": "ok", "agent_version": "...", "read_only": true}
    GET  /agent/tail?path=...   → 跟随文件（SSE 风格逐行下发，用于 OVS 日志）
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

AGENT_VERSION = '1.0.0'

# ----------------------------------------------------------------------
# 命令白名单
# ----------------------------------------------------------------------
# key = 允许的命令名（第一个 argv 元素），value = 说明。
# ⚠️ 这里刻意**不含** shell / 解释器 / 下载器：
#    bash / sh / zsh / python / perl / env / curl / wget / nc / rm / mv …
#    一律不在白名单里 —— 它们会把「受限执行」变成「任意执行」。
READ_ONLY_COMMANDS: Dict[str, str] = {
    'ovs-ofctl': '查流表 / 端口状态',
    'ovs-vsctl': '查 OVS 数据库',
    'ovs-appctl': '查 OVS 运行时',
    'ip': '查网络接口',
    'ifconfig': '查网络接口',
    'tc': '查流量整形',
    'ping': '连通性探测',
    'ping6': '连通性探测',
    'traceroute': '路径探测',
    'cat': '读文件（受路径白名单限制）',
    'pgrep': '找进程',
    'ps': '列进程',
    'tail': '跟文件（受路径白名单限制）',
    'head': '读文件头（受路径白名单限制）',
    'grep': '文本过滤',
    'mnexec': '进 Mininet 命名空间执行',
    'ovs-dpctl': '查 datapath',
}

# 写命令：注故障要用，默认**不放行**（要 --allow-write）
#
# ⚠️ 关键：`ip` / `ovs-ofctl` / `tc` 这些命令**同一个二进制下有只读和写两种子命令**
#    （`ip link show` 是读，`ip link set` 是写）。如果按二进制整体分类，
#    只读模式会把 `ip link show` 也拒掉 —— agent 在真实场景下基本不可用。
#    所以这里按**子命令**细分：
#      - 该二进制在 READ_ONLY_SUBCOMMANDS 里的子命令 → 读
#      - 其余子命令 → 写（保守：不在只读列表里的一律当写）
WRITE_COMMANDS: Dict[str, str] = {
    'ovs-ofctl': 'mod-port / add-flow（改交换机行为）',
    'ovs-vsctl': 'set port（改 OVS 配置）',
    'ip': 'link set up/down（改接口状态）',
    'tc': 'qdisc（改流量整形）',
}

# 只读子命令白名单：命中 → 按只读处理；没命中 → 按写处理（保守默认）。
#
# ⚠️ 判定依据是「**子命令动词**」，不是完整参数。
#    `ip link show` / `ip -br link show` / `ip addr` 的动词分别是
#    link / -br / addr —— 这里列的是**安全的读类动词**。
# 写动作动词：出现在子命令里就说明这是改东西，不能当只读。
# ⚠️ 这些词单独作为子命令时都是「动词」：
#    ip link set / tc qdisc replace / ovs-ofctl add-flow / ovs-vsctl set …
WRITE_VERBS: Tuple[str, ...] = (
    'set', 'replace', 'add', 'del', 'delete', 'remove', 'insert',
    'reset', 'clear', 'change', 'modify', 'update', 'create', 'destroy',
    'flush', 'update', 'del-flows',
)

READ_ONLY_SUBCOMMANDS: Dict[str, Tuple[str, ...]] = {
    # 只读的 link 操作：show / list
    # ⚠️ ip 的只读判定要两段：`ip link show`（读） vs `ip link set`（写）。
    #    单独列 'link' 会把 ip link set 也判成读 —— 那就等于放行了写操作。
    #    所以这里只列**组合键**与确定安全的单段命令。
    'ip': ('addr', 'route', 'neigh', 'address', 'rule', 'link show',
           'addr show', 'route show', 'neigh show', 'rule show',
           'link get'),
    # 只读的 ovs-ofctl 操作：show / dump / dump-flows / dump-ports
    'ovs-ofctl': ('show', 'dump-flows', 'dump-ports', 'dump-ports-desc',
                  'dump-tables', 'dump-groups', 'dump-bundle'),
    # 只读的 ovs-vsctl 操作：show / get / list / find / list-*
    'ovs-vsctl': ('show', 'get', 'list', 'list-columns', 'list-tables',
                  'find', 'get-controller', 'list-uuids'),
    # 只读的 tc 操作：show / class / filter / qdisc show
    # ⚠️ tc 是「两层动词」：tc qdisc show（读） vs tc qdisc replace（写）。
    #    只看第一段会把 qdisc show 也判成写 → 只读模式下连查都不行。
    #    所以这里用 'qdisc show' 这种组合键，匹配前两段。
    'tc': ('show', 'class', 'filter', 'monitor', 'qdisc show', 'qdisc list',
           'class show', 'filter show'),
    # 只读的 ovs-appctl 操作：show / dpif/show / ofproto/trace
    'ovs-appctl': ('show', 'dpif/show', 'ofproto/trace', 'coverage/show',
                   'version'),
    # 只读的 ovs-dpctl
    'ovs-dpctl': ('show',),
}

# 读文件类命令的路径白名单前缀。
# ⚠️ 没有前缀白名单的话，`cat /etc/shadow`、`cat ~/.ssh/id_rsa` 就都能读。
READ_PATH_PREFIXES: Tuple[str, ...] = (
    '/var/log',
    '/etc/openvswitch',
    '/etc/ovs',
    '/proc',
    '/sys/class/net',
    '/run/openvswitch',
)

# 写命令允许触碰的路径（tc/ovs 用的网卡名不是路径，这里留给将来扩展）
WRITE_PATH_PREFIXES: Tuple[str, ...] = ()


class AgentError(Exception):
    """agent 内部错误（会变成 4xx/5xx 响应）。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class AgentPolicy:
    """agent 的执行策略（白名单 / 认证 / 只读开关）。"""

    def __init__(self, token: str = '', read_only: bool = True,
                 read_paths: Tuple[str, ...] = READ_PATH_PREFIXES,
                 extra_read_commands: Optional[List[str]] = None):
        self.token = token or ''
        self.read_only = read_only
        self.read_paths = tuple(read_paths or ())
        self.extra_read_commands = set(extra_read_commands or ())

    # -- 认证 ---------------------------------------------------------
    def check_token(self, provided: str) -> None:
        if not self.token:
            raise AgentError('agent 未配置 token（拒绝无认证请求）', 503)
        if not provided or provided != self.token:
            raise AgentError('token 不匹配', 401)

    # -- 命令 ---------------------------------------------------------
    def classify(self, argv: List[str]) -> Tuple[str, bool]:
        """判断命令是否放行。

        返回 (类别, 是否写操作)，类别取值 'read' / 'write'。
        放行则正常返回，不放行抛 AgentError。

        ⚠️ 分类**按子命令**细分（不是按二进制）：
           `ip link show` 是读、`ip link set` 是写 —— 都用 `ip` 这个二进制。
           规则：命中只读子命令白名单 → 读；否则 → 写（保守默认）。
           这样只读模式下 agent 依然能做「查询」，只是不能改东西。
        """
        if not argv:
            raise AgentError('argv 为空')
        # 去掉 sudo 前缀再判断 —— sudo 本身不在白名单，但命令在白名单里
        # 就应该放行（远端 agent 通常直接以 root 跑）。
        cmd = argv[0]
        if cmd == 'sudo':
            # sudo -n xxx / sudo xxx
            rest = argv[1:]
            while rest and rest[0].startswith('-'):
                rest = rest[1:]
            if not rest:
                raise AgentError('sudo 后面没有命令')
            argv = rest
            cmd = argv[0]

        # 该命令有没有「只读子命令」这一说？
        has_sub = cmd in READ_ONLY_SUBCOMMANDS and cmd in WRITE_COMMANDS
        if has_sub:
            if self._is_read_subcommand(cmd, argv[1:]):
                return 'read', False
            # 不是只读子命令 → 属于写
            if self.read_only:
                raise AgentError(
                    f'写操作在只读模式下被拒绝：{cmd}（{WRITE_COMMANDS[cmd]}）', 403)
            return 'write', True

        if cmd in WRITE_COMMANDS:
            if self.read_only:
                raise AgentError(
                    f'写操作在只读模式下被拒绝：{cmd}（{WRITE_COMMANDS[cmd]}）', 403)
            return 'write', True
        if cmd in READ_ONLY_COMMANDS or cmd in self.extra_read_commands:
            return 'read', False
        raise AgentError(f'命令不在白名单里：{cmd}', 403)

    @staticmethod
    def _is_read_subcommand(cmd: str, rest: List[str]) -> bool:
        """判断是不是只读子命令。

        规则：取参数里**第一个非选项词**（跳过 -x 之类的选项），
        看它在不在只读子命令白名单里。
        ⚠️ 保守：如果一个非选项词都找不到（比如 `ovs-ofctl -O OpenFlow13`），
        判为**不是**只读 → 当写操作处理。

        另外支持**两段动词**：`tc qdisc show` 这种（tc 的只读动词是第二段）。
        先试「第一段+第二段」的组合键，再试单段。
        """
        allowed = READ_ONLY_SUBCOMMANDS.get(cmd, ())
        words = []
        for a in rest:
            if a.startswith('-'):
                continue
            # 跳过选项的参数值（比如 -O OpenFlow13 → OpenFlow13 不是子命令）
            if a.upper() in ('OPENFLOW13', 'OPENFLOW10', 'OF10', 'OF13'):
                continue
            words.append(a)
        if not words:
            return False
        # 两段动词优先（tc qdisc show / ip link show）
        if len(words) >= 2:
            combo = f'{words[0]} {words[1]}'
            if combo in allowed:
                return True
            # ⚠️ 关键：第二段是**动词**时，若该动词本身就是写动作
            #    （set / replace / add / del / reset …），必须判为写。
            #    否则 `ip link set` 会因为 'link' 单段在白名单里而被放行。
            if words[1] in WRITE_VERBS:
                return False
        return words[0] in allowed

    # -- 路径 ---------------------------------------------------------
    def check_path(self, argv: List[str]) -> None:
        """读文件类命令的路径必须在白名单前缀内。"""
        cmd = argv[0]
        if cmd not in ('cat', 'tail', 'head'):
            return
        for a in argv[1:]:
            if a.startswith('-'):
                continue
            if not any(a.startswith(p) for p in self.read_paths):
                raise AgentError(f'路径不在白名单里：{a}', 403)


def _exec(argv: List[str], timeout: float) -> Dict[str, object]:
    """执行一条命令（不经过 shell）。"""
    t0 = time.monotonic()
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {
            'returncode': r.returncode,
            'stdout': r.stdout or '',
            'stderr': r.stderr or '',
            'duration_ms': round((time.monotonic() - t0) * 1000, 2),
        }
    except subprocess.TimeoutExpired:
        return {
            'returncode': 124,
            'stdout': '',
            'stderr': f'命令超时（{timeout}s）: {shlex.join(argv)}',
            'duration_ms': round((time.monotonic() - t0) * 1000, 2),
        }
    except FileNotFoundError as e:
        return {
            'returncode': 127,
            'stdout': '',
            'stderr': str(e),
            'duration_ms': round((time.monotonic() - t0) * 1000, 2),
        }
    except Exception as e:                     # noqa: BLE001
        return {
            'returncode': 1,
            'stdout': '',
            'stderr': f'{e.__class__.__name__}: {e}',
            'duration_ms': round((time.monotonic() - t0) * 1000, 2),
        }


class _Handler(BaseHTTPRequestHandler):
    """HTTP 处理。policy 由 server 注入。"""

    server_version = f'intensure-agent/{AGENT_VERSION}'
    policy: AgentPolicy = None       # type: ignore[assignment]

    # 静音默认的 stderr 日志（每条请求都打一行太吵）
    def log_message(self, fmt, *args):
        if os.environ.get('INTENSURE_AGENT_VERBOSE'):
            sys.stderr.write('[agent] ' + (fmt % args) + '\n')

    # -- 工具 ---------------------------------------------------------
    def _send_json(self, code: int, payload: Dict[str, object]):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端等不及先断了（比如客户端超时比命令超时短）。
            # 这不是 agent 的错，别把栈打到日志里。
            pass

    def _token_from_request(self) -> str:
        token = self.headers.get('X-Intensure-Token') or ''
        if token:
            return token.strip()
        auth = self.headers.get('Authorization') or ''
        if auth.lower().startswith('bearer '):
            return auth[7:].strip()
        return ''

    # -- 路由 ---------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == '/agent/health':
                self.policy.check_token(self._token_from_request())
                self._send_json(200, {
                    'status': 'ok',
                    'agent_version': AGENT_VERSION,
                    'read_only': self.policy.read_only,
                })
                return
            if parsed.path == '/agent/tail':
                self._handle_tail(parse_qs(parsed.query))
                return
            self._send_json(404, {'error': '未知路径'})
        except AgentError as e:
            self._send_json(e.status, {'error': e.message})
        except Exception as e:                     # noqa: BLE001
            self._send_json(500, {'error': f'{e.__class__.__name__}: {e}'})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path != '/agent/exec':
                self._send_json(404, {'error': '未知路径'})
                return
            self.policy.check_token(self._token_from_request())
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0:
                raise AgentError('请求体为空')
            if length > 64 * 1024:
                raise AgentError('请求体太大', 413)
            try:
                body = json.loads(self.rfile.read(length).decode('utf-8'))
            except Exception as e:                 # noqa: BLE001
                raise AgentError(f'JSON 解析失败: {e}') from e
            argv = body.get('argv')
            if not isinstance(argv, list) or not argv:
                raise AgentError('argv 必须是非空数组')
            if not all(isinstance(a, str) for a in argv):
                raise AgentError('argv 元素必须是字符串')
            timeout = float(body.get('timeout') or 20)
            timeout = max(1.0, min(timeout, 120.0))

            kind, _is_write = self.policy.classify(argv)
            self.policy.check_path(argv)
            result = _exec(argv, timeout)
            result['kind'] = kind
            self._send_json(200, result)
        except AgentError as e:
            self._send_json(e.status, {'error': e.message})
        except Exception as e:                     # noqa: BLE001
            self._send_json(500, {'error': f'{e.__class__.__name__}: {e}'})

    # -- tail ---------------------------------------------------------
    def _handle_tail(self, query: Dict[str, List[str]]):
        """跟随文件，逐行下发（text/plain, 每行一条 JSON）。"""
        self.policy.check_token(self._token_from_request())
        path = (query.get('path') or [''])[0]
        if not path:
            raise AgentError('缺少 path 参数')
        self.policy.check_path(['tail', path])
        follow = (query.get('follow') or ['1'])[0] not in ('0', 'false')
        lines_back = int((query.get('lines') or ['50'])[0])

        argv = ['tail', '-F', '-n', str(max(0, lines_back)), '--', path]
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.end_headers()
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except Exception as e:                     # noqa: BLE001
            self.wfile.write((json.dumps({'error': str(e)}) + '\n').encode('utf-8'))
            return
        try:
            if follow:
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    self.wfile.write((json.dumps({'line': line.rstrip('\n')})
                                      + '\n').encode('utf-8'))
                    self.wfile.flush()
            else:
                out = subprocess.run(argv, capture_output=True, text=True, timeout=10)
                for line in (out.stdout or '').splitlines():
                    self.wfile.write((json.dumps({'line': line}) + '\n').encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.terminate()
            except Exception:
                pass


def build_server(host: str = '127.0.0.1', port: int = 9099, token: str = '',
                 read_only: bool = True,
                 extra_read_commands: Optional[List[str]] = None
                 ) -> ThreadingHTTPServer:
    """造一个 agent server（测试与 main 共用）。"""
    handler = type('BoundHandler', (_Handler,), {
        'policy': AgentPolicy(token=token,
                              read_only=read_only,
                              extra_read_commands=extra_read_commands)})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='intensure 设备侧执行代理')
    parser.add_argument('--host', default='127.0.0.1',
                        help='监听地址（跨机访问要设 0.0.0.0，⚠️ 记得配防火墙）')
    parser.add_argument('--port', type=int, default=9099)
    parser.add_argument('--token', default=os.environ.get('INTENSURE_AGENT_TOKEN', ''),
                        help='认证 token（也可用环境变量 INTENSURE_AGENT_TOKEN）')
    parser.add_argument('--allow-write', action='store_true',
                        help='放行写操作（注故障）。默认只读')
    args = parser.parse_args(argv)

    if not args.token:
        print('❌ 必须提供 token（--token 或环境变量 INTENSURE_AGENT_TOKEN）',
              file=sys.stderr)
        print('   没有认证的 agent 等于在网络上开远程 shell，不允许启动。',
              file=sys.stderr)
        return 2

    read_only = not args.allow_write
    srv = build_server(args.host, args.port, args.token, read_only)
    print(f'✅ intensure agent v{AGENT_VERSION} 已启动')
    print(f'   监听      : {args.host}:{args.port}')
    print(f'   模式      : {"只读" if read_only else "可写（⚠️ 能注故障）"}')
    print('   认证      : X-Intensure-Token / Bearer')
    print(f'   命令白名单: {len(READ_ONLY_COMMANDS)} 个只读命令'
          + (f' + {len(WRITE_COMMANDS)} 类写操作' if not read_only else ''))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n收到中断，退出')
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
