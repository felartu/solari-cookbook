# Solari Enterprise VDI Toolkit

A reference implementation that extends Solari computer use into enterprise VDI environments: userspace VPN access, RDP-based legacy desktop automation, secrets and MFA, durable goal execution, and verified GUI control.

The project demonstrates a real-world Solari use case where an AI agent must do more than interact with a public website. It must enter a private enterprise network, authenticate safely, open a Windows desktop, operate a legacy back-office application, complete a multi-step business process, and verify the result.

## What this project delivers

### Enterprise VDI access for Solari

The toolkit gives a Solari desktop a controlled path into enterprise Windows environments over RDP. The agent works through a real Remmina GUI session instead of bypassing the desktop application layer.

### Legacy desktop application use

Many finance, operations, healthcare, telecom and government workflows still depend on Windows applications that expose no modern API. The toolkit lets a vision-capable agent use those applications as a human operator would: inspect screens, click controls, type values, navigate dialogs and verify outcomes.

### Back-office automation

The demonstration workflow processes an employee reimbursement claim in a legacy accounting application. The same pattern maps to invoice entry, reconciliation, claims processing, order administration, ERP/CRM work, provisioning, desktop support and other repetitive back-office tasks.

### Enterprise VPN compatibility

Two userspace VPN providers are included:

| Provider | Protocol | Purpose |
| --- | --- | --- |
| **userswan** | IKEv2/IPsec | strongSwan-based no-TUN userspace IPsec with a localhost TCP relay |
| **userguard** | WireGuard | wireguard-go + userspace TCP/IP with localhost TCP forwarding |

Both providers are designed for constrained agent desktops where creating kernel TUN interfaces or changing host routing may be undesirable. Remmina connects to the same loopback RDP endpoint regardless of the selected provider.

_Both providers were designed specifically for the Solari MicroVM kernel that lacks support for VPNs out of the box._

### Secrets management and MFA

The toolkit integrates Vaultwarden/Bitwarden so the model can search safe credential metadata and request host-side entry of usernames, passwords and rotating TOTP codes without receiving the secret values in conversation context. This is specially useful to avoid leaking credentials into the AI inference provider.

This provides two agent-facing security capabilities:

- **Secrets MCP**: safe item discovery and host-side username/password typing.
- **2FA/MFA MCP**: host-side TOTP generation and direct submission to the focused desktop field.

### Enhanced computer use

The desktop agent adds orchestration controls around normal visual computer use:

- persistent multi-step **goal management**;
- durable task facts/checkpoints that survive screenshot compaction;
- current-frame-only **visual epochs** to prevent stale-screen reasoning;
- native framebuffer coordinate clicking;
- explicit local Linux vs remote RDP input scopes;
- semantic action-loop detection;
- bounded retries for failed text entry;
- secure field screenshot redaction;
- host-owned Solari lifecycle, VPN and credential boundaries.

## Demo video

<img width="1308" height="688" alt="image" src="https://github.com/user-attachments/assets/9fb69b0e-0308-4217-88e0-9330acbb9d96" />


**YouTube demo:** _https://www.youtube.com/watch?v=PJJrHUFTkdA_

## Real-world architecture

```text
                        AI / vision model
                               |
                    OpenAI-compatible API
                               |
                               v
                  +-------------------------+
                  | Solari VDI agent        |
                  | goal + GUI controller   |
                  +-------------------------+
                      |               |
          live RFB    |               | host-owned tools
          framebuffer |               |
                      v               v
              +---------------+   +------------------+
              | Solari Linux  |   | Bitwarden /      |
              | desktop       |   | Vaultwarden      |
              | + Remmina     |   | secrets + TOTP   |
              +-------+-------+   +------------------+
                      |
              127.0.0.1:3390
                      |
          +-----------+-----------+
          |                       |
          v                       v
      userswan                 userguard
     IKEv2/IPsec               WireGuard
     userspace relay           userspace relay
          |                       |
          +-----------+-----------+
                      |
                enterprise VPN
                      |
                      v
                 Windows / RDP
                      |
                      v
             legacy application
                      |
                      v
                back-office work
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the detailed control and trust boundaries.

## Repository layout

```text
src/
  solari_vdi_toolkit.py   main launcher / Solari desktop preparation
  agent_engine.py         visual computer-use + goal orchestration
  vpn_mcp.py              provider-selectable enterprise VPN MCP
  security_mcp.py         Bitwarden secrets + TOTP/MFA MCP
  security_vault.py       host-only Bitwarden/Vaultwarden backend
  solari_desktop.py       Solari desktop compatibility wrapper
  demo_ui.py              operator-facing terminal UI

