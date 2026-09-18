package main

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/netip"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun/netstack"
)

type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error {
	if strings.TrimSpace(v) == "" {
		return errors.New("empty value")
	}
	*m = append(*m, v)
	return nil
}

type mapping struct {
	listen string
	target string
}

type relay struct {
	ctx      context.Context
	sem      chan struct{}
	wg       *sync.WaitGroup
	sessions atomic.Uint64
}

func secureFile(path string) ([]byte, error) {
	st, err := os.Stat(path)
	if err != nil {
		return nil, err
	}
	if !st.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a regular file", path)
	}
	if st.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("%s permissions are too broad; require mode 0600 or stricter", path)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return []byte(strings.TrimSpace(string(b))), nil
}

func wireGuardKeyHex(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if len(raw) == 64 {
		b, err := hex.DecodeString(raw)
		if err == nil && len(b) == 32 {
			return strings.ToLower(raw), nil
		}
	}
	b, err := base64.StdEncoding.DecodeString(raw)
	if err != nil {
		return "", errors.New("key must be a standard WireGuard base64 key or 64 hex characters")
	}
	if len(b) != 32 {
		return "", fmt.Errorf("WireGuard key decoded to %d bytes; expected 32", len(b))
	}
	return hex.EncodeToString(b), nil
}

func secretKeyFileHex(path string) (string, error) {
	b, err := secureFile(path)
	if err != nil {
		return "", err
	}
	return wireGuardKeyHex(string(b))
}

func parseMapping(spec string) (mapping, error) {
	left, right, ok := strings.Cut(spec, "=")
	if !ok || strings.TrimSpace(left) == "" || strings.TrimSpace(right) == "" {
		return mapping{}, fmt.Errorf("mapping must be LISTEN=TARGET, got %q", spec)
	}
	return mapping{listen: strings.TrimSpace(left), target: strings.TrimSpace(right)}, nil
}

func validateHostPort(s string) error {
	host, port, err := net.SplitHostPort(s)
	if err != nil {
		return err
	}
	if strings.TrimSpace(host) == "" || strings.TrimSpace(port) == "" {
		return fmt.Errorf("host and port are required in %q", s)
	}
	return nil
}

func (r *relay) runSession(local net.Conn, dial func(context.Context) (net.Conn, error), label string) {
	select {
	case r.sem <- struct{}{}:
	case <-r.ctx.Done():
		_ = local.Close()
		return
	}
	r.wg.Add(1)
	go func() {
		defer r.wg.Done()
		defer func() { <-r.sem }()
		defer local.Close()

		id := r.sessions.Add(1)
		remote, err := dial(r.ctx)
		if err != nil {
			log.Printf("SESSION_CONNECT_FAIL id=%d mapping=%s err=%v", id, label, err)
			return
		}
		defer remote.Close()
		log.Printf("SESSION_CONNECTED id=%d mapping=%s local=%s remote=%s", id, label, local.RemoteAddr(), remote.RemoteAddr())

		done := make(chan string, 2)
		copyOne := func(direction string, dst, src net.Conn) {
			n, err := io.Copy(dst, src)
			if err != nil && !errors.Is(err, net.ErrClosed) && r.ctx.Err() == nil {
				log.Printf("SESSION_COPY_FAIL id=%d direction=%s bytes=%d err=%v", id, direction, n, err)
			}
			done <- direction
		}
		go copyOne("local_to_tunnel", remote, local)
		go copyOne("tunnel_to_local", local, remote)

		select {
		case <-done:
		case <-r.ctx.Done():
		}
		_ = local.Close()
		_ = remote.Close()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
		}
		log.Printf("SESSION_CLOSED id=%d mapping=%s", id, label)
	}()
}

func (r *relay) serve(listener net.Listener, label string, dial func(context.Context) (net.Conn, error)) {
	r.wg.Add(1)
	go func() {
		defer r.wg.Done()
		defer listener.Close()
		for {
			c, err := listener.Accept()
			if err != nil {
				if r.ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
					return
				}
				log.Printf("ACCEPT_FAIL mapping=%s err=%v", label, err)
				continue
			}
			r.runSession(c, dial, label)
		}
	}()
}

