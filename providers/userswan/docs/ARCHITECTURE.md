# Architecture

## Control plane

strongSwan remains the IPsec control plane. `charon` performs IKEv2, PSK authentication, proposal negotiation, NAT detection, NAT-T, virtual-IP assignment, CHILD_SA creation/rekey/delete and ESP key management. `swanctl` communicates with charon over VICI.

The runtime deliberately configures:

```text
routing_table = 0
install_routes = no
install_virtual_ip = no
kernel-libipsec.no_tun = yes
```

The assigned virtual IP still exists logically inside the IKE/CHILD_SA state and is passed to lwIP, but it is not installed as a Linux interface address.

## Data plane

### Outbound

1. A local application connects to `127.0.0.1:<local-port>` using ordinary kernel TCP.
2. `userswan-forwarder` accepts that connection.
3. The forwarder creates a second TCP connection in lwIP using the VPN-assigned virtual IPv4 address as the userspace interface address.
4. lwIP emits complete IPv4/TCP packets for `<VIP>:ephemeral -> <TARGET_IP>:<TARGET_PORT>`.
5. The forwarder's lwIP netif output callback sends each complete IPv4 packet to charon's Unix datagram packet socket.
6. Patched `kernel-libipsec` queues the packet through `ipsec->processor->queue_outbound()`.
7. strongSwan performs policy selection, ESP encryption, sequence/replay bookkeeping and NAT-T encapsulation.
8. charon sends the UDP/4500 packet through the normal physical network interface.

### Inbound

1. charon receives UDP/4500 and identifies ESP-in-UDP.
2. libipsec authenticates/decrypts ESP and recovers the inner IPv4 packet.
3. In no-TUN mode, the patched router sends the decrypted inner packet to the registered Unix datagram peer instead of writing to a TUN fd.
4. `userswan-forwarder` copies the IPv4 packet into an lwIP pbuf and submits it with `tcpip_input()`.
5. lwIP advances the remote TCP state machine and exposes received stream bytes.
6. The forwarder writes those bytes to the accepted localhost kernel TCP socket.

## Concurrent TCP sessions

The forwarder accepts localhost connections continuously and dispatches each accepted socket to an independent session worker. A session creates its own `lwip_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP)`, which gives it a distinct lwIP TCP PCB and ephemeral source port. No TCP PCB is shared between Remmina channels.

All sessions intentionally share the same lwIP netif and the same strongSwan Unix packet socket. This is safe because lwIP demultiplexes inbound TCP by the inner IPv4/TCP tuple and the Debian lwIP build uses the threaded TCP/IP core/socket API. The packet adapter transports complete inner IP packets and is not session-aware; TCP demultiplexing belongs to lwIP.

The reference process defaults to `--max-sessions 32` to bound native worker threads and lwIP resources. `MAX_SESSIONS` in `run-userswan.sh` maps to this option.

Receive timeouts are used only as periodic wakeups so workers can observe shutdown. `EAGAIN`/`EWOULDBLOCK` does not close an idle connection; this is important for long-lived RDP channels.

## Patched strongSwan components

The patch modifies only `src/libcharon/plugins/kernel_libipsec/`.

### `kernel_libipsec_plugin.c`

Adds settings:

```text
%s.plugins.kernel-libipsec.no_tun
%s.plugins.kernel-libipsec.packet_socket
```

When no-TUN mode is enabled, the plugin does not create/open a TUN interface for the plaintext packet path and does not require TUN-driven route/interface installation.

### `kernel_libipsec_ipsec.c`

Route installation becomes a successful no-op in no-TUN mode while the libipsec policy/SAs remain active.

### `kernel_libipsec_router.c`

Adds the Unix packet transport. Outbound plaintext packets received from the userspace peer are wrapped as strongSwan packets and queued to libipsec. Decrypted inbound plaintext packets are returned to the last registered Unix datagram sender.

The current Unix transport is intentionally single-consumer oriented. For a production daemon supporting multiple independent forwarding processes, replace "last sender" ownership with an explicit registration/multiplexing protocol or one central packet broker.

## Why raw TCP bytes cannot be sent directly to libipsec

ESP tunnel-mode processing operates on inner IP packets. A TCP byte stream does not contain IP addresses, TCP sequence numbers, ACK state, retransmission behavior, checksums, congestion control, or packetization. That is why the remote side of the proxy uses lwIP: it converts application bytes to/from complete inner IPv4/TCP packets while strongSwan remains responsible for IPsec.

## Current limitations

- IPv4 only.
- One remote mapping per forwarder process.
- Concurrent localhost sessions are supported within one forwarder process; each session owns an independent lwIP TCP socket/PCB and ephemeral source port. The process-level `--max-sessions` guard defaults to 32.
- Unix datagram packet endpoint tracks the last sender; use one packet consumer.
- No UDP application forwarding.
- No RDP UDP transport; TCP RDP is supported as an ordinary TCP stream.
- No production service manager/hardening profile is supplied yet.
