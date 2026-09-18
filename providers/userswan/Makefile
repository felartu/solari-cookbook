PREFIX ?= /usr/local
CC ?= cc
CFLAGS ?= -O2 -g -Wall -Wextra -Werror
LWIP_CFLAGS := $(shell pkg-config --cflags lwip)
LWIP_LIBS := $(shell pkg-config --libs lwip)

.PHONY: all clean install check
all: build/userswan-forwarder

build/userswan-forwarder: src/userswan-forwarder.c
	mkdir -p build
	$(CC) $(CFLAGS) -pthread $(LWIP_CFLAGS) $< -o $@ $(LWIP_LIBS)

install: build/userswan-forwarder
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 0755 build/userswan-forwarder $(DESTDIR)$(PREFIX)/bin/userswan-forwarder

check:
	sh -n scripts/build-strongswan.sh
	sh -n scripts/build-forwarder.sh
	sh -n scripts/install-deps-debian.sh
	sh -n scripts/run-userswan.sh
	sh -n scripts/status-userswan.sh
	sh -n scripts/stop-userswan.sh
	python3 -m py_compile scripts/render-swanctl.py examples/rdp-local-probe.py tests/concurrent-rdp-probe.py
	$(CC) $(CFLAGS) -pthread $(LWIP_CFLAGS) src/userswan-forwarder.c -o /tmp/userswan-forwarder-check $(LWIP_LIBS)
	/tmp/userswan-forwarder-check --help
	rm -f /tmp/userswan-forwarder-check

clean:
	rm -rf build
