# Local Vaultwarden for the demo

This optional container provides a self-hosted Bitwarden-compatible store for the toolkit's secrets and MFA tools.

Start it:

```bash
docker compose up -d
```

Open `http://127.0.0.1:8222`, create a demo account and add Login items. Each item can contain a username, password and TOTP seed.

Install the official Bitwarden CLI on the host:

```bash
npm install -g @bitwarden/cli
```

Configure the toolkit with:

```bash
VAULT_SERVER_URL=http://127.0.0.1:8222
BW_CLIENTID=...
BW_CLIENTSECRET=...
VAULT_MASTER_PASSWORD_FILE=./secrets/vault-master-password.txt
```

After account enrollment, disable signups for any persistent instance. Do not expose this example deployment publicly without TLS, authentication hardening, backups and normal production controls.

See `../docs/SECRETS_AND_MFA.md`.
