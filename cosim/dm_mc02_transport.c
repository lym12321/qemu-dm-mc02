#ifndef _DEFAULT_SOURCE
#define _DEFAULT_SOURCE 1
#endif

#include "dm_mc02_transport.h"

#include <errno.h>
#include <fcntl.h>
#include <arpa/inet.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <netinet/in.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define FRAME_MAX (DM_MC02_PROTOCOL_HEADER_SIZE + DM_MC02_PROTOCOL_MAX_PAYLOAD)

struct DmMc02TransportListener {
    int fd;
    DmMc02TransportMode mode;
    bool is_unix;
    char unix_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    dev_t unix_dev;
    ino_t unix_ino;
};
struct DmMc02Transport {
    int fd;
    DmMc02TransportMode mode;
    bool connecting;
    uint8_t tx[4 + FRAME_MAX];
    size_t tx_len, tx_pos;
    uint8_t rx[4 + FRAME_MAX];
    size_t rx_have, rx_need;
};

#ifdef MSG_NOSIGNAL
#define DM_MC02_SEND_FLAGS MSG_NOSIGNAL
#else
#define DM_MC02_SEND_FLAGS 0
#endif

static DmMc02TransportResult result_errno(void)
{
    return (errno == EAGAIN || errno == EWOULDBLOCK) ?
        DM_MC02_TRANSPORT_WOULD_BLOCK : DM_MC02_TRANSPORT_SYSTEM;
}

static int set_mode(int fd, DmMc02TransportMode mode)
{
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) return -1;
    if (mode == DM_MC02_TRANSPORT_NONBLOCKING)
        flags |= O_NONBLOCK;
    else
        flags &= ~O_NONBLOCK;
    return fcntl(fd, F_SETFL, flags);
}

static int set_cloexec(int fd)
{
    int flags = fcntl(fd, F_GETFD, 0);
    return flags < 0 ? -1 : fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
}

static int socket_stream(int domain)
{
    int fd = socket(domain, SOCK_STREAM, 0);
    if (fd >= 0 && set_cloexec(fd) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static DmMc02TransportResult make_transport(int fd, DmMc02TransportMode mode,
                                             DmMc02Transport **out)
{
    DmMc02Transport *t;
    if (!out) { close(fd); return DM_MC02_TRANSPORT_INVALID; }
    t = (DmMc02Transport *)calloc(1, sizeof(*t));
    if (!t) { close(fd); return DM_MC02_TRANSPORT_SYSTEM; }
    t->fd = fd; t->mode = mode; t->rx_need = 4;
    if (set_cloexec(fd) < 0 || set_mode(fd, mode) < 0) {
        free(t); close(fd); return DM_MC02_TRANSPORT_SYSTEM;
    }
    *out = t;
    return DM_MC02_TRANSPORT_OK;
}

static DmMc02TransportResult make_listener(int fd, DmMc02TransportMode mode,
                                            DmMc02TransportListener **out)
{
    DmMc02TransportListener *l;
    if (!out) { close(fd); return DM_MC02_TRANSPORT_INVALID; }
    l = (DmMc02TransportListener *)calloc(1, sizeof(*l));
    if (!l) { close(fd); return DM_MC02_TRANSPORT_SYSTEM; }
    l->fd = fd; l->mode = mode;
    if (set_cloexec(fd) < 0 || set_mode(fd, mode) < 0) {
        free(l); close(fd); return DM_MC02_TRANSPORT_SYSTEM;
    }
    *out = l;
    return DM_MC02_TRANSPORT_OK;
}

DmMc02TransportResult dm_mc02_transport_unix_listener_create(const char *path,
    DmMc02TransportMode mode, DmMc02TransportListener **out)
{
    struct sockaddr_un sa;
    int fd;
    if (!path || !out || strlen(path) >= sizeof(sa.sun_path)) return DM_MC02_TRANSPORT_INVALID;
    fd = socket_stream(AF_UNIX);
    if (fd < 0) return DM_MC02_TRANSPORT_SYSTEM;
    memset(&sa, 0, sizeof(sa)); sa.sun_family = AF_UNIX;
    strcpy(sa.sun_path, path);
    {
        struct stat st;
        if (lstat(path, &st) == 0) {
            if (!S_ISSOCK(st.st_mode) || unlink(path) < 0) {
                close(fd);
                return DM_MC02_TRANSPORT_SYSTEM;
            }
        } else if (errno != ENOENT) {
            close(fd);
            return DM_MC02_TRANSPORT_SYSTEM;
        }
    }
    if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0 || listen(fd, 16) < 0) {
        close(fd); return DM_MC02_TRANSPORT_SYSTEM;
    }
    if (make_listener(fd, mode, out) != DM_MC02_TRANSPORT_OK) return DM_MC02_TRANSPORT_SYSTEM;
    (*out)->is_unix = true;
    strcpy((*out)->unix_path, path);
    {
        struct stat st;
        if (lstat(path, &st) < 0) {
            dm_mc02_transport_listener_close(*out);
            *out = NULL;
            return DM_MC02_TRANSPORT_SYSTEM;
        }
        (*out)->unix_dev = st.st_dev;
        (*out)->unix_ino = st.st_ino;
    }
    return DM_MC02_TRANSPORT_OK;
}

DmMc02TransportResult dm_mc02_transport_tcp_listener_create(const char *address,
    uint16_t port, DmMc02TransportMode mode, DmMc02TransportListener **out)
{
    struct sockaddr_in sa;
    int fd, yes = 1;
    if (!out) return DM_MC02_TRANSPORT_INVALID;
    fd = socket_stream(AF_INET);
    if (fd < 0) return DM_MC02_TRANSPORT_SYSTEM;
    memset(&sa, 0, sizeof(sa)); sa.sin_family = AF_INET; sa.sin_port = htons(port);
    sa.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (address && inet_pton(AF_INET, address, &sa.sin_addr) != 1) { close(fd); return DM_MC02_TRANSPORT_INVALID; }
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0 || listen(fd, 16) < 0) { close(fd); return DM_MC02_TRANSPORT_SYSTEM; }
    return make_listener(fd, mode, out);
}

