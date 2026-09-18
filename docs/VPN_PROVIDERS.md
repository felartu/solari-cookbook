# Enterprise VPN providers

The toolkit exposes two named VPN providers behind the same MCP tools and the same localhost RDP endpoint.

Set:

```bash
VPN_PROVIDER=userswan
```

or:

```bash
VPN_PROVIDER=userguard
```

Then use:

```text
vdi_vpn_status
vdi_vpn_connect
vdi_vpn_disconnect
```

The provider name is included in sanitized status output.

## userswan: IKEv2/IPsec

`userswan` uses strongSwan for IKEv2, authentication, CHILD_SA negotiation, ESP, replay protection and NAT-T. A no-TUN kernel-libipsec patch exchanges inner packets with a userspace forwarder over an AF_UNIX datagram socket. lwIP provides the remote TCP leg.

Source: `providers/userswan/`

### Configure

Minimum toolkit variables:

```bash
VPN_PROVIDER=userswan
VDI_IPSEC_GATEWAY=198.51.100.10
VDI_IPSEC_LOCAL_ID=solari-vdi-client
VDI_IPSEC_REMOTE_ID=198.51.100.10
VDI_IPSEC_REMOTE_TS=10.20.30.40/32
VDI_IPSEC_PSK_FILE=./secrets/userswan.psk
VDI_RDP_HOST=10.20.30.40
VDI_RDP_PORT=3389
VDI_RDP_LOCAL_PORT=3390
```

The launcher can build a reusable Solari snapshot from the bundled userswan source when no valid userswan snapshot is configured.

### Provider-local build/test

```bash
cd providers/userswan
sudo ./scripts/install-deps-debian.sh
sudo PREFIX=/opt/userswan/strongswan ./scripts/build-strongswan.sh
sudo PREFIX=/opt/userswan ./scripts/build-forwarder.sh
```

Check runtime details with:

```bash
sudo -E ./scripts/status-userswan.sh
```

For an RDP protocol probe:

```bash
python3 examples/rdp-local-probe.py --host 127.0.0.1 --port 3390 --timeout 20
```

## userguard: WireGuard

`userguard` uses upstream `wireguard-go` plus its gVisor userspace netstack. It does not create a kernel WireGuard interface or `/dev/net/tun`.

Source: `providers/userguard/`

### Configure

```bash
VPN_PROVIDER=userguard
USERGUARD_ADDRESS=10.77.0.2/32
USERGUARD_PRIVATE_KEY_FILE=./secrets/userguard.key
USERGUARD_PEER_PUBLIC_KEY=BASE64_PUBLIC_KEY
USERGUARD_ENDPOINT=198.51.100.10:51820
USERGUARD_ALLOWED_IPS=10.20.30.0/24
VDI_RDP_HOST=10.20.30.40
VDI_RDP_PORT=3389
VDI_RDP_LOCAL_PORT=3390
```

The toolkit uploads the private key to a mode-0600 guest runtime file and starts the userguard binary with a forward equivalent to:

```text
127.0.0.1:3390 = 10.20.30.40:3389
```

The Solari desktop image must contain `/opt/userguard/bin/userguard`, or `USERGUARD_BINARY` must point to its installed path.

### Build and self-test

```bash
cd providers/userguard
sudo ./scripts/install-deps-debian.sh
sudo PREFIX=/opt/userguard ./scripts/build.sh
make selftest
```

Expected self-test result:

```text
USERGUARD_SELFTEST_OK payload_roundtrip=1
```

### Check provider runtime

Inside a prepared Linux environment:

```bash
RUNDIR=/run/userguard ./scripts/status-userguard.sh
```

From the toolkit repository, once a Solari desktop is attached:

```bash
./scripts/check-vpn.sh
```

## Common RDP abstraction

The point of the provider interface is that Remmina never needs the enterprise target route directly. It connects to:

```text
127.0.0.1:3390
```

The selected provider owns the encrypted path from that local listener to the protected RDP server.
