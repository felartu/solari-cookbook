# Troubleshooting

## IKE establishes but the localhost connection hangs

Check:

```bash
./scripts/status-userswan.sh
```

Look for `REMOTE_CONNECT_START` followed by `TX_INNER`. If there is no `RX_INNER`, capture the gateway's UDP/4500 traffic on the physical interface:

```bash
GW="$VPN_GATEWAY"
IFACE=$(ip route get "$GW" | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
sudo tcpdump -ni "$IFACE" -s0 -vvv "host $GW and udp and port 4500"
```

Outbound data should decode as `UDP-encap: ESP(...)`. If inbound ESP is present but no `RX_INNER` appears, inspect charon/libipsec logs. If no inbound ESP is present, investigate the VPN peer, remote routing/policy and target service.

## `bad udp cksum` on outbound tcpdump

On many virtual/NIC paths, tcpdump observes transmit packets before hardware/software checksum offload completes. An outbound `bad udp cksum` line alone does not prove a malformed packet. Correlate with successful IKE traffic, receive-side checksums and actual peer behavior.

## TUN exists on the host

That is not a failure. userswan's criterion is that the runtime does not open/use TUN for this IPsec data path. `kernel-libipsec.no_tun = yes` directs plaintext packets through the Unix socket instead.

## CHILD_SAs rekey/reappear frequently

Peer-initiated CHILD_SA replacement is legal and may be driven by remote policy. Confirm the newest CHILD_SA is `INSTALLED` and that data counters/SPIs move. Do not hard-code ESP SPIs in the forwarder; libipsec owns SA lifecycle.