func main() {
	var addresses multiFlag
	var dnsServers multiFlag
	var allowedIPs multiFlag
	var forwards multiFlag
	var reverses multiFlag

	privateKeyFile := flag.String("private-key-file", "", "mode-0600 file containing the local WireGuard private key (base64 or hex)")
	peerPublicKey := flag.String("peer-public-key", "", "peer WireGuard public key (base64 or hex)")
	peerPSKFile := flag.String("peer-preshared-key-file", "", "optional mode-0600 WireGuard preshared key file")
	peerEndpoint := flag.String("peer-endpoint", "", "peer UDP endpoint host:port")
	listenPort := flag.Int("listen-port", 0, "local WireGuard UDP listen port (0 chooses an ephemeral port)")
	keepalive := flag.Int("persistent-keepalive", 25, "WireGuard persistent keepalive interval in seconds (0 disables)")
	mtu := flag.Int("mtu", 1420, "userspace tunnel MTU")
	maxSessions := flag.Int("max-sessions", 64, "maximum concurrent relayed TCP sessions")
	verbose := flag.Bool("verbose", false, "enable verbose wireguard-go logging")
	flag.Var(&addresses, "address", "local userspace tunnel address/prefix; repeatable, e.g. 10.77.0.2/32")
	flag.Var(&dnsServers, "dns", "userspace DNS server address; repeatable")
	flag.Var(&allowedIPs, "allowed-ip", "peer AllowedIPs prefix; repeatable")
	flag.Var(&forwards, "forward", "native localhost/host listener to userspace-WireGuard target: LISTEN=TARGET; repeatable")
	flag.Var(&reverses, "reverse", "userspace-WireGuard listener to native host target: TUNNEL_LISTEN=TARGET; repeatable")
	flag.Parse()

	if *privateKeyFile == "" || *peerPublicKey == "" || *peerEndpoint == "" || len(addresses) == 0 || len(allowedIPs) == 0 {
		flag.Usage()
		os.Exit(2)
	}
	if len(forwards) == 0 && len(reverses) == 0 {
		log.Fatal("at least one --forward or --reverse mapping is required")
	}
	if *listenPort < 0 || *listenPort > 65535 || *keepalive < 0 || *keepalive > 65535 || *mtu < 576 || *mtu > 65535 || *maxSessions < 1 {
		log.Fatal("invalid listen-port, persistent-keepalive, mtu, or max-sessions")
	}

	privateHex, err := secretKeyFileHex(*privateKeyFile)
	if err != nil {
		log.Fatalf("private key: %v", err)
	}
	peerHex, err := wireGuardKeyHex(*peerPublicKey)
	if err != nil {
		log.Fatalf("peer public key: %v", err)
	}
	pskHex := ""
	if *peerPSKFile != "" {
		pskHex, err = secretKeyFileHex(*peerPSKFile)
		if err != nil {
			log.Fatalf("preshared key: %v", err)
		}
	}

	localAddrs := make([]netip.Addr, 0, len(addresses))
	for _, value := range addresses {
		p, err := netip.ParsePrefix(value)
		if err != nil {
			log.Fatalf("address %q: %v", value, err)
		}
		localAddrs = append(localAddrs, p.Addr())
	}
	dnsAddrs := make([]netip.Addr, 0, len(dnsServers))
	for _, value := range dnsServers {
		a, err := netip.ParseAddr(value)
		if err != nil {
			log.Fatalf("dns %q: %v", value, err)
		}
		dnsAddrs = append(dnsAddrs, a)
	}
	normalizedAllowed := make([]string, 0, len(allowedIPs))
	for _, value := range allowedIPs {
		p, err := netip.ParsePrefix(value)
		if err != nil {
			log.Fatalf("allowed-ip %q: %v", value, err)
		}
		normalizedAllowed = append(normalizedAllowed, p.String())
	}
	if err := validateHostPort(*peerEndpoint); err != nil {
		log.Fatalf("peer endpoint %q: %v", *peerEndpoint, err)
	}

	tunDev, tnet, err := netstack.CreateNetTUN(localAddrs, dnsAddrs, *mtu)
	if err != nil {
		log.Fatalf("create userspace netstack: %v", err)
	}
	logLevel := device.LogLevelError
	if *verbose {
		logLevel = device.LogLevelVerbose
	}
	dev := device.NewDevice(tunDev, conn.NewDefaultBind(), device.NewLogger(logLevel, "userguard/wireguard: "))

	var uapi strings.Builder
	fmt.Fprintf(&uapi, "private_key=%s\n", privateHex)
	fmt.Fprintf(&uapi, "listen_port=%d\n", *listenPort)
	uapi.WriteString("replace_peers=true\n")
	fmt.Fprintf(&uapi, "public_key=%s\n", peerHex)
	if pskHex != "" {
		fmt.Fprintf(&uapi, "preshared_key=%s\n", pskHex)
	}
	fmt.Fprintf(&uapi, "endpoint=%s\n", *peerEndpoint)
	fmt.Fprintf(&uapi, "persistent_keepalive_interval=%d\n", *keepalive)
	uapi.WriteString("replace_allowed_ips=true\n")
	for _, prefix := range normalizedAllowed {
		fmt.Fprintf(&uapi, "allowed_ip=%s\n", prefix)
	}
	if err := dev.IpcSet(uapi.String()); err != nil {
		log.Fatalf("configure wireguard-go: %v", err)
	}
	if err := dev.Up(); err != nil {
		log.Fatalf("bring userspace WireGuard device up: %v", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var wg sync.WaitGroup
	r := &relay{ctx: ctx, sem: make(chan struct{}, *maxSessions), wg: &wg}
	listeners := make([]net.Listener, 0, len(forwards)+len(reverses))

	for _, spec := range forwards {
		m, err := parseMapping(spec)
		if err != nil {
			log.Fatal(err)
		}
		if err := validateHostPort(m.listen); err != nil {
			log.Fatalf("forward listen %q: %v", m.listen, err)
		}
		if err := validateHostPort(m.target); err != nil {
			log.Fatalf("forward target %q: %v", m.target, err)
		}
		ln, err := net.Listen("tcp", m.listen)
		if err != nil {
			log.Fatalf("listen %s: %v", m.listen, err)
		}
		listeners = append(listeners, ln)
		target := m.target
		label := fmt.Sprintf("forward:%s=>%s", m.listen, m.target)
		r.serve(ln, label, func(c context.Context) (net.Conn, error) {
			return tnet.DialContext(c, "tcp", target)
		})
		log.Printf("FORWARD_READY listen=%s target=%s", m.listen, m.target)
	}

	for _, spec := range reverses {
		m, err := parseMapping(spec)
		if err != nil {
			log.Fatal(err)
		}
		ap, err := netip.ParseAddrPort(m.listen)
		if err != nil {
			log.Fatalf("reverse tunnel listen %q: %v", m.listen, err)
		}
		if err := validateHostPort(m.target); err != nil {
			log.Fatalf("reverse target %q: %v", m.target, err)
		}
		ln, err := tnet.ListenTCPAddrPort(ap)
		if err != nil {
			log.Fatalf("userspace listen %s: %v", m.listen, err)
		}
		listeners = append(listeners, ln)
		target := m.target
		label := fmt.Sprintf("reverse:%s=>%s", m.listen, m.target)
		r.serve(ln, label, func(c context.Context) (net.Conn, error) {
			var d net.Dialer
			return d.DialContext(c, "tcp", target)
		})
		log.Printf("REVERSE_READY listen=%s target=%s", m.listen, m.target)
	}

	log.Printf("USERGUARD_READY addresses=%s endpoint=%s allowed_ips=%s max_sessions=%d", strings.Join(addresses, ","), *peerEndpoint, strings.Join(normalizedAllowed, ","), *maxSessions)
	<-ctx.Done()
	log.Printf("USERGUARD_STOPPING")
	for _, ln := range listeners {
		_ = ln.Close()
	}
	dev.Close()
	wg.Wait()
	log.Printf("USERGUARD_STOPPED")
}
