# Notice

This repository packages a Solari enterprise VDI reference implementation together with two inspectable userspace VPN provider projects:

- `providers/userswan` — strongSwan/lwIP-based userspace IKEv2/IPsec forwarding.
- `providers/userguard` — wireguard-go/gVisor-based userspace WireGuard forwarding.

Each provider documents its upstream dependencies and third-party components under its own `docs/THIRD_PARTY.md` and related build/security documentation. `providers/userguard` also carries its provider-level license file.

Before redistribution or production deployment, review the licensing, security, cryptographic, operational and support requirements of Solari, Remmina/FreeRDP, strongSwan, lwIP, wireguard-go, gVisor, Bitwarden/Vaultwarden and the other dependencies used by your deployment.
