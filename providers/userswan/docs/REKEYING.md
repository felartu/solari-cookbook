# CHILD_SA rekeying diagnostics

## userswan defaults

userswan explicitly configures the CHILD lifetime values instead of relying on a derived jitter:

```text
rekey_time = 1h
life_time  = 70m
rand_time  = 5m
```

This is intentional. strongSwan subtracts a random value up to `rand_time` from `rekey_time` to calculate the effective soft lifetime. Therefore these defaults schedule a local CHILD_SA rekey roughly 55–60 minutes after installation, with a 70-minute hard lifetime.

Do not combine a short `rekey_time` with a much larger `life_time` while leaving `rand_time` implicit. For example, `rekey_time=1h` plus `life_time=8h` makes the derived randomization window 7 hours. strongSwan caps that randomization at the rekey value, which can make the effective local soft lifetime fall anywhere from approximately 0 to 1 hour.

The values can be overridden at runtime:

```bash
export CHILD_REKEY_TIME=1h
export CHILD_LIFE_TIME=70m
export CHILD_RAND_TIME=5m
```

For diagnostics only, local initiation can be disabled with `CHILD_REKEY_TIME=0` (choose a hard lifetime policy appropriate for your deployment).

## Determine which side initiates

In a charon log:

```text
parsed CREATE_CHILD_SA request ... N(REKEY_SA)
```

means the **peer initiated** a CHILD_SA rekey.

```text
generating CREATE_CHILD_SA request ... N(REKEY_SA)
```

means the **local userswan/charon instance initiated** it.

A normal responder-side sequence looks like:

```text
parsed CREATE_CHILD_SA request ... N(REKEY_SA)
inbound CHILD_SA ... established
generating CREATE_CHILD_SA response ...
received DELETE for ESP CHILD_SA ...
outbound CHILD_SA ... established
rekeyed CHILD_SA ...
```

That sequence is normal IKEv2 make-before-break behavior. The abnormal condition is a peer immediately starting another independent rekey after the previous exchange completed.

Sequential IKE message IDs and new ESP SPIs distinguish independent rekeys from retransmission of one lost exchange.

## What the no-TUN patch changes

The userswan strongSwan patch only changes the `kernel-libipsec` plaintext packet transport and route handling. It does not patch IKEv2 `CREATE_CHILD_SA`, `child_rekey`, task-manager, or DELETE exchange logic.

Therefore, if the local log only shows `parsed CREATE_CHILD_SA request ... N(REKEY_SA)` and never `generating ... request ... N(REKEY_SA)`, the immediate rekey initiation is external to the userswan data-plane patch.

## Gateway-side checks

If the peer is strongSwan, inspect its effective CHILD configuration for:

```text
rekey_time
life_time
rand_time
rekey_bytes
life_bytes
rand_bytes
rekey_packets
life_packets
rand_packets
```

Also inspect `strongswan.conf` for load/stress-test or custom automation such as:

```text
charon.plugins.load-tester.child_rekey
```

and check whether any service invokes:

```bash
swanctl --rekey --child ...
```

Useful gateway commands include:

```bash
swanctl --list-conns --raw
swanctl --list-sas --raw
journalctl -u strongswan -f
```

In the gateway log, look immediately before each outbound CREATE_CHILD_SA for messages such as:

```text
creating rekey job for CHILD_SA ...
rekeying CHILD_SA ...
```

The event immediately preceding that line usually identifies whether the trigger is a time/byte/packet soft expiry, an explicit VICI request, a kernel expire event, or application/test logic.

## Childless IKE

userswan exposes `CHILDLESS=allow|prefer|force|never` and defaults to `allow`.

`prefer` can be useful when the first CHILD_SA must have a separate PFS key exchange, because the first CHILD created inside IKE_AUTH cannot use an independent CHILD key exchange. However, childless initiation is an interoperability choice, not a general fix for peer-side rekey storms; test it against the specific gateway before enabling it.
