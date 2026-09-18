# Architecture

## Design objective

The toolkit lets a Solari-hosted Linux desktop act as a secure enterprise VDI workstation for an AI agent. The agent sees the desktop through Solari's live RFB framebuffer and uses Solari Desktop input APIs, while sensitive infrastructure operations remain host-owned.

## Control planes

### Model-visible computer use

The model receives the newest framebuffer and can request bounded desktop actions such as native-coordinate click/double-click, keyboard text, keyboard chords, refresh, goal-state updates and secure credential entry.

### Host-owned infrastructure

The model does not own Solari VM lifecycle, raw credentials or VPN cryptographic material. The host layer manages:

- Solari desktop creation/attachment;
- VPN status/connect/disconnect;
- Remmina profile provisioning;
- Bitwarden/Vaultwarden retrieval;
- secure username/password/TOTP typing;
- runtime lifecycle IDs and snapshot state.

## Network data path

Both VPN providers expose the protected RDP target as a local TCP socket, typically `127.0.0.1:3390`.

### userswan

```text
Remmina -> localhost TCP -> userswan forwarder -> lwIP TCP
        -> patched strongSwan kernel-libipsec no-TUN packet socket
        -> ESP/NAT-T -> enterprise VPN gateway -> RDP server
```

### userguard

```text
Remmina -> localhost TCP -> userguard gVisor TCP/IP
        -> wireguard-go -> encrypted WireGuard UDP
        -> enterprise peer -> RDP server
```

No protected-prefix host route is required for either forwarding model.

## Desktop path

```text
AI model
  -> Solari VDI agent
  -> latest RFB framebuffer
  -> native Solari mouse/keyboard events
  -> Linux/XFCE + Remmina
  -> Windows RDP session
  -> legacy enterprise application
```

## Visual epoch model

Only the newest framebuffer in a provider request is authoritative for current GUI state. Older framebuffer pixels, geometry text and assistant screen-state narration are superseded. Explicitly remembered business facts remain in durable goal memory.

This separates:

```text
What is visible now?            newest framebuffer
What was verified earlier?      durable goal memory
```

## Security path

Secrets are intentionally asymmetric:

```text
model -> safe item name/id -> host vault backend -> secret in host memory
                                              -> Solari keyboard -> focused field
model <- sanitized success/failure only
```

The same pattern is used for TOTP MFA.