providers/
  userswan/               userspace IKEv2/IPsec provider source
  userguard/              userspace WireGuard provider source

config/
  toolkit.conf.example
  providers/

vaultwarden/
  docker-compose.yml

scripts/
  run-toolkit.sh
  run-vpn-mcp.sh
  run-security-mcp.sh
  check-vpn.sh

docs/
  ARCHITECTURE.md
  VPN_PROVIDERS.md
  SECRETS_AND_MFA.md
  MCP_TOOLS.md
  COMPUTER_USE.md
  REAL_WORLD_USE_CASE.md
  DEMO_GUIDE.md
```

## Quick start

### 1. Install host dependencies

```bash
python3 -m pip install --user -r requirements.txt
```

Install the official Bitwarden CLI if you want secrets/MFA:

```bash
npm install -g @bitwarden/cli
```

### 2. Create configuration

```bash
cp .env.example .env
cp config/toolkit.conf.example config/toolkit.conf
chmod 600 .env
```

Edit `.env` with your Solari API key, AI endpoint/model, protected RDP target, VPN provider and credential backend.

### 3. Choose a VPN provider

For IKEv2/IPsec:

```bash
VPN_PROVIDER=userswan
```

For WireGuard:

```bash
VPN_PROVIDER=userguard
```

Provider-specific configuration and status checks are documented in [docs/VPN_PROVIDERS.md](docs/VPN_PROVIDERS.md).

### 4. Optional: start Vaultwarden

```bash
cd vaultwarden
docker compose up -d
```

Then configure the Bitwarden CLI and create Login items for the enterprise systems the agent may use. See [docs/SECRETS_AND_MFA.md](docs/SECRETS_AND_MFA.md) and [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md).

### 5. Start the toolkit

```bash
./scripts/run-toolkit.sh
```

The normal flow creates or attaches a Solari desktop, prepares the enterprise VPN/RDP path, attaches the live framebuffer, and exposes the controlled computer-use tools to the selected vision-capable model.

## VPN MCP

Run the VPN tool server independently:

```bash
./scripts/run-vpn-mcp.sh
```

Core tools:

```text
vdi_vpn_status
vdi_vpn_connect
vdi_vpn_disconnect
vdi_remmina_prepare_profile
vdi_desktop_state
```

The selected provider is controlled by `VPN_PROVIDER=userswan|userguard`.

To check provider state from the repository:

```bash
./scripts/check-vpn.sh
```

## Secrets and MFA MCP

Run the standalone security MCP:

```bash
./scripts/run-security-mcp.sh
```

It exposes:

```text
secrets_status
secrets_search
secrets_type_username
secrets_type_password
mfa_type_totp
```

Secret values are retrieved host-side and typed directly through the Solari Desktop keyboard channel. They are not returned in MCP tool results.

The integrated agent additionally provides coordinate-validated secure typing and screenshot redaction around fields that received a secret. Check the vault backend safely with `./scripts/check-vault.sh`.

## Example business workflow

A representative task is:

1. Establish the enterprise VPN.
2. Open Remmina in the Solari desktop.
3. Connect to a Windows server through RDP.
4. Read process instructions from the Windows desktop.
5. Open a scanned invoice from a shared folder.
6. Persist the relevant invoice facts as durable goal memory.
7. Open a legacy accounting application.
8. Create a reimbursement claim for an employee.
9. Populate vendor, invoice number, date, description, amount and account.
10. Use the secrets/MFA tools if authentication is required.
11. Save, submit, approve and pay the claim.
12. Verify the resulting accounting posting.

This is the kind of workflow where Solari becomes an enterprise automation platform rather than only a browser automation runtime.

## Security model

- Solari desktop/session lifecycle is host-owned.
- VPN PSKs/private keys and RDP passwords are not returned to the model.
- Bitwarden master password and `BW_SESSION` remain host-side.
- TOTP seeds and generated codes are never returned as model-visible tool values.
- The model receives safe vault item names/IDs and capability flags only.
- Secure-entry screen regions can be redacted after secret typing.
- Current GUI decisions use the newest framebuffer; superseded screen narration is removed from model requests.
- Both VPN providers intentionally keep protected TCP forwarding in userspace.

See the provider security documents and [docs/SECRETS_AND_MFA.md](docs/SECRETS_AND_MFA.md) before production adaptation.

## Project scope

This repository is a reference implementation and demonstration of a real enterprise Solari use case. `userswan` and `userguard` are intentionally inspectable userspace networking implementations; organizations should apply their normal production security review, image-hardening, monitoring, credential governance and operational controls before deployment.