void dm_mc02_transport_listener_close(DmMc02TransportListener *l)
{
    if (!l) return;
    close(l->fd);
    if (l->is_unix) {
        struct stat st;
        if (lstat(l->unix_path, &st) == 0 && st.st_dev == l->unix_dev &&
            st.st_ino == l->unix_ino)
            unlink(l->unix_path);
    }
    free(l);
}

DmMc02TransportResult dm_mc02_transport_accept(DmMc02TransportListener *l, DmMc02Transport **out)
{
    int fd;
    if (!l || !out) return DM_MC02_TRANSPORT_INVALID;
    do {
        fd = accept(l->fd, NULL, NULL);
    } while (fd < 0 && errno == EINTR);
    if (fd < 0) return result_errno();
    return make_transport(fd, l->mode, out);
}

static DmMc02TransportResult connect_fd(int fd, const struct sockaddr *sa, socklen_t len,
                                        DmMc02TransportMode mode, DmMc02Transport **out)
{
    DmMc02TransportResult result;
    int saved_errno;

    if (set_mode(fd, mode) < 0) {
        close(fd);
        return DM_MC02_TRANSPORT_SYSTEM;
    }
    do {
        if (connect(fd, sa, len) == 0)
            return make_transport(fd, mode, out);
    } while (errno == EINTR);
    saved_errno = errno;
    if (mode != DM_MC02_TRANSPORT_NONBLOCKING ||
        (saved_errno != EINPROGRESS && saved_errno != EALREADY &&
         saved_errno != EWOULDBLOCK)) {
        close(fd);
        return DM_MC02_TRANSPORT_SYSTEM;
    }
    result = make_transport(fd, mode, out);
    if (result == DM_MC02_TRANSPORT_OK) {
        (*out)->connecting = true;
        return DM_MC02_TRANSPORT_WOULD_BLOCK;
    }
    return result;
}

DmMc02TransportResult dm_mc02_transport_unix_connect(const char *path,
    DmMc02TransportMode mode, DmMc02Transport **out)
{
    struct sockaddr_un sa; int fd;
    if (!path || !out || strlen(path) >= sizeof(sa.sun_path)) return DM_MC02_TRANSPORT_INVALID;
    fd = socket_stream(AF_UNIX); if (fd < 0) return DM_MC02_TRANSPORT_SYSTEM;
    memset(&sa, 0, sizeof(sa)); sa.sun_family = AF_UNIX; strcpy(sa.sun_path, path);
    return connect_fd(fd, (struct sockaddr *)&sa, sizeof(sa), mode, out);
}

DmMc02TransportResult dm_mc02_transport_tcp_connect(const char *address,
    uint16_t port, DmMc02TransportMode mode, DmMc02Transport **out)
{
    struct sockaddr_in sa; int fd;
    if (!address || !out) return DM_MC02_TRANSPORT_INVALID;
    fd = socket_stream(AF_INET); if (fd < 0) return DM_MC02_TRANSPORT_SYSTEM;
    memset(&sa, 0, sizeof(sa)); sa.sin_family = AF_INET; sa.sin_port = htons(port);
    if (inet_pton(AF_INET, address, &sa.sin_addr) != 1) { close(fd); return DM_MC02_TRANSPORT_INVALID; }
    return connect_fd(fd, (struct sockaddr *)&sa, sizeof(sa), mode, out);
}

