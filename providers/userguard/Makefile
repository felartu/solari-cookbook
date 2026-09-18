PREFIX ?= /usr/local
GO ?= go

.PHONY: all build install clean check selftest

all: build

build:
	mkdir -p build
	CGO_ENABLED=0 $(GO) build -trimpath -buildvcs=false -o build/userguard ./cmd/userguard

install: build
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 0755 build/userguard $(DESTDIR)$(PREFIX)/bin/userguard

check:
	./tests/check-repo.sh

selftest:
	./tests/selftest.sh

clean:
	rm -rf build dist
