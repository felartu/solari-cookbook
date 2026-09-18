# Demo guide

## Demo story

A back-office employee has submitted an invoice for reimbursement. The AI agent must access the enterprise network, connect to the Windows environment, read the business instructions and invoice, enter the claim in a legacy accounting application, complete the approval/payment workflow, and verify the accounting result.

## Suggested recording sequence

1. Show the Solari desktop and explain that it is isolated from the enterprise LAN.
2. Show `VPN_PROVIDER` and briefly explain `userswan` / `userguard`.
3. Run the toolkit.
4. Ask the agent to process the reimbursement end-to-end.
5. Show the agent opening Remmina and connecting through the localhost VPN relay.
6. Show the agent reading instructions and the invoice visually.
7. Point out persistent goal memory when it leaves the invoice screen.
8. Show the legacy accounting GUI being completed.
9. If authentication appears, show the Bitwarden/TOTP tools typing secrets without exposing values in chat.
10. Finish on the accounting ledger verification.
11. Show `vdi_vpn_status` and the provider name as technical proof of the enterprise network path.

## Talking points

- No kernel TUN requirement for the included VPN providers.
- The remote desktop is a real Windows GUI, not a purpose-built web demo.
- Secrets and TOTP remain host-side.
- Goal memory lets the agent carry verified business facts across multiple screens.
- Current GUI decisions are always based on the newest framebuffer.
- The design generalizes to many enterprise back-office workflows.
