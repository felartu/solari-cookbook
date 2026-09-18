# No-TUN self-test

Run:

```bash
make selftest
```

The test creates two temporary WireGuard keypairs and starts two userguard instances:

```text
native client -> 127.0.0.1:18080
                 |
                 v
        userguard peer A
        gVisor 10.77.0.1
                 |
        encrypted WireGuard UDP
                 |
        userguard peer B
        gVisor 10.77.0.2:8080
                 |
                 v
       native 127.0.0.1:19090 echo server
```

Peer A listens for WireGuard UDP on `127.0.0.1:51820`. Peer B listens on `127.0.0.1:51821`.

The test verifies:

1. both runtimes reach `USERGUARD_READY`;
2. neither process has an fd referring to `/dev/net/tun`;
3. a payload enters the native forward on peer A;
4. a WireGuard handshake/data exchange occurs;
5. peer B accepts the TCP connection in its userspace netstack;
6. the payload reaches a native echo service and returns unchanged.

Expected result:

```text
USERGUARD_SELFTEST_OK payload_roundtrip=1
```

The test intentionally retries the TCP connection briefly because WireGuard handshakes are demand-driven and process startup scheduling is nondeterministic.
