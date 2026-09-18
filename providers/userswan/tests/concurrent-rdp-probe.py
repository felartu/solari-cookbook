#!/usr/bin/env python3
from __future__ import annotations
import argparse
import socket
import threading
import time

REQ = bytes.fromhex('030000130ee000000000000100080003000000')


def worker(idx: int, host: str, port: int, timeout: float, hold: float, barrier: threading.Barrier, results: list[str]) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        barrier.wait(timeout=timeout)
        t0 = time.time()
        s.connect((host, port))
        local = s.getsockname()
        s.sendall(REQ)
        data = s.recv(4096)
        dt = time.time() - t0
        if len(data) >= 4 and data[:2] == b'\x03\x00':
            results[idx] = f'OK idx={idx} local={local[0]}:{local[1]} bytes={len(data)} elapsed={dt:.3f}s'
            if hold:
                time.sleep(hold)
        else:
            results[idx] = f'FAIL idx={idx} non_tpkt bytes={len(data)} hex={data[:32].hex()}'
    except Exception as e:
        results[idx] = f'FAIL idx={idx} {type(e).__name__}: {e}'
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description='Open concurrent localhost RDP negotiation sessions.')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=3390)
    ap.add_argument('--connections', type=int, default=3)
    ap.add_argument('--timeout', type=float, default=10.0)
    ap.add_argument('--hold', type=float, default=2.0)
    a = ap.parse_args()
    if a.connections < 1:
        return 2
    barrier = threading.Barrier(a.connections)
    results = [''] * a.connections
    threads = [threading.Thread(target=worker, args=(i, a.host, a.port, a.timeout, a.hold, barrier, results)) for i in range(a.connections)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for r in results:
        print(r)
    ok = sum(r.startswith('OK ') for r in results)
    print(f'CONCURRENT_RDP_RESULT ok={ok} total={a.connections}')
    return 0 if ok == a.connections else 1


if __name__ == '__main__':
    raise SystemExit(main())
