"""把宿主 loopback 上的 HTTP 代理转发到 kind 网桥网关，让集群里的 Pod 够得着。

为什么需要它（2026-10-23 实测）：Pod 直连 github.com:443 超时（同一时刻
api.github.com 却回 200），而宿主代理只绑在 127.0.0.1，Pod 连它会 connection
refused。ArgoCD 的 repo-server 要在 Pod 里 clone 仓库，所以必须在网桥网关上开
一个转发口。完整实测矩阵见 deploy/README.md §1.6。

这是个纯 TCP 转发，不解析 HTTP——CONNECT 隧道与明文请求都原样透传，所以对
https 也够用，且不需要知道上游是什么代理软件。

由 scripts/host-proxy-relay.sh 负责起停，一般不直接跑这个文件。
"""

import socket
import sys
import threading

CHUNK = 65536


def pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (src, dst):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket, upstream_host: str, upstream_port: int) -> None:
    try:
        upstream = socket.create_connection((upstream_host, upstream_port), 10)
    except OSError:
        client.close()
        return
    for pair in ((client, upstream), (upstream, client)):
        threading.Thread(target=pipe, args=pair, daemon=True).start()


def main() -> int:
    if len(sys.argv) != 4:
        print(f"用法: {sys.argv[0]} <listen_port> <upstream_host> <upstream_port>")
        return 2
    listen_port = int(sys.argv[1])
    upstream_host = sys.argv[2]
    upstream_port = int(sys.argv[3])

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", listen_port))
    listener.listen(128)
    # 打到 stderr 而不是 stdout：调用方要靠 stdout 干净来判断状态。
    print(f"relay 0.0.0.0:{listen_port} -> {upstream_host}:{upstream_port}", file=sys.stderr, flush=True)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handle, args=(conn, upstream_host, upstream_port), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