void dm_mc02_transport_close(DmMc02Transport *t) { if (t) { close(t->fd); free(t); } }

int dm_mc02_transport_fd(const DmMc02Transport *t) { return t ? t->fd : -1; }

static DmMc02TransportResult finish_connect(DmMc02Transport *t)
{
    int error = 0;
    socklen_t length = sizeof(error);
    if (!t->connecting) return DM_MC02_TRANSPORT_OK;
    if (getsockopt(t->fd, SOL_SOCKET, SO_ERROR, &error, &length) < 0)
        return DM_MC02_TRANSPORT_SYSTEM;
    if (error == 0) {
        t->connecting = false;
        return DM_MC02_TRANSPORT_OK;
    }
    if (error == EINPROGRESS || error == EALREADY || error == EWOULDBLOCK)
        return DM_MC02_TRANSPORT_WOULD_BLOCK;
    errno = error;
    return DM_MC02_TRANSPORT_SYSTEM;
}

static void put_len(uint8_t *p, uint32_t n) { p[0]=n; p[1]=n>>8; p[2]=n>>16; p[3]=n>>24; }
static uint32_t get_len(const uint8_t *p) { return (uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24); }

DmMc02TransportResult dm_mc02_transport_send_frame(DmMc02Transport *t, const DmMc02Frame *f)
{
    ssize_t n;
    DmMc02TransportResult connection;
    size_t nframe;
    uint8_t candidate[4 + FRAME_MAX];
    if (!t || !f) return DM_MC02_TRANSPORT_INVALID;
    connection = finish_connect(t);
    if (connection != DM_MC02_TRANSPORT_OK) return connection;
    nframe = dm_mc02_frame_encode(candidate + 4, FRAME_MAX, f);
    if (!nframe) return DM_MC02_TRANSPORT_PROTOCOL;
    put_len(candidate, (uint32_t)nframe);
    if (t->tx_pos != t->tx_len) {
        /* A non-blocking send is a stateful operation.  Silently treating a
         * different frame as a retry would lose the new frame while the old
         * one is still pending.  Require the caller to finish the same frame
         * before accepting another one. */
        if (t->tx_len != nframe + 4 ||
            memcmp(t->tx, candidate, nframe + 4) != 0) {
            return DM_MC02_TRANSPORT_INVALID;
        }
    } else {
        memcpy(t->tx, candidate, nframe + 4);
        t->tx_len = nframe + 4;
        t->tx_pos = 0;
    }
    while (t->tx_pos < t->tx_len) {
        n = send(t->fd, t->tx + t->tx_pos, t->tx_len - t->tx_pos, DM_MC02_SEND_FLAGS);
        if (n > 0) t->tx_pos += (size_t)n;
        else if (n < 0 && errno == EINTR) continue;
        else if (n < 0) return result_errno();
        else return DM_MC02_TRANSPORT_EOF;
    }
    t->tx_len = t->tx_pos = 0; return DM_MC02_TRANSPORT_OK;
}

DmMc02TransportResult dm_mc02_transport_recv_frame(DmMc02Transport *t, DmMc02Frame *f)
{
    ssize_t n;
    DmMc02TransportResult connection;
    if (!t || !f) return DM_MC02_TRANSPORT_INVALID;
    connection = finish_connect(t);
    if (connection != DM_MC02_TRANSPORT_OK) return connection;
    while (t->rx_have < t->rx_need) {
        n = recv(t->fd, t->rx + t->rx_have, t->rx_need - t->rx_have, 0);
        if (n > 0) t->rx_have += (size_t)n;
        else if (n < 0 && errno == EINTR) continue;
        else if (n < 0) return result_errno();
        else return DM_MC02_TRANSPORT_EOF;
    }
    if (t->rx_need == 4) {
        uint32_t length = get_len(t->rx);
        if (length < DM_MC02_PROTOCOL_HEADER_SIZE || length > FRAME_MAX) {
            t->rx_have = 0; return DM_MC02_TRANSPORT_PROTOCOL;
        }
        t->rx_need = length + 4;
    }
    while (t->rx_have < t->rx_need) {
        n = recv(t->fd, t->rx + t->rx_have, t->rx_need - t->rx_have, 0);
        if (n > 0) t->rx_have += (size_t)n;
        else if (n < 0 && errno == EINTR) continue;
        else if (n < 0) return result_errno();
        else return DM_MC02_TRANSPORT_EOF;
    }
    if (!dm_mc02_frame_decode(f, t->rx + 4, t->rx_need - 4)) {
        t->rx_have = 0; t->rx_need = 4; return DM_MC02_TRANSPORT_PROTOCOL;
    }
    t->rx_have = 0; t->rx_need = 4; return DM_MC02_TRANSPORT_OK;
}
