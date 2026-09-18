#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <lwip/err.h>
#include <lwip/init.h>
#include <lwip/ip4_addr.h>
#include <lwip/netif.h>
#include <lwip/pbuf.h>
#include <lwip/sockets.h>
#include <lwip/tcpip.h>

static struct netif g_netif;
static int g_packet_fd = -1;
static volatile sig_atomic_t g_listener_fd = -1;
static atomic_bool g_stop = false;
static atomic_bool g_lwip_ready = false;
static atomic_uint g_active_sessions = 0;
static atomic_ullong g_next_session_id = 1;
static unsigned g_max_sessions = 32;

static void on_signal(int sig) {
    (void)sig;
    atomic_store(&g_stop, true);
    if (g_listener_fd >= 0) {
        close((int)g_listener_fd);
        g_listener_fd = -1;
    }
    if (g_packet_fd >= 0) {
        shutdown(g_packet_fd, SHUT_RDWR);
    }
}

static void lwip_ready_cb(void *arg) {
    (void)arg;
    atomic_store(&g_lwip_ready, true);
}

static void log_inner_packet(const char *dir, const uint8_t *buf, size_t len) {
    if (len < 20 || (buf[0] >> 4) != 4) {
        return;
    }
    unsigned ihl = (unsigned)(buf[0] & 0x0f) * 4U;
    if (ihl < 20 || len < ihl) {
        return;
    }
    if (buf[9] == IPPROTO_TCP && len >= ihl + 4) {
        unsigned sport = ((unsigned)buf[ihl] << 8) | buf[ihl + 1];
        unsigned dport = ((unsigned)buf[ihl + 2] << 8) | buf[ihl + 3];
        fprintf(stderr,
                "%s_INNER bytes=%zu proto=6 %u.%u.%u.%u:%u -> %u.%u.%u.%u:%u\n",
                dir, len,
                buf[12], buf[13], buf[14], buf[15], sport,
                buf[16], buf[17], buf[18], buf[19], dport);
    } else {
        fprintf(stderr,
                "%s_INNER bytes=%zu proto=%u %u.%u.%u.%u -> %u.%u.%u.%u\n",
                dir, len, (unsigned)buf[9],
                buf[12], buf[13], buf[14], buf[15],
                buf[16], buf[17], buf[18], buf[19]);
    }
}

static err_t notun_output(struct netif *netif, struct pbuf *p, const ip4_addr_t *dst) {
    (void)netif;
    (void)dst;
    if (g_packet_fd < 0 || p->tot_len == 0) {
        return ERR_IF;
    }
    uint8_t *buf = malloc(p->tot_len);
    if (!buf) {
        return ERR_MEM;
    }
    u16_t copied = pbuf_copy_partial(p, buf, p->tot_len, 0);
    if (copied != p->tot_len) {
        free(buf);
        return ERR_BUF;
    }
    ssize_t n = send(g_packet_fd, buf, p->tot_len, 0);
    if (n > 0) {
        log_inner_packet("TX", buf, p->tot_len);
    }
    free(buf);
    return n == (ssize_t)p->tot_len ? ERR_OK : ERR_IF;
}

static err_t notun_netif_init(struct netif *netif) {
    netif->name[0] = 'n';
    netif->name[1] = 't';
    netif->mtu = 1400;
    netif->output = notun_output;
    netif->flags = NETIF_FLAG_UP | NETIF_FLAG_LINK_UP;
    return ERR_OK;
}

