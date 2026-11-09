/* Native request transport, independent of board, descriptors and USB class.
 * Wire v1: BE32 payload length. Request: kind,id,ep,in,length,flags,setup[8],
 * followed by OUT bytes. Reply: kind,id,signed status,actual, then IN bytes.
 * Kind 1 transfer, 2 cancel (id is target), 3 bus reset, 4 connection event
 * (id=wire version 1, status=connected). Numeric error values follow USB/IP's
 * Linux wire ABI, including on Windows. Partial IO never discards a suffix. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_bridge.h"
#include "hw/usb/dm_usb_host.h"
#include "chardev/char-fe.h"
#include "qemu/main-loop.h"
#include "qemu/bswap.h"

#define MAX_TRANSFER (1024 * 1024)
#define MAX_BYTES (8 * MAX_TRANSFER)
#define MAX_REQUESTS 64

typedef struct Pending {
    uint32_t id;
    uint64_t order;
    DmUsbHostRequest request;
    uint8_t *data;
} Pending;

struct DmUsbBridge {
    DmUsbDwc2Device *device; /* borrowed */
    CharBackend chr;
    DmUsbHost host;
    QEMUBH *bh;
    GByteArray *rx, *tx;
    size_t tx_offset, bytes;
    uint64_t order;
    unsigned endpoint_count;
    guint watch;
    bool open, connected;
    Pending pending[MAX_REQUESTS];
};

static void flush(DmUsbBridge *b);

static gboolean writable(void *unused, GIOCondition condition, void *opaque)
{
    DmUsbBridge *b = opaque;
    b->watch = 0;
    if (!(condition & G_IO_HUP)) { flush(b); }
    return G_SOURCE_REMOVE;
}

static void flush(DmUsbBridge *b)
{
    int n;
    if (!b->open || b->tx_offset == b->tx->len) { return; }
    n = qemu_chr_fe_write(&b->chr, b->tx->data + b->tx_offset,
                          b->tx->len - b->tx_offset);
    if (n > 0) { b->tx_offset += n; }
    if (b->tx_offset == b->tx->len) {
        g_byte_array_set_size(b->tx, 0);
        b->tx_offset = 0;
    } else if (!b->watch) {
        b->watch = qemu_chr_fe_add_watch(&b->chr, G_IO_OUT | G_IO_HUP,
                                         writable, b);
    }
}

static void reply(DmUsbBridge *b, unsigned kind, uint32_t id, int status,
                    size_t actual, const uint8_t *data, size_t size)
{
    uint8_t header[20];
    if (!b->open) { return; }
    if (b->tx->len - b->tx_offset + 20 + size > MAX_BYTES) {
        qemu_chr_fe_disconnect(&b->chr);
        return;
    }
    if (b->tx_offset) {
        g_byte_array_remove_range(b->tx, 0, b->tx_offset);
        b->tx_offset = 0;
    }
    stl_be_p(header, 16 + size);
    stl_be_p(header + 4, kind);
    stl_be_p(header + 8, id);
    stl_be_p(header + 12, status);
    stl_be_p(header + 16, actual);
    g_byte_array_append(b->tx, header, sizeof(header));
    if (size) { g_byte_array_append(b->tx, data, size); }
    flush(b);
}

static void release(DmUsbBridge *b, Pending *p)
{
    if (!p->id) { return; }
    b->bytes -= p->request.length;
    g_free(p->data);
    memset(p, 0, sizeof(*p));
}

void dm_usb_bridge_reset(DmUsbBridge *b)
{
    if (!b) { return; }
    for (unsigned i = 0; i < MAX_REQUESTS; i++) {
        Pending *p = &b->pending[i];
        if (p->id) {
            reply(b, 1, p->id, -19, p->request.offset, p->data,
                  p->request.in ? p->request.offset : 0);
            release(b, p);
        }
    }
    qemu_bh_schedule(b->bh);
}

static DmUsbTransactionResult submit(void *opaque, const DmUsbTransaction *t)
{
    return dm_usb_dwc2_submit(opaque, t);
}

static int wire_status(DmUsbTransactionStatus status)
{
    switch (status) {
    case DM_USB_TRANSACTION_ACCEPTED: return 0;
    case DM_USB_TRANSACTION_STALL: return -32;
    case DM_USB_TRANSACTION_NO_DEVICE: return -19;
    case DM_USB_TRANSACTION_BABBLE: return -75;
    default: return -22;
    }
}

static void pump(void *opaque)
{
    DmUsbBridge *b = opaque;
    bool connected = dm_usb_dwc2_connected(b->device);
    if (!b->open) { return; }
    if (connected != b->connected) {
        b->connected = connected;
        if (!connected) { dm_usb_bridge_reset(b); }
        reply(b, 4, 1, connected, 0, NULL, 0);
    }
    for (unsigned i = 0; i < MAX_REQUESTS; i++) {
        Pending *p = &b->pending[i];
        DmUsbTransactionStatus status;
        bool earlier = false;
        if (!p->id) { continue; }
        /* Submission order is independent of caller-selected wire IDs. */
        for (unsigned j = 0; j < MAX_REQUESTS; j++) {
            Pending *q = &b->pending[j];
            if (q->id && q->order < p->order &&
                q->request.endpoint == p->request.endpoint &&
                (!p->request.endpoint || q->request.in == p->request.in)) {
                earlier = true;
            }
        }
        if (earlier) { continue; }
        status = connected ? dm_usb_host_request_pump(&p->request,
            qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) : DM_USB_TRANSACTION_NO_DEVICE;
        if (status == DM_USB_TRANSACTION_NAK) { continue; }
        if (status == DM_USB_TRANSACTION_DEFERRED) {
            qemu_bh_schedule(b->bh);
            continue;
        }
        reply(b, 1, p->id, wire_status(status), p->request.offset, p->data,
            p->request.in ? p->request.offset : 0);
        release(b, p);
        qemu_bh_schedule(b->bh);
    }
}

