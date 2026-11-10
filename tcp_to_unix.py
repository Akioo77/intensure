#!/usr/bin/env python3
"""tcp_to_unix.py —— 把 TCP 端口桥到 unix socket（跨机验证用的小工具）

用途：Colima 的 SSH 走 unix socket（~/.colima/_lima/colima/ssh.sock），
宿主上没有 socat，所以用标准库 socket 做一个最小桥：

    python3 tcp_to_unix.py <unix_socket_path> <listen_port> [listen_host]

⚠️ 这是**本地开发/验证**工具，不是生产组件：
   - 没有 TLS，token 明文传输 → 只在受控网络里用
   - 生产环境应该用 device_agent.py 本身跑在设备上，
     配合防火墙 / mTLS / VPN
"""
import os
import socket
import sys
import threading


def pump(src: socket.socket, dst: socket.socket):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket, sock_path: str):
    try:
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.connect(sock_path)
    except OSError as e:
        sys.stderr.write(f'连接 {sock_path} 失败: {e}\n')
        client.close()
        return
    t1 = threading.Thread(target=pump, args=(client, up), daemon=True)
    t2 = threading.Thread(target=pump, args=(up, client), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    client.close()
    up.close()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    sock_path = sys.argv[1]
    port = int(sys.argv[2])
    host = sys.argv[3] if len(sys.argv) > 3 else '127.0.0.1'

    if not os.path.exists(sock_path):
        print(f'socket 不存在: {sock_path}', file=sys.stderr)
        return 2

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    print(f'桥已就绪: {host}:{port} → {sock_path}', flush=True)
    try:
        while True:
            client, _ = srv.accept()
            threading.Thread(target=handle, args=(client, sock_path),
                             daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
