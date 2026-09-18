# Enhanced computer use

The toolkit wraps Solari Desktop control with orchestration designed for long enterprise GUI tasks.

## Goal management

For a multi-step outcome, the agent declares a persistent goal using:

```text
start_goal
```

The goal includes an objective and an observable success condition. The model cannot simply stop after an intermediate click; final completion is gated through:

```text
finish_goal
```

with `SUCCESS`, `NOT_FOUND` or host-authorized `BLOCKED`.

## Durable task memory

Screenshots are intentionally compacted to keep requests efficient. Before leaving an information-rich screen, the agent can call:

```text
goal_remember
```

Examples:

```text
reimbursement_instructions
invoice_fields
claim_saved
payment_verified
```

This separates business memory from screenshot history.

## Authoritative visual epochs

Only the newest host framebuffer is authoritative for current GUI state. Older framebuffer pixels, geometry and assistant visual narration are superseded before the next provider request.

This avoids a common multimodal failure mode where old text such as “Paint is maximized” competes with a newer image showing a different window.

## Native visual control

The vision-capable model selects native framebuffer pixels. Before sending input, the host validates:

- frame version;
- coordinate bounds;
- Solari display geometry;
- stale-point neighborhood consistency;
- optional cursor echo.

No secondary host vision model is required for the active click path.

## Nested VDI scopes

A Solari desktop containing Remmina has two GUI layers:

```text
local       Linux/XFCE/Remmina chrome
remote_rdp  Windows desktop/application rendered inside Remmina
```

Keyboard and pointer tools carry explicit scope metadata. Remote keyboard entry is permitted only when the active local X11 top-level window is the RDP session window.

## Loop protection

The host records semantic actions independently of framebuffer version. It can reject:

- repeated clicks on the same semantic target;
- alternating action cycles;
- repeated unavailable hotkeys;
- repeated unlabeled taskbar probes;
- repeated identical text entry with no visible effect.

This keeps a long-running back-office goal from consuming arbitrary tool/model rounds without progress.
