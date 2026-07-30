"""Local TCP relay: 127.0.0.1:7456 -> 127.0.0.1:<daemon_port>.

Open Design's MCP server hard-codes the daemon URL at 127.0.0.1:7456, but the
Electron app binds its daemon to a random high port. This relay bridges the two
so the MCP tools work without restarting the app.

Usage: python od_relay.py <daemon_port> [listen_port]
"""
import socket
import sys
import threading

LISTEN_HOST = "127.0.0.1"


def pump(src, dst):
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


def handle(client, target_port):
    try:
        upstream = socket.create_connection((LISTEN_HOST, target_port), timeout=10)
    except OSError as exc:
        print(f"upstream connect failed: {exc}", flush=True)
        client.close()
        return
    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pump, args=(upstream, client), daemon=True).start()


def main():
    target_port = int(sys.argv[1])
    listen_port = int(sys.argv[2]) if len(sys.argv) > 2 else 7456
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, listen_port))
    srv.listen(64)
    print(f"relay {LISTEN_HOST}:{listen_port} -> {LISTEN_HOST}:{target_port}", flush=True)
    while True:
        client, _ = srv.accept()
        handle(client, target_port)


if __name__ == "__main__":
    main()
