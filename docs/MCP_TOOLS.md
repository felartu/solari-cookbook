# MCP tool surfaces

The repository can run the VPN and security capabilities as standalone MCP servers in addition to using them inside the integrated VDI agent.

## Enterprise VPN MCP

Start:

```bash
./scripts/run-vpn-mcp.sh
```

Tools:

| Tool | Purpose |
| --- | --- |
| `vdi_vpn_status` | Sanitized status of the selected `userswan` or `userguard` provider |
| `vdi_vpn_connect` | Establish the provider and localhost relay |
| `vdi_vpn_disconnect` | Disconnect the provider/relay |
| `vdi_remmina_prepare_profile` | Write the saved Remmina profile without launching it |
| `vdi_remmina_connect` | Optional host-side Remmina launch helper |
| `vdi_desktop_state` | Sanitized process/window/provider state |

The provider is selected by `VPN_PROVIDER=userswan|userguard`.

## Secrets + MFA MCP

Start:

```bash
./scripts/run-security-mcp.sh
```

Tools:

| Tool | Purpose |
| --- | --- |
| `secrets_status` | Check Bitwarden/Vaultwarden readiness without returning secrets |
| `secrets_search` | Search safe item metadata |
| `secrets_type_username` | Type the selected username into the currently focused field |
| `secrets_type_password` | Type the selected password into the currently focused field |
| `mfa_type_totp` | Generate and type the current TOTP; defaults to submitting with Enter |

For standalone MCP use, focus the destination GUI field before calling a `*_type_*` tool. The integrated VDI agent provides coordinate-validated secure field focus and screenshot redaction as an additional safety layer.

## Example MCP client configuration

See `examples/mcp-servers.json.example`. Replace the placeholder repository path with the absolute path to your checkout. The exact MCP client configuration syntax may vary by host application.