static void *packet_rx_thread(void *arg) {
    (void)arg;
    uint8_t buf[65536];
    while (!atomic_load(&g_stop)) {
        ssize_t n = recv(g_packet_fd, buf, sizeof(buf), 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (atomic_load(&g_stop)) {
                break;
            }
            perror("packet recv");
            usleep(100000);
            continue;
        }
        if (n == 0) {
            continue;
        }
        if ((buf[0] >> 4) != 4 || n < 20) {
            fprintf(stderr, "RX_DROP non_ipv4 bytes=%zd\n", n);
            continue;
        }
        log_inner_packet("RX", buf, (size_t)n);
        struct pbuf *p = pbuf_alloc(PBUF_RAW, (u16_t)n, PBUF_RAM);
        if (!p) {
            fprintf(stderr, "RX_DROP pbuf_alloc_failed bytes=%zd\n", n);
            continue;
        }
        if (pbuf_take(p, buf, (u16_t)n) != ERR_OK) {
            pbuf_free(p);
            fprintf(stderr, "RX_DROP pbuf_take_failed\n");
            continue;
        }
        err_t e = tcpip_input(p, &g_netif);
        if (e != ERR_OK) {
            pbuf_free(p);
            fprintf(stderr, "RX_DROP tcpip_input=%d\n", (int)e);
        }
    }
    return NULL;
}

struct bridge_ctx {
    int local_fd;
    int remote_fd;
    uint64_t session_id;
    atomic_bool done;
};

static bool retryable_io_error(void) {
    return errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK;
}

static void *local_to_remote(void *arg) {
    struct bridge_ctx *c = arg;
    uint8_t buf[16384];
    while (!atomic_load(&c->done) && !atomic_load(&g_stop)) {
        ssize_t n = recv(c->local_fd, buf, sizeof(buf), 0);
        if (n < 0) {
            if (retryable_io_error()) {
                continue;
            }
            fprintf(stderr, "PROXY_LOCAL_RECV_FAIL session=%llu errno=%d\n",
                    (unsigned long long)c->session_id, errno);
            break;
        }
        if (n == 0) {
            break;
        }
        size_t off = 0;
        while (off < (size_t)n && !atomic_load(&g_stop)) {
            ssize_t w = lwip_send(c->remote_fd, buf + off, (size_t)n - off, 0);
            if (w < 0) {
                if (retryable_io_error()) {
                    continue;
                }
                fprintf(stderr, "PROXY_REMOTE_SEND_FAIL session=%llu errno=%d\n",
                        (unsigned long long)c->session_id, errno);
                goto out;
            }
            if (w == 0) {
                goto out;
            }
            off += (size_t)w;
        }
        fprintf(stderr, "PROXY local_to_remote session=%llu bytes=%zd\n",
                (unsigned long long)c->session_id, n);
    }
out:
    atomic_store(&c->done, true);
    lwip_shutdown(c->remote_fd, SHUT_WR);
    shutdown(c->local_fd, SHUT_RD);
    return NULL;
}

static void *remote_to_local(void *arg) {
    struct bridge_ctx *c = arg;
    uint8_t buf[16384];
    while (!atomic_load(&c->done) && !atomic_load(&g_stop)) {
        ssize_t n = lwip_recv(c->remote_fd, buf, sizeof(buf), 0);
        if (n < 0) {
            if (retryable_io_error()) {
                continue;
            }
            fprintf(stderr, "PROXY_REMOTE_RECV_FAIL session=%llu errno=%d\n",
                    (unsigned long long)c->session_id, errno);
            break;
        }
        if (n == 0) {
            break;
        }
        size_t off = 0;
        while (off < (size_t)n && !atomic_load(&g_stop)) {
            ssize_t w = send(c->local_fd, buf + off, (size_t)n - off, MSG_NOSIGNAL);
            if (w < 0) {
                if (retryable_io_error()) {
                    continue;
                }
                fprintf(stderr, "PROXY_LOCAL_SEND_FAIL session=%llu errno=%d\n",
                        (unsigned long long)c->session_id, errno);
                goto out;
            }
            if (w == 0) {
                goto out;
            }
            off += (size_t)w;
        }
        fprintf(stderr, "PROXY remote_to_local session=%llu bytes=%zd\n",
                (unsigned long long)c->session_id, n);
    }
out:
    atomic_store(&c->done, true);
    shutdown(c->local_fd, SHUT_WR);
    lwip_shutdown(c->remote_fd, SHUT_RD);
    return NULL;
}

