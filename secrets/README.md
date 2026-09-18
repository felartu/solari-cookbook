# Local secrets directory

Keep deployment secrets outside Git. Typical files include:

- `vault-master-password.txt`
- `userswan.psk`
- `userguard.key`
- optional `userguard.psk`

Use mode `0600` and never commit these files.