static void notify(void *opaque)
{
    DmUsbBridge *b = opaque;
    qemu_bh_schedule(b->bh);
}

static bool receive_frame(DmUsbBridge *b, const uint8_t *data, size_t size)
{
    unsigned kind = ldl_be_p(data), ep = ldl_be_p(data + 8), in = ldl_be_p(data + 12);
    uint32_t id = ldl_be_p(data + 4), length = ldl_be_p(data + 16);
    Pending *slot = NULL;
    if (!id || size < 32) { return false; }
    if (kind == 2) {
        for (unsigned i = 0; i < MAX_REQUESTS; i++) {
            if (b->pending[i].id == id) {
                size_t actual = b->pending[i].request.offset;
                release(b, &b->pending[i]);
                reply(b, 2, id, -104, actual, NULL, 0);
                qemu_bh_schedule(b->bh);
                return true;
            }
        }
        reply(b, 2, id, -2, 0, NULL, 0);
        return true;
    }
    if (kind == 3) {
        dm_usb_bridge_reset(b);
        dm_usb_dwc2_bus_reset(b->device, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        reply(b, 3, id, 0, 0, NULL, 0);
        return true;
    }
    if (kind != 1 || ep >= b->endpoint_count || in > 1 || length > MAX_TRANSFER ||
        size != 32 + (in ? 0 : length)) { return false; }
    for (unsigned i = 0; i < MAX_REQUESTS; i++) {
        if (b->pending[i].id == id) { return false; }
        if (!b->pending[i].id && !slot) { slot = &b->pending[i]; }
    }
    if (!slot || length + b->bytes > MAX_BYTES) {
        reply(b, 1, id, -12, 0, NULL, 0);
        return true;
    }
    slot->data = g_malloc0(length);
    if (!in && length) { memcpy(slot->data, data + 32, length); }
    if (!dm_usb_host_request_init(&slot->request, &b->host, ep, in,
        dm_usb_dwc2_max_packet(b->device, ep, in), ep ? NULL : data + 24,
        slot->data, length, (ldl_be_p(data + 20) & 0x40) != 0)) {
        g_free(slot->data);
        slot->data = NULL;
        reply(b, 1, id, -22, 0, NULL, 0);
        return true;
    }
    slot->id = id;
    slot->order = ++b->order;
    b->bytes += length;
    qemu_bh_schedule(b->bh);
    return true;
}

static int can_receive(void *opaque)
{
    DmUsbBridge *b = opaque;
    return MAX_TRANSFER + 36 - b->rx->len;
}

static void receive(void *opaque, const uint8_t *data, int length)
{
    DmUsbBridge *b = opaque;
    g_byte_array_append(b->rx, data, length);
    while (b->rx->len >= 4) {
        uint32_t size = ldl_be_p(b->rx->data);
        if (size < 32 || size > MAX_TRANSFER + 32) {
            qemu_chr_fe_disconnect(&b->chr);
            return;
        }
        if (b->rx->len < size + 4) { break; }
        if (!receive_frame(b, b->rx->data + 4, size)) {
            qemu_chr_fe_disconnect(&b->chr);
            return;
        }
        if (!b->open) { return; }
        g_byte_array_remove_range(b->rx, 0, size + 4);
    }
    qemu_chr_fe_accept_input(&b->chr);
}

static void event(void *opaque, QEMUChrEvent event)
{
    DmUsbBridge *b = opaque;
    if (event == CHR_EVENT_OPENED) {
        b->open = true;
        b->connected = dm_usb_dwc2_connected(b->device);
        reply(b, 4, 1, b->connected, 0, NULL, 0);
    } else if (event == CHR_EVENT_CLOSED) {
        b->open = false;
        if (b->watch) { g_source_remove(b->watch); b->watch = 0; }
        g_byte_array_set_size(b->rx, 0);
        g_byte_array_set_size(b->tx, 0);
        b->tx_offset = 0;
        dm_usb_bridge_reset(b);
    }
}

DmUsbBridge *dm_usb_bridge_new(DmUsbDwc2Device *device, unsigned endpoint_count,
                                Chardev *chr, Error **errp)
{
    DmUsbBridge *b = g_new0(DmUsbBridge, 1);
    b->device = device;
    b->endpoint_count = endpoint_count;
    b->rx = g_byte_array_new();
    b->tx = g_byte_array_new();
    b->bh = qemu_bh_new(pump, b);
    dm_usb_host_init(&b->host, submit, device, 64);
    if (!qemu_chr_fe_init(&b->chr, chr, errp)) {
        dm_usb_bridge_free(b);
        return NULL;
    }
    dm_usb_dwc2_set_notify(device, notify, b);
    qemu_chr_fe_set_handlers(&b->chr, can_receive, receive, event, NULL, b, NULL, true);
    return b;
}

void dm_usb_bridge_free(DmUsbBridge *b)
{
    if (!b) { return; }
    dm_usb_dwc2_set_notify(b->device, NULL, NULL);
    b->open = false;
    dm_usb_bridge_reset(b);
    if (b->watch) { g_source_remove(b->watch); }
    qemu_chr_fe_deinit(&b->chr, false);
    qemu_bh_delete(b->bh);
    g_byte_array_unref(b->rx);
    g_byte_array_unref(b->tx);
    g_free(b);
}