static int connect_remote(const char *remote_ip, uint16_t remote_port, uint64_t session_id) {
    int fd = lwip_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) {
        fprintf(stderr, "REMOTE_SOCKET_FAIL session=%llu errno=%d\n",
                (unsigned long long)session_id, errno);
        return -1;
    }

    struct timeval send_tv = {.tv_sec = 15, .tv_usec = 0};
    struct timeval recv_tv = {.tv_sec = 1, .tv_usec = 0};
    lwip_setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &send_tv, sizeof(send_tv));
    lwip_setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &recv_tv, sizeof(recv_tv));

    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons(remote_port);
    if (inet_pton(AF_INET, remote_ip, &sa.sin_addr) != 1) {
        lwip_close(fd);
        return -1;
    }

    fprintf(stderr, "REMOTE_CONNECT_START session=%llu %s:%u\n",
            (unsigned long long)session_id, remote_ip, remote_port);
    if (lwip_connect(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0) {
        fprintf(stderr, "REMOTE_CONNECT_FAIL session=%llu %s:%u errno=%d\n",
                (unsigned long long)session_id, remote_ip, remote_port, errno);
        lwip_close(fd);
        return -1;
    }

    struct sockaddr_in local;
    socklen_t llen = sizeof(local);
    char local_ip[INET_ADDRSTRLEN] = "?";
    unsigned local_port = 0;
    memset(&local, 0, sizeof(local));
    if (lwip_getsockname(fd, (struct sockaddr *)&local, &llen) == 0) {
        inet_ntop(AF_INET, &local.sin_addr, local_ip, sizeof(local_ip));
        local_port = ntohs(local.sin_port);
    }
    fprintf(stderr,
            "REMOTE_CONNECT_OK session=%llu local=%s:%u remote=%s:%u\n",
            (unsigned long long)session_id, local_ip, local_port,
            remote_ip, remote_port);
    return fd;
}

struct session_args {
    int local_fd;
    uint64_t session_id;
    char peer_ip[INET_ADDRSTRLEN];
    uint16_t peer_port;
    char remote_ip[INET_ADDRSTRLEN];
    uint16_t remote_port;
};

static void *session_thread(void *arg) {
    struct session_args *s = arg;
    int local = s->local_fd;
    uint64_t session_id = s->session_id;
    char remote_ip[INET_ADDRSTRLEN];
    snprintf(remote_ip, sizeof(remote_ip), "%s", s->remote_ip);
    uint16_t remote_port = s->remote_port;

    struct timeval local_tv = {.tv_sec = 1, .tv_usec = 0};
    setsockopt(local, SOL_SOCKET, SO_RCVTIMEO, &local_tv, sizeof(local_tv));
    setsockopt(local, SOL_SOCKET, SO_SNDTIMEO, &local_tv, sizeof(local_tv));

    fprintf(stderr,
            "LOCAL_ACCEPT session=%llu peer=%s:%u active=%u\n",
            (unsigned long long)session_id, s->peer_ip, s->peer_port,
            atomic_load(&g_active_sessions));
    free(s);

    int remote = connect_remote(remote_ip, remote_port, session_id);
    if (remote < 0) {
        close(local);
        fprintf(stderr, "LOCAL_SESSION_CLOSED session=%llu reason=remote_connect_failed\n",
                (unsigned long long)session_id);
        atomic_fetch_sub(&g_active_sessions, 1);
        return NULL;
    }

    struct bridge_ctx c = {
        .local_fd = local,
        .remote_fd = remote,
        .session_id = session_id,
    };
    atomic_init(&c.done, false);

    pthread_t rx_thread;
    bool rx_started = pthread_create(&rx_thread, NULL, remote_to_local, &c) == 0;
    if (!rx_started) {
        fprintf(stderr, "SESSION_THREAD_FAIL session=%llu direction=remote_to_local\n",
                (unsigned long long)session_id);
        atomic_store(&c.done, true);
    } else {
        local_to_remote(&c);
        pthread_join(rx_thread, NULL);
    }

    lwip_close(remote);
    close(local);
    unsigned remaining = atomic_fetch_sub(&g_active_sessions, 1) - 1;
    fprintf(stderr, "LOCAL_SESSION_CLOSED session=%llu active=%u\n",
            (unsigned long long)session_id, remaining);
    return NULL;
}

