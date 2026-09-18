# Secrets management and MFA

The toolkit uses the official Bitwarden CLI against Bitwarden or a self-hosted Vaultwarden server. Secret values stay on the host side and are typed directly into the Solari desktop.

## Why this matters for VDI agents

Enterprise desktop workflows frequently require credentials and a rotating second factor. Asking a model to read or reproduce those values expands the secret exposure boundary unnecessarily. This toolkit instead gives the model safe capabilities:

- search for a credential by service/system name;
- know whether username/password/TOTP fields exist;
- request secure entry into the desktop;
- never receive the actual value.

## Start Vaultwarden

```bash
cd vaultwarden
docker compose up -d
```

Open `http://127.0.0.1:8222`, create an account, and add Login items such as:

- `Windows Server Admin`
- `Citrix Portal`
- `Accounting Application`

A Login item can contain username, password and TOTP authenticator seed.

After enrollment, disable public signup for any persistent deployment and add TLS/normal production hardening if the service is exposed beyond localhost.

## Install Bitwarden CLI

```bash
npm install -g @bitwarden/cli
bw --version
```

The toolkit uses an isolated CLI application directory so it does not disturb a human operator's normal Bitwarden session.

## Configure unattended access

Use a personal API key:

```bash
BW_CLIENTID=user.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
BW_CLIENTSECRET=replace-me
```

Put the vault master password in a local mode-0600 file:

```bash
printf '%s\n' 'YOUR_MASTER_PASSWORD' > secrets/vault-master-password.txt
chmod 600 secrets/vault-master-password.txt
```

Then configure:

```bash
VAULT_SERVER_URL=http://127.0.0.1:8222
VAULT_MASTER_PASSWORD_FILE=./secrets/vault-master-password.txt
```

The generated `BW_SESSION` is held in process memory and the CLI vault is locked on exit by default.

## Standalone Secrets + MFA MCP

Run:

```bash
./scripts/run-security-mcp.sh
```

Tools:

### `secrets_status`

Returns sanitized readiness information only.

### `secrets_search`

Input:

```json
{"query":"Windows Server"}
```

Example safe result shape:

```json
{
  "name": "Windows Server Admin",
  "has_username": true,
  "has_password": true,
  "has_totp": true
}
```

### `secrets_type_username`

Types the selected item's username into the currently focused desktop field.

### `secrets_type_password`

Types the selected item's password into the currently focused desktop field.

### `mfa_type_totp`

Generates the current TOTP and types it directly. `submit=true` is recommended so generation, typing and Enter happen atomically.

## Integrated agent security tools

The full desktop agent adds a stronger coordinate-aware variant:

```text
security_vault_status
security_vault_search
security_vault_type_username
security_vault_type_password
security_vault_type_totp
```

For secure typing, the model identifies a visible field using the current framebuffer and native coordinates. The host validates/focuses the field, retrieves the secret, types it through Solari, and returns only sanitized metadata.

After secret entry, the model-visible framebuffer can redact a full-width band around the credential row for a configurable interval. This is particularly useful for OTP fields that render the value visibly.

## What is never returned to the model

- password values;
- TOTP seeds;
- generated TOTP codes;
- vault master password;
- Bitwarden session decryption key;
- VPN PSKs/private keys.
