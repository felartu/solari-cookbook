# Real-world enterprise use case

## Problem

A large amount of enterprise work is still performed through private Windows desktops and applications rather than public web APIs. Typical constraints include:

- private RFC1918 networks;
- IKEv2/IPsec or WireGuard VPN access;
- RDP/Citrix-style VDI entry points;
- legacy Windows applications;
- usernames/passwords stored in enterprise vaults;
- TOTP/2FA requirements;
- scanned invoices or documents that must be read visually;
- long procedures with approval, payment and verification steps.

A generic browser agent does not solve this environment.

## Solari fit

Solari provides an isolated, controllable desktop with live framebuffer and input APIs. This toolkit layers the enterprise infrastructure needed to make that desktop useful for operational work:

```text
Solari desktop
+ enterprise VPN
+ VDI/RDP client
+ secrets/MFA
+ multimodal GUI control
+ goal orchestration
= enterprise back-office agent runtime
```

## Demonstrated workflow

The reference demo uses a Windows accounting application to process an employee reimbursement:

- enter the enterprise network;
- connect to Windows through RDP;
- read reimbursement instructions;
- inspect a photographed invoice;
- transfer invoice data into a legacy claims form;
- save and submit the claim;
- approve and pay it;
- verify the accounting ledger entry.

Nothing about the architecture is accounting-specific. The same runtime can support:

- ERP data entry;
- claims administration;
- customer account maintenance;
- provisioning portals;
- legacy CRM tasks;
- desktop-only finance tools;
- internal support consoles;
- operations workflows that span documents, VPN and GUI applications.

## Why this is valuable

The value is not “AI can click an RDP window.” The value is a complete infrastructure pattern for safely connecting a Solari agent to the software enterprises already use without requiring those organizations to replace every legacy application with a new API first.