static int setup_packet_socket(const char *bind_path, const char *peer_path) {
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        perror("socket AF_UNIX");
        return -1;
    }
    struct sockaddr_un local, peer;
    memset(&local, 0, sizeof(local));
    memset(&peer, 0, sizeof(peer));
    local.sun_family = AF_UNIX;
    peer.sun_family = AF_UNIX;
    if (strlen(bind_path) >= sizeof(local.sun_path) || strlen(peer_path) >= sizeof(peer.sun_path)) {
        fprintf(stderr, "unix socket path too long\n");
        close(fd);
        return -1;
    }
    strcpy(local.sun_path, bind_path);
    strcpy(peer.sun_path, peer_path);
    unlink(bind_path);
    mode_t old = umask(0077);
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)) != 0) {
        umask(old);
        perror("bind packet client");
        close(fd);
        return -1;
    }
    umask(old);
    chmod(bind_path, 0600);
    if (connect(fd, (struct sockaddr *)&peer, sizeof(peer)) != 0) {
        perror("connect packet server");
        unlink(bind_path);
        close(fd);
        return -1;
    }
    return fd;
}

static int setup_listener(const char *listen_ip, uint16_t listen_port) {
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        perror("listener socket");
        return -1;
    }
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons(listen_port);
    if (inet_pton(AF_INET, listen_ip, &sa.sin_addr) != 1 ||
        bind(fd, (struct sockaddr *)&sa, sizeof(sa)) != 0 ||
        listen(fd, 64) != 0) {
        perror("listener bind/listen");
        close(fd);
        return -1;
    }
    return fd;
}

static void usage(const char *p) {
    fprintf(stderr,
            "usage: %s --vip IP --packet-socket PATH --remote-ip IP --remote-port PORT "
            "[--bind-socket PATH] [--listen-ip 127.0.0.1] [--listen-port PORT] "
            "[--max-sessions N]\n",
            p);
}

