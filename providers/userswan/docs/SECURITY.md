# Security notes

## Secrets

Never commit PSKs, `.env` files, generated `swanctl.conf`, packet captures containing sensitive traffic, or runtime logs containing deployment metadata.

The PSK file must be mode `0600` (or stricter). `scripts/render-swanctl.py` refuses group/world-readable PSK files. Keep the secret outside the repository, for example under `/run/secrets/userswan.psk`.

## Unix packet socket

The no-TUN packet socket carries plaintext inner IP packets before encryption and after decryption. Treat it as sensitive. The reference runtime directory is mode `0700`, and the forwarder's client Unix socket is mode `0600`.

Do not expose `/run/userswan/notun.sock` to untrusted users or containers.

## Local TCP listener

The default is `127.0.0.1`. Changing `LOCAL_IP` to `0.0.0.0` or another non-loopback address exposes the proxied service to other hosts according to normal host firewall/network reachability. Do that only intentionally.

## Privileges

The design avoids TUN and Linux XFRM installation for the protected data path. Actual privilege requirements still depend on how charon is built, which plugins are loaded, the chosen local IKE/NAT-T source ports, filesystem ownership and the host/container capability model.

The reference scripts use high local IKE/NAT-T ports (`15000` and `14500`) so binding privileged ports is unnecessary. Running under a dedicated service account with a private runtime directory is preferable to an unrestricted root service once deployment requirements are understood.

## Cryptography

Do not modify this project to implement custom IKE or ESP cryptography. The purpose of the design is specifically to reuse strongSwan's protocol/crypto machinery while replacing only the plaintext packet transport normally provided by TUN.
