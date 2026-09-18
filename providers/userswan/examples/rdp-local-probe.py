#!/usr/bin/env python3
import argparse
import socket
import sys
import time

# TPKT + X.224 Connection Request + RDP Negotiation Request (TLS|CredSSP)
REQ = bytes.fromhex('030000130ee000000000000100080003000000')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=3390)
    ap.add_argument('--timeout', type=float, default=20.0)
    args = ap.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(args.timeout)
    try:
        t0 = time.time()
        s.connect((args.host, args.port))
        print(f'LOCAL_CONNECT_OK {args.host}:{args.port}')
        s.sendall(REQ)
        print(f'RDP_NEGOTIATION_SENT bytes={len(REQ)}')
        data = s.recv(4096)
        dt = time.time() - t0
        if not data:
            print('RDP_PROBE_FAIL peer_closed_without_response')
            return 3
        print(f'RDP_RESPONSE bytes={len(data)} elapsed={dt:.3f}s hex={data[:64].hex()}')
        if len(data) >= 4 and data[0] == 0x03 and data[1] == 0x00:
            print('RDP_PROBE_OK tpkt_response_received')
            return 0
        print('RDP_PROBE_FAIL non_tpkt_response')
        return 4
    except Exception as e:
        print(f'RDP_PROBE_FAIL {type(e).__name__}: {e}')
        return 2
    finally:
        s.close()


if __name__ == '__main__':
    raise SystemExit(main())