int main(int argc, char **argv) {
    const char *vip = NULL;
    const char *packet_socket = NULL;
    const char *bind_socket = "/tmp/strongswan-notun-forwarder.sock";
    const char *listen_ip = "127.0.0.1";
    const char *remote_ip = NULL;
    uint16_t listen_port = 3390, remote_port = 0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--vip") && i + 1 < argc) vip = argv[++i];
        else if (!strcmp(argv[i], "--packet-socket") && i + 1 < argc) packet_socket = argv[++i];
        else if (!strcmp(argv[i], "--bind-socket") && i + 1 < argc) bind_socket = argv[++i];
        else if (!strcmp(argv[i], "--listen-ip") && i + 1 < argc) listen_ip = argv[++i];
        else if (!strcmp(argv[i], "--listen-port") && i + 1 < argc) listen_port = (uint16_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--remote-ip") && i + 1 < argc) remote_ip = argv[++i];
        else if (!strcmp(argv[i], "--remote-port") && i + 1 < argc) remote_port = (uint16_t)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--max-sessions") && i + 1 < argc) g_max_sessions = (unsigned)atoi(argv[++i]);
        else if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
            usage(argv[0]);
            return 0;
        } else {
            usage(argv[0]);
            return 2;
        }
    }
    if (!vip || !packet_socket || !remote_ip || remote_port == 0 || listen_port == 0 || g_max_sessions == 0) {
        usage(argv[0]);
        return 2;
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = on_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    g_packet_fd = setup_packet_socket(bind_socket, packet_socket);
    if (g_packet_fd < 0) {
        return 3;
    }

    tcpip_init(lwip_ready_cb, NULL);
    for (int i = 0; i < 500 && !atomic_load(&g_lwip_ready); i++) {
        usleep(10000);
    }
    if (!atomic_load(&g_lwip_ready)) {
        fprintf(stderr, "lwIP init timeout\n");
        return 4;
    }

    ip4_addr_t ip, mask, gw;
    if (!ip4addr_aton(vip, &ip)) {
        fprintf(stderr, "bad VIP %s\n", vip);
        return 2;
    }
    IP4_ADDR(&mask, 255, 255, 255, 255);
    IP4_ADDR(&gw, 0, 0, 0, 0);
    LOCK_TCPIP_CORE();
    struct netif *n = netif_add(&g_netif, &ip, &mask, &gw, NULL, notun_netif_init, tcpip_input);
    if (n) {
        netif_set_default(&g_netif);
        netif_set_up(&g_netif);
        netif_set_link_up(&g_netif);
    }
    UNLOCK_TCPIP_CORE();
    if (!n) {
        fprintf(stderr, "netif_add failed\n");
        return 4;
    }

    pthread_t packet_rx;
    if (pthread_create(&packet_rx, NULL, packet_rx_thread, NULL) != 0) {
        perror("pthread packet_rx");
        return 4;
    }

    int listener = setup_listener(listen_ip, listen_port);
    if (listener < 0) {
        atomic_store(&g_stop, true);
        shutdown(g_packet_fd, SHUT_RDWR);
        pthread_join(packet_rx, NULL);
        return 5;
    }
    g_listener_fd = listener;
    fprintf(stderr,
            "LISTENER_READY %s:%u -> %s:%u vip=%s packet_socket=%s max_sessions=%u\n",
            listen_ip, listen_port, remote_ip, remote_port, vip, packet_socket,
            g_max_sessions);

    while (!atomic_load(&g_stop)) {
        struct sockaddr_in peer;
        socklen_t plen = sizeof(peer);
        int local = accept4(listener, (struct sockaddr *)&peer, &plen, SOCK_CLOEXEC);
        if (local < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (atomic_load(&g_stop) || errno == EBADF) {
                break;
            }
            perror("accept");
            continue;
        }

        unsigned active = atomic_load(&g_active_sessions);
        if (active >= g_max_sessions) {
            fprintf(stderr, "LOCAL_REJECT reason=max_sessions active=%u limit=%u\n",
                    active, g_max_sessions);
            close(local);
            continue;
        }

        struct session_args *s = calloc(1, sizeof(*s));
        if (!s) {
            close(local);
            continue;
        }
        s->local_fd = local;
        s->session_id = atomic_fetch_add(&g_next_session_id, 1);
        inet_ntop(AF_INET, &peer.sin_addr, s->peer_ip, sizeof(s->peer_ip));
        s->peer_port = ntohs(peer.sin_port);
        snprintf(s->remote_ip, sizeof(s->remote_ip), "%s", remote_ip);
        s->remote_port = remote_port;

        atomic_fetch_add(&g_active_sessions, 1);
        pthread_t session;
        if (pthread_create(&session, NULL, session_thread, s) != 0) {
            atomic_fetch_sub(&g_active_sessions, 1);
            fprintf(stderr, "SESSION_THREAD_FAIL session=%llu direction=session\n",
                    (unsigned long long)s->session_id);
            close(local);
            free(s);
            continue;
        }
        pthread_detach(session);
    }

    if (g_listener_fd >= 0) {
        close(listener);
        g_listener_fd = -1;
    }
    atomic_store(&g_stop, true);
    shutdown(g_packet_fd, SHUT_RDWR);

    for (int i = 0; i < 50 && atomic_load(&g_active_sessions) > 0; i++) {
        usleep(100000);
    }
    if (atomic_load(&g_active_sessions) > 0) {
        fprintf(stderr, "SHUTDOWN_PENDING active_sessions=%u\n",
                atomic_load(&g_active_sessions));
    }

    pthread_join(packet_rx, NULL);
    close(g_packet_fd);
    unlink(bind_socket);
    return 0;
}
