# Building

## Tested source versions

- strongSwan: `6.0.7`
- strongSwan 6.0.7 tarball SHA-256: `e518e34e159514f4c6ba80d1f926cb151e0dd4e3a1d94213171234b8b9ae6f55`
- lwIP development ABI used by the reference build: Debian `liblwip-dev` 2.2.x
- The tested Debian lwIP build has `NO_SYS=0`, socket/netconn support, `LWIP_TCPIP_CORE_LOCKING=1`, and `MEMP_MEM_MALLOC=1`, allowing concurrent socket-backed TCP sessions to share one threaded lwIP core.
- C language/toolchain: GCC/Clang-compatible C11 atomics and POSIX threads

The build script verifies the strongSwan tarball before patching it.

## Debian/Ubuntu dependency install

```bash
sudo ./scripts/install-deps-debian.sh
```

Equivalent package set:

```text
build-essential
pkg-config
libssl-dev
libgmp-dev
libcap-dev
liblwip-dev
ca-certificates
curl
bzip2
patch
python3
tcpdump
netcat-openbsd
```

## Build strongSwan

Automated:

```bash
sudo PREFIX=/opt/userswan/strongswan ./scripts/build-strongswan.sh
```

Manual equivalent:

```bash
VERSION=6.0.7
curl -fLso strongswan.tar.bz2 "https://download.strongswan.org/strongswan-${VERSION}.tar.bz2"
printf '%s  %s\n' \
  e518e34e159514f4c6ba80d1f926cb151e0dd4e3a1d94213171234b8b9ae6f55 \
  strongswan.tar.bz2 | sha256sum -c -
mkdir strongswan-src
tar -xjf strongswan.tar.bz2 -C strongswan-src --strip-components=1
cd strongswan-src
patch -p2 < ../patches/strongswan-6.0.7/0001-kernel-libipsec-no-tun-packet-socket.patch
./configure \
  --prefix=/opt/userswan/strongswan \
  --with-piddir=/run/userswan \
  --enable-kernel-libipsec \
  --enable-libipsec
make -j"$(nproc)"
make check
sudo make install
```

The critical configure options are `--enable-kernel-libipsec` and `--enable-libipsec`. The normal kernel-netlink plugin is retained for host network/address discovery, but no protected-route/XFRM data path is installed by the runtime configuration.

For custom lwIP builds, ensure the socket/netconn API is enabled and sized for the desired concurrency. If `MEMP_MEM_MALLOC=0`, configure `MEMP_NUM_NETCONN` and `MEMP_NUM_TCP_PCB` at least as high as the intended `MAX_SESSIONS` (plus any other lwIP users); otherwise `lwip_socket()` may fail before the userswan process-level session limit is reached.

## Build the userswan forwarder

Automated:

```bash
sudo PREFIX=/opt/userswan ./scripts/build-forwarder.sh
```

Manual equivalent:

```bash
cc -O2 -g -Wall -Wextra -Werror -pthread \
  $(pkg-config --cflags lwip) \
  src/userswan-forwarder.c \
  -o userswan-forwarder \
  $(pkg-config --libs lwip)
```

Or use the Makefile:

```bash
make
sudo make install PREFIX=/usr/local
```

## Verify repository/build syntax

```bash
./tests/check-repo.sh
```

This checks shell syntax, Python syntax, compiles the forwarder with warnings-as-errors and checks for common secret/runtime filenames.

## Build output

With the default scripts:

```text
/opt/userswan/strongswan/libexec/ipsec/charon
/opt/userswan/strongswan/sbin/swanctl
/opt/userswan/strongswan/bin/pki
/opt/userswan/bin/userswan-forwarder
```

Additional strongSwan libraries/plugins and helper executables are installed beneath the strongSwan prefix. Enumerate the exact installed executables on your build with:

```bash
find /opt/userswan/strongswan -type f -perm -111 -print | sort
```
