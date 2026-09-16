#ifndef DM_MC02_TRANSPORT_H
#define DM_MC02_TRANSPORT_H

#include "dm_mc02_protocol.h"

#include <stddef.h>
#include <stdint.h>

typedef struct DmMc02TransportListener DmMc02TransportListener;
typedef struct DmMc02Transport DmMc02Transport;

typedef enum DmMc02TransportMode {
    DM_MC02_TRANSPORT_BLOCKING = 0,
    DM_MC02_TRANSPORT_NONBLOCKING = 1,
} DmMc02TransportMode;

/* A successful operation returns OK.  EOF means an orderly peer shutdown.
 * WOULD_BLOCK is only expected for a non-blocking object; retrying the same
 * operation continues any partially transferred frame.  Passing a different
 * frame while a send is pending returns INVALID instead of silently dropping
 * the new frame. */
typedef enum DmMc02TransportResult {
    DM_MC02_TRANSPORT_OK = 0,
    DM_MC02_TRANSPORT_EOF = 1,
    DM_MC02_TRANSPORT_WOULD_BLOCK = 2,
    DM_MC02_TRANSPORT_INVALID = -1,
    DM_MC02_TRANSPORT_PROTOCOL = -2,
    DM_MC02_TRANSPORT_SYSTEM = -3,
} DmMc02TransportResult;

/* The returned fd is suitable for poll(2)/select(2).  For a non-blocking
 * connect, creation may return WOULD_BLOCK with *out_transport set; polling
 * for writability and retrying an operation completes the connection. */
int dm_mc02_transport_fd(const DmMc02Transport *transport);

DmMc02TransportResult dm_mc02_transport_unix_listener_create(
    const char *path, DmMc02TransportMode mode,
    DmMc02TransportListener **out_listener);
DmMc02TransportResult dm_mc02_transport_tcp_listener_create(
    const char *bind_address, uint16_t port, DmMc02TransportMode mode,
    DmMc02TransportListener **out_listener);
void dm_mc02_transport_listener_close(DmMc02TransportListener *listener);
DmMc02TransportResult dm_mc02_transport_accept(
    DmMc02TransportListener *listener, DmMc02Transport **out_transport);

DmMc02TransportResult dm_mc02_transport_unix_connect(
    const char *path, DmMc02TransportMode mode, DmMc02Transport **out_transport);
DmMc02TransportResult dm_mc02_transport_tcp_connect(
    const char *address, uint16_t port, DmMc02TransportMode mode,
    DmMc02Transport **out_transport);
void dm_mc02_transport_close(DmMc02Transport *transport);

DmMc02TransportResult dm_mc02_transport_send_frame(
    DmMc02Transport *transport, const DmMc02Frame *frame);
DmMc02TransportResult dm_mc02_transport_recv_frame(
    DmMc02Transport *transport, DmMc02Frame *frame);

#endif
