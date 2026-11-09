/* Board-independent minimal DWC2 device-mode controller. */
#include "hw/usb/dm_usb_dwc2_device.h"

#include <string.h>

static uint32_t min_u32(uint32_t left, uint32_t right)
{
    return left < right ? left : right;
}

static size_t min_size(size_t left, size_t right)
{
    return left < right ? left : right;
}

static uint32_t access_mask(unsigned size)
{
    return size == 4 ? UINT32_MAX : (1u << (size * 8)) - 1;
}

static bool access_size_valid(unsigned size)
{
    return size == 1 || size == 2 || size == 4;
}

static bool register_access_valid(uint32_t offset, unsigned size,
                                  uint32_t base)
{
    return access_size_valid(size) && offset >= base &&
           offset - base + size <= 4;
}

static bool dieptxf_access_valid(uint32_t offset, unsigned size,
                                 unsigned *index)
{
    uint32_t relative;

    if (offset < DM_USB_DWC2_DIEPTXF1) {
        return false;
    }
    relative = offset - DM_USB_DWC2_DIEPTXF1;
    *index = relative / DM_USB_DWC2_DIEPTXF_STRIDE;
    return *index < DM_USB_DWC2_DIEPTXF_COUNT &&
           relative % DM_USB_DWC2_DIEPTXF_STRIDE == 0 && size <= 4 &&
           relative + size <= DM_USB_DWC2_DIEPTXF_COUNT *
                               DM_USB_DWC2_DIEPTXF_STRIDE;
}

static uint32_t reg_read(uint32_t value, uint32_t offset,
                         uint32_t base, unsigned size)
{
    unsigned shift = (offset - base) * 8;

    return (value >> shift) & access_mask(size);
}

static uint32_t reg_write(uint32_t value, uint32_t offset,
                          uint32_t base, uint64_t written, unsigned size)
{
    unsigned shift = (offset - base) * 8;
    uint32_t mask = access_mask(size) << shift;

    return (value & ~mask) | (((uint32_t)written & access_mask(size)) << shift);
}

static unsigned fifo_endpoint(uint32_t offset)
{
    if (offset < DM_USB_DWC2_FIFO0) {
        return DM_USB_DWC2_MAX_ENDPOINTS;
    }
    return (offset - DM_USB_DWC2_FIFO0) / DM_USB_DWC2_FIFO_STRIDE;
}

static bool fifo_offset_valid(uint32_t offset, unsigned size,
                              unsigned endpoint)
{
    uint32_t base = DM_USB_DWC2_FIFO0 +
                    endpoint * DM_USB_DWC2_FIFO_STRIDE;

    return endpoint < DM_USB_DWC2_MAX_ENDPOINTS && offset == base &&
           access_size_valid(size);
}

static uint16_t endpoint_mps(uint32_t control, unsigned endpoint,
                            uint16_t ep0_mps)
{
    uint16_t mps = control & DM_USB_DWC2_DXEPCTL_MPS_MASK;

    if (!mps && endpoint == 0) {
        mps = ep0_mps;
    }
    return mps;
}

static DmUsbEndpointType endpoint_type(uint32_t control)
{
    switch ((control & DM_USB_DWC2_DXEPCTL_EPTYPE_MASK) >> 18) {
    case 1:
        return DM_USB_ENDPOINT_ISOCHRONOUS;
    case 2:
        return DM_USB_ENDPOINT_BULK;
    case 3:
        return DM_USB_ENDPOINT_INTERRUPT;
    default:
        return DM_USB_ENDPOINT_CONTROL;
    }
}

static size_t fifo_free(size_t count)
{
    return DM_USB_DWC2_FIFO_BYTES - count;
}

static size_t fifo_capacity(const DmUsbDwc2Device *device)
{
    size_t bytes = (size_t)device->grxfsiz * 4;

    return min_size(bytes, DM_USB_DWC2_FIFO_BYTES);
}

static size_t fifo_push(uint8_t *fifo, size_t *head, size_t *count,
                        const uint8_t *data, size_t length)
{
    size_t tail;
    size_t first;

    length = min_size(length, fifo_free(*count));
    tail = (*head + *count) % DM_USB_DWC2_FIFO_BYTES;
    first = min_size(length, DM_USB_DWC2_FIFO_BYTES - tail);
    if (first) {
        memcpy(fifo + tail, data, first);
    }
    if (length > first) {
        memcpy(fifo, data + first, length - first);
    }
    *count += length;
    return length;
}

static size_t fifo_pop(uint8_t *fifo, size_t *head, size_t *count,
                       uint8_t *data, size_t length)
{
    size_t first;

    length = min_size(length, *count);
    first = min_size(length, DM_USB_DWC2_FIFO_BYTES - *head);
    if (first) {
        memcpy(data, fifo + *head, first);
    }
    if (length > first) {
        memcpy(data + first, fifo, length - first);
    }
    *head = (*head + length) % DM_USB_DWC2_FIFO_BYTES;
    *count -= length;
    return length;
}

/* PIO queue/endpoint notification organization adapted from Renode
 * DesignWare_USB_OTG at 042a58285ee91453f99d31f19e8aa140102ec540,
 * Copyright (c) 2010-2026 Antmicro, MIT (see LICENSES/renode-dwc2-MIT.txt).
 * Geometry, RX completion order and dedicated FIFO routing follow RM0468
 * Rev3 62.11, 62.14 and ST HAL PCD/CherryUSB's public DWC2 consumer. */
static unsigned tx_fifo(const DmUsbDwc2Device *d, unsigned ep)
{
    return d->control ? ep : (d->endpoint[ep].in_ctl >> 22) & 15;
}

static size_t tx_capacity(const DmUsbDwc2Device *d, unsigned fifo)
{
    uint32_t reg, start, depth;
    if (fifo >= d->endpoint_count) { return 0; }
    if (d->control) {
        return DM_USB_DWC2_FIFO_BYTES;
    }
    reg = fifo ? d->dieptxf[fifo - 1] : d->gnptxfsiz;
    start = reg & 0xffff;
    depth = reg >> 16;
    if (start < d->grxfsiz || start + depth > 1024) {
        return 0;
    }
    for (unsigned i = 0; i < d->endpoint_count; i++) {
        uint32_t other = i ? d->dieptxf[i - 1] : d->gnptxfsiz;
        unsigned other_start = other & 0xffff, other_depth = other >> 16;
        if (i != fifo && other_depth && start < other_start + other_depth &&
            other_start < start + depth) {
            return 0;
        }
    }
    return depth * 4;
}

uint16_t dm_usb_dwc2_max_packet(const DmUsbDwc2Device *d, unsigned ep, bool in)
{
    uint32_t ctl;
    if (ep >= d->endpoint_count) {
        return 0;
    }
    ctl = in ? d->endpoint[ep].in_ctl : d->endpoint[ep].out_ctl;
    return !d->control && ep == 0 ? 64 >> (ctl & 3) :
           endpoint_mps(ctl, ep, d->ep0_max_packet_size);
}

bool dm_usb_dwc2_connected(const DmUsbDwc2Device *d)
{
    return !(d->dctl & (1 << 1));
}

void dm_usb_dwc2_set_notify(DmUsbDwc2Device *d,
                            DmUsbDwc2Notify *notify, void *opaque)
{
    d->notify = notify;
    d->notify_opaque = opaque;
}

static uint32_t in_interrupt(const DmUsbDwc2Device *d, unsigned ep)
{
    const DmUsbDwc2Endpoint *e = &d->endpoint[ep];
    unsigned fifo = tx_fifo(d, ep);
    size_t capacity = tx_capacity(d, fifo);
    uint32_t value = e->in_int;
    if (!d->control && (e->in_ctl & DM_USB_DWC2_DXEPCTL_EPENA) &&
        (e->in_tsiz & DM_USB_DXEPTSIZ_XFERSIZE_MASK) &&
        d->endpoint[fifo].in_fifo_count + 4 <= capacity) {
        value |= 1 << 7; /* DIEPINT.TXFE is a level, not W1C. */
    }
    return value;
}

static void rx_status_push(DmUsbDwc2Device *d, uint32_t status)
{
    d->rx_status[(d->rx_status_head + d->rx_status_count++) % 64] = status;
}

static bool rx_admit(DmUsbDwc2Device *d, size_t length, unsigned statuses)
{
    return d->rx_status_count + statuses <= 64 &&
           d->rx_count + ((length + 3) & ~3) +
           4 * (d->rx_status_count + statuses) <= fifo_capacity(d);
}

static void rx_push(DmUsbDwc2Device *d, unsigned ep, const uint8_t *data,
                     size_t length, bool setup, bool complete)
{
    uint8_t padding[3] = {0};
    fifo_push(d->rx_data, &d->rx_head, &d->rx_count, data, length);
    fifo_push(d->rx_data, &d->rx_head, &d->rx_count, padding, (-length) & 3);
    rx_status_push(d, ep | (length << 4) | ((setup ? 6 : 2) << 17));
    if (complete) {
        rx_status_push(d, ep | ((setup ? 4 : 3) << 17));
    }
}

static uint32_t device_daint(const DmUsbDwc2Device *device)
{
    uint32_t daint = 0;

    for (unsigned ep = 0; ep < DM_USB_DWC2_MAX_ENDPOINTS; ++ep) {
        if (in_interrupt(device, ep) &
            (device->diepmsk | ((device->diepempmsk & (1u << ep)) ? 128 : 0))) {
            daint |= 1u << ep;
        }
        if (device->endpoint[ep].out_int & device->doepmsk) {
            daint |= 1u << (16 + ep);
        }
    }
    return daint;
}

static uint32_t device_gintsts(const DmUsbDwc2Device *device)
{
    uint32_t status = device->gintsts;
    bool in_pending = false;
    bool out_pending = false;

    for (unsigned ep = 0; ep < DM_USB_DWC2_MAX_ENDPOINTS; ++ep) {
        if ((in_interrupt(device, ep) & (device->diepmsk |
             ((device->diepempmsk & (1u << ep)) ? 128 : 0))) &&
            (device->daintmsk & (1u << ep))) {
            in_pending = true;
        }
        if ((device->endpoint[ep].out_int & device->doepmsk) &&
            (device->daintmsk & (1u << (16 + ep)))) {
            out_pending = true;
        }
    }
    if (in_pending) {
        status |= DM_USB_DWC2_GINTSTS_IEPINT;
    } else {
        status &= ~DM_USB_DWC2_GINTSTS_IEPINT;
    }
    if (out_pending) {
        status |= DM_USB_DWC2_GINTSTS_OEPINT;
    } else {
        status &= ~DM_USB_DWC2_GINTSTS_OEPINT;
    }
    if (!device->control && device->rx_status_count) {
        status |= DM_USB_DWC2_GINTSTS_RXFLVL;
    }
    return status;
}

static void device_update_irq(DmUsbDwc2Device *device)
{
    bool level = (device_gintsts(device) & device->gintmsk) &&
                 (device->gahbcfg & DM_USB_DWC2_GAHBCFG_GINT);

    if (device->notify) {
        device->notify(device->notify_opaque);
    }
    if (level == device->irq_level) {
        return;
    }
    device->irq_level = level;
    if (device->irq) {
        device->irq(device->irq_opaque, level);
    }
}

static void endpoint_set_transfer_complete(DmUsbDwc2Device *device,
                                            unsigned ep,
                                            bool in)
{
    DmUsbDwc2Endpoint *endpoint = &device->endpoint[ep];

    if (in) {
        endpoint->in_tsiz &= ~DM_USB_DXEPTSIZ_PKTCNT_MASK;
        endpoint->in_tsiz &= ~DM_USB_DXEPTSIZ_XFERSIZE_MASK;
        endpoint->in_ctl &= ~DM_USB_DWC2_DXEPCTL_EPENA;
        endpoint->in_int |= DM_USB_DXEPINT_XFRC;
    } else {
        endpoint->out_tsiz &= ~DM_USB_DXEPTSIZ_PKTCNT_MASK;
        if (device->control) {
            endpoint->out_tsiz &= ~DM_USB_DXEPTSIZ_XFERSIZE_MASK;
        }
        endpoint->out_ctl &= ~DM_USB_DWC2_DXEPCTL_EPENA;
        endpoint->out_int |= DM_USB_DXEPINT_XFRC;
    }
}

static void endpoint_advance(DmUsbDwc2Device *device, unsigned ep,
                             bool in, size_t length)
{
    DmUsbDwc2Endpoint *endpoint = &device->endpoint[ep];
    uint32_t *tsiz = in ? &endpoint->in_tsiz : &endpoint->out_tsiz;
    uint32_t transfer_size = *tsiz & DM_USB_DXEPTSIZ_XFERSIZE_MASK;
    uint32_t packet_count = (*tsiz & DM_USB_DXEPTSIZ_PKTCNT_MASK) >>
                            DM_USB_DXEPTSIZ_PKTCNT_SHIFT;

    transfer_size -= min_u32(transfer_size, length);
    if (packet_count) {
        --packet_count;
    }
    *tsiz = (*tsiz & ~(DM_USB_DXEPTSIZ_PKTCNT_MASK |
                       DM_USB_DXEPTSIZ_XFERSIZE_MASK)) |
            (packet_count << DM_USB_DXEPTSIZ_PKTCNT_SHIFT) |
            transfer_size;
    if (!packet_count) {
        endpoint_set_transfer_complete(device, ep, in);
    }
}

static DmUsbTransactionStatus dwc2_transaction_in(
    void *opaque, uint8_t ep, uint8_t *data, size_t capacity,
    size_t *length, uint64_t timestamp_ns)
{
    DmUsbDwc2Device *device = opaque;
    DmUsbDwc2Endpoint *endpoint, *fifo;
    uint16_t mps;
    size_t packet_length;

    (void)timestamp_ns;
    if (ep >= DM_USB_DWC2_MAX_ENDPOINTS) {
        return DM_USB_TRANSACTION_INVALID;
    }
    endpoint = &device->endpoint[ep];
    fifo = &device->endpoint[tx_fifo(device, ep)];
    if (endpoint->in_ctl & DM_USB_DWC2_DXEPCTL_STALL) {
        return DM_USB_TRANSACTION_STALL;
    }
    if ((ep && !(endpoint->in_ctl & DM_USB_DWC2_DXEPCTL_USBAEP)) ||
        !(endpoint->in_ctl & DM_USB_DWC2_DXEPCTL_EPENA) ||
        (!device->control && (endpoint->in_ctl & (1u << 17)))) {
        return DM_USB_TRANSACTION_NAK;
    }
    mps = dm_usb_dwc2_max_packet(device, ep, true);
    if (!mps || !(endpoint->in_tsiz & DM_USB_DXEPTSIZ_PKTCNT_MASK)) {
        return DM_USB_TRANSACTION_NAK;
    }
    packet_length = min_size(mps, endpoint->in_tsiz &
                                      DM_USB_DXEPTSIZ_XFERSIZE_MASK);
    if (packet_length > capacity) {
        return DM_USB_TRANSACTION_INVALID;
    }
    if (fifo->in_fifo_count < packet_length) {
        return DM_USB_TRANSACTION_NAK;
    }
    fifo_pop(fifo->in_fifo, &fifo->in_fifo_head,
             &fifo->in_fifo_count, data, packet_length);
    *length = packet_length;
    endpoint_advance(device, ep, true, packet_length);
    if (!device->control && !(endpoint->in_ctl & DM_USB_DWC2_DXEPCTL_EPENA)) {
        fifo->in_fifo_head = fifo->in_fifo_count = 0;
    }
    device_update_irq(device);
    return DM_USB_TRANSACTION_ACCEPTED;
}

static DmUsbTransactionStatus dwc2_transaction_out(
    void *opaque, uint8_t ep, const uint8_t *data, size_t length,
    uint64_t timestamp_ns)
{
    DmUsbDwc2Device *device = opaque;
    DmUsbDwc2Endpoint *endpoint;
    uint16_t mps;
    size_t accepted;

    if (ep >= DM_USB_DWC2_MAX_ENDPOINTS) {
        return DM_USB_TRANSACTION_INVALID;
    }
    endpoint = &device->endpoint[ep];
    if (endpoint->out_ctl & DM_USB_DWC2_DXEPCTL_STALL) {
        return DM_USB_TRANSACTION_STALL;
    }
    if ((ep && !(endpoint->out_ctl & DM_USB_DWC2_DXEPCTL_USBAEP)) ||
        !(endpoint->out_ctl & DM_USB_DWC2_DXEPCTL_EPENA) ||
        (!device->control && (endpoint->out_ctl & (1u << 17)))) {
        return DM_USB_TRANSACTION_NAK;
    }
    mps = dm_usb_dwc2_max_packet(device, ep, false);
    if (!mps || !(endpoint->out_tsiz & DM_USB_DXEPTSIZ_PKTCNT_MASK) ||
        length > mps || length >
        (endpoint->out_tsiz & DM_USB_DXEPTSIZ_XFERSIZE_MASK)) {
        return DM_USB_TRANSACTION_INVALID;
    }
    if (!device->control) {
        bool complete = length < mps ||
            (endpoint->out_tsiz & DM_USB_DXEPTSIZ_PKTCNT_MASK) == (1u << 19);
        if (!rx_admit(device, length, complete ? 2 : 1)) {
            return DM_USB_TRANSACTION_NAK;
        }
        endpoint_advance(device, ep, false, length);
        if (complete) {
            endpoint->out_ctl &= ~DM_USB_DWC2_DXEPCTL_EPENA;
        }
        endpoint->out_int &= ~DM_USB_DXEPINT_XFRC;
        rx_push(device, ep, data, length, false, complete);
        device_update_irq(device);
        return DM_USB_TRANSACTION_ACCEPTED;
    }
    if (length > fifo_capacity(device) ||
        length > fifo_free(endpoint->out_fifo_count)) {
        ++device->fifo_overflow;
        return DM_USB_TRANSACTION_NAK;
    }
    accepted = fifo_push(endpoint->out_fifo, &endpoint->out_fifo_head,
                         &endpoint->out_fifo_count, data, length);
    if (accepted != length) {
        ++device->fifo_overflow;
        return DM_USB_TRANSACTION_NAK;
    }
    endpoint_advance(device, ep, false, length);
    device_update_irq(device);
    return DM_USB_TRANSACTION_ACCEPTED;
}

static const DmUsbTransactionOps dwc2_transaction_ops = {
    .in = dwc2_transaction_in,
    .out = dwc2_transaction_out,
};

static void device_sync_endpoint(DmUsbDwc2Device *device, unsigned ep,
                                 bool in)
{
    DmUsbDwc2Endpoint *endpoint = &device->endpoint[ep];
    uint32_t control = in ? endpoint->in_ctl : endpoint->out_ctl;
    DmUsbEndpointDirection direction = in ? DM_USB_ENDPOINT_IN :
                                               DM_USB_ENDPOINT_OUT;
    uint16_t mps = endpoint_mps(control, ep, device->ep0_max_packet_size);

    if (!mps) {
        mps = device->ep0_max_packet_size;
    }
    dm_usb_transaction_configure_endpoint(
        &device->transaction, ep, direction, endpoint_type(control), mps,
        (control & DM_USB_DWC2_DXEPCTL_USBAEP) != 0);
    if (!(control & DM_USB_DWC2_DXEPCTL_STALL)) {
        dm_usb_transaction_clear_halt(&device->transaction, ep, direction);
    }
}

static void device_write_fifo(DmUsbDwc2Device *device, unsigned ep,
                              uint64_t value, unsigned size)
{
    uint8_t bytes[4];
    DmUsbDwc2Endpoint *endpoint = &device->endpoint[ep];

    if (!device->control && endpoint->in_fifo_count + size > tx_capacity(device, ep)) {
        ++device->fifo_overflow;
        return;
    }
    for (unsigned i = 0; i < size; ++i) {
        bytes[i] = value >> (i * 8);
    }
    if (fifo_push(endpoint->in_fifo, &endpoint->in_fifo_head,
                  &endpoint->in_fifo_count, bytes, size) != size) {
        ++device->fifo_overflow;
    }
    device_update_irq(device);
}

static uint32_t device_read_fifo(DmUsbDwc2Device *device, unsigned ep,
                                 unsigned size)
{
    uint8_t bytes[4] = { 0 };
    DmUsbDwc2Endpoint *endpoint = &device->endpoint[ep];

    if (!device->control) {
        fifo_pop(device->rx_data, &device->rx_head, &device->rx_count, bytes, size);
        device_update_irq(device);
    } else {
        fifo_pop(endpoint->out_fifo, &endpoint->out_fifo_head,
                 &endpoint->out_fifo_count, bytes, size);
    }
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

void dm_usb_dwc2_init(DmUsbDwc2Device *device,
                      DmUsbControlDevice *control,
                      DmUsbDwc2Irq *irq, void *irq_opaque,
                      uint16_t ep0_max_packet_size, unsigned endpoint_count)
{
    if (!device) {
        return;
    }
    memset(device, 0, sizeof(*device));
    device->control = control;
    device->endpoint_count = endpoint_count;
    device->irq = irq;
    device->irq_opaque = irq_opaque;
    device->ep0_max_packet_size = ep0_max_packet_size ? ep0_max_packet_size : 64;
    dm_usb_transaction_init(&device->transaction, control,
                            &dwc2_transaction_ops, device,
                            device->ep0_max_packet_size);
    dm_usb_dwc2_reset(device);
}

void dm_usb_dwc2_reset(DmUsbDwc2Device *device)
{
    DmUsbDwc2Irq *irq;
    void *irq_opaque;
    DmUsbControlDevice *control;
    uint16_t ep0_max_packet_size;
    unsigned endpoint_count;
    bool was_irq_level;
    DmUsbDwc2Notify *notify;
    void *notify_opaque;

    if (!device) {
        return;
    }
    irq = device->irq;
    irq_opaque = device->irq_opaque;
    control = device->control;
    ep0_max_packet_size = device->ep0_max_packet_size;
    was_irq_level = device->irq_level;
    endpoint_count = device->endpoint_count;
    notify = device->notify;
    notify_opaque = device->notify_opaque;
    memset(device, 0, sizeof(*device));
    device->irq = irq;
    device->irq_opaque = irq_opaque;
    device->endpoint_count = endpoint_count;
    device->notify = notify;
    device->notify_opaque = notify_opaque;
    device->control = control;
    device->ep0_max_packet_size = ep0_max_packet_size ? ep0_max_packet_size : 64;
    device->dctl = control ? 0 : 2; /* Guest releases soft-disconnect. */
    device->grstctl = DM_USB_DWC2_GRSTCTL_AHBIDL;
    device->grxfsiz = DM_USB_DWC2_FIFO_BYTES / 4;
    device->gnptxfsiz = (DM_USB_DWC2_FIFO_BYTES / 4) << 16;
    for (unsigned i = 0; i < DM_USB_DWC2_DIEPTXF_COUNT; ++i) {
        /* Synthesized reset sizing maxima for dynamic FIFO discovery. These
         * are not simultaneously usable partitions or a silicon reset dump;
         * tx_capacity admits only a non-overlapping layout inside 4 KiB. */
        device->dieptxf[i] = (DM_USB_DWC2_FIFO_BYTES / 4) << 16;
    }
    dm_usb_transaction_init(&device->transaction, control,
                            &dwc2_transaction_ops, device,
                            device->ep0_max_packet_size);
    if (was_irq_level && device->irq) {
        device->irq(device->irq_opaque, false);
    }
}

void dm_usb_dwc2_bus_reset(DmUsbDwc2Device *device, uint64_t timestamp_ns)
{
    if (!device) {
        return;
    }
    (void)timestamp_ns;
    if (device->control) {
        dm_usb_control_bus_reset(device->control);
    }
    dm_usb_transaction_reset(&device->transaction);
    device->dcfg &= ~DM_USB_DWC2_DCFG_DAD_MASK;
    device->gintsts = device->control ? 0 : (1u << 12) | (1u << 13);
    device->dsts = 3u << 1; /* Full speed, internal FS PHY. */
    device->rx_status_head = device->rx_status_count = 0;
    device->rx_head = device->rx_count = 0;
    for (unsigned ep = 0; ep < DM_USB_DWC2_MAX_ENDPOINTS; ++ep) {
        DmUsbDwc2Endpoint *endpoint = &device->endpoint[ep];

        /* Preserve programmed endpoint properties, but cancel bus transfers. */
        endpoint->in_ctl &= ~(DM_USB_DWC2_DXEPCTL_EPENA |
                              DM_USB_DWC2_DXEPCTL_STALL);
        endpoint->out_ctl &= ~(DM_USB_DWC2_DXEPCTL_EPENA |
                               DM_USB_DWC2_DXEPCTL_STALL);
        endpoint->in_int = 0;
        endpoint->out_int = 0;
        endpoint->in_tsiz = 0;
        endpoint->out_tsiz = 0;
        endpoint->in_fifo_head = 0;
        endpoint->in_fifo_count = 0;
        endpoint->out_fifo_head = 0;
        endpoint->out_fifo_count = 0;
    }
    device_update_irq(device);
}

void dm_usb_dwc2_sync_runtime(DmUsbDwc2Device *device)
{
    bool was_irq_level;

    if (!device) {
        return;
    }

    /* These fields describe the destination's callback graph, not the
     * guest-visible DWC2 state.  A component restore deliberately leaves
     * them untouched, so rebind them here before any future token arrives. */
    device->transaction.control = device->control;
    device->transaction.ops = dwc2_transaction_ops;
    device->transaction.opaque = device;

    /* irq_level is derived from the loaded masks, status and endpoint bits.
     * Re-project it even when the destination happened to have an asserted
     * level before loading. */
    was_irq_level = device->irq_level;
    device->irq_level = false;
    if (was_irq_level && device->irq) {
        device->irq(device->irq_opaque, false);
    }
    device_update_irq(device);
}

DmUsbTransactionResult dm_usb_dwc2_submit(
    DmUsbDwc2Device *device, const DmUsbTransaction *transaction)
{
    DmUsbTransactionResult result;

    if (!device) {
        return (DmUsbTransactionResult) {
            .status = DM_USB_TRANSACTION_INVALID,
        };
    }
    if (!device->control) {
        size_t length = 0;
        if (!transaction || transaction->endpoint >= device->endpoint_count ||
            (transaction->length && !transaction->out_data) ||
            (transaction->capacity && !transaction->in_data)) {
            return (DmUsbTransactionResult){ .status = DM_USB_TRANSACTION_INVALID };
        }
        if (transaction->token == DM_USB_TRANSACTION_SETUP) {
            DmUsbDwc2Endpoint *ep = &device->endpoint[0];
            if (transaction->endpoint || transaction->length != 8) {
                return (DmUsbTransactionResult){ .status = DM_USB_TRANSACTION_INVALID };
            }
            if (!(ep->out_tsiz & (3u << 29)) || !rx_admit(device, 8, 2)) {
                return (DmUsbTransactionResult){ .status = DM_USB_TRANSACTION_NAK };
            }
            ep->in_ctl &= ~(DM_USB_DWC2_DXEPCTL_STALL | DM_USB_DWC2_DXEPCTL_EPENA);
            ep->out_ctl &= ~DM_USB_DWC2_DXEPCTL_STALL;
            ep->in_fifo_count = ep->in_fifo_head = 0;
            rx_push(device, 0, transaction->out_data, 8, true, true);
            device_update_irq(device);
            return (DmUsbTransactionResult){ .status = DM_USB_TRANSACTION_ACCEPTED,
                                             .actual_length = 8 };
        }
        if (transaction->token == DM_USB_TRANSACTION_IN) {
            result.status = dwc2_transaction_in(device, transaction->endpoint,
                transaction->in_data, transaction->capacity, &length, transaction->timestamp_ns);
        } else if (transaction->token == DM_USB_TRANSACTION_OUT) {
            result.status = dwc2_transaction_out(device, transaction->endpoint,
                transaction->out_data, transaction->length, transaction->timestamp_ns);
            length = transaction->length;
        } else {
            result.status = DM_USB_TRANSACTION_INVALID;
        }
        result.actual_length = result.status == DM_USB_TRANSACTION_ACCEPTED ? length : 0;
        result.next_pid = DM_USB_TRANSACTION_PID_AUTO;
        return result;
    }
    result = dm_usb_transaction_submit(&device->transaction, transaction);
    if (result.status == DM_USB_TRANSACTION_ACCEPTED &&
        transaction->endpoint == 0) {
        if (transaction->token == DM_USB_TRANSACTION_SETUP) {
            device->endpoint[0].out_int |= DM_USB_DXEPINT_STUP;
        } else if (transaction->token == DM_USB_TRANSACTION_IN) {
            device->endpoint[0].in_int |= DM_USB_DXEPINT_XFRC;
        } else {
            device->endpoint[0].out_int |= DM_USB_DXEPINT_XFRC;
        }
        device_update_irq(device);
    }
    return result;
}

bool dm_usb_dwc2_endpoint_fifo_count(const DmUsbDwc2Device *device,
                                     unsigned endpoint, bool in,
                                     size_t *count)
{
    if (!device || endpoint >= DM_USB_DWC2_MAX_ENDPOINTS || !count) {
        return false;
    }
    *count = in ? device->endpoint[endpoint].in_fifo_count :
                  device->endpoint[endpoint].out_fifo_count;
    return true;
}

uint64_t dm_usb_dwc2_read(DmUsbDwc2Device *device,
                          uint32_t offset, unsigned size)
{
    unsigned ep;

    if (!device || !access_size_valid(size)) {
        return 0;
    }
    ep = fifo_endpoint(offset);
    if (fifo_offset_valid(offset, size, ep)) {
        return device_read_fifo(device, ep, size);
    }
    if (offset >= DM_USB_DWC2_DIEPCTL0 &&
        offset < DM_USB_DWC2_DIEPCTL0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DIEPCTL0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DIEPCTL0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read(device->endpoint[ep].in_ctl, offset,
                            DM_USB_DWC2_DIEPCTL0 + ep * DM_USB_DWC2_EP_STRIDE,
                            size);
        }
    }
    if (offset >= DM_USB_DWC2_DIEPINT0 &&
        offset < DM_USB_DWC2_DIEPINT0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DIEPINT0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DIEPINT0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read(in_interrupt(device, ep), offset,
                            DM_USB_DWC2_DIEPINT0 + ep * DM_USB_DWC2_EP_STRIDE,
                            size);
        }
    }
    if (offset >= DM_USB_DWC2_DIEPTSIZ0 &&
        offset < DM_USB_DWC2_DIEPTSIZ0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DIEPTSIZ0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DIEPTSIZ0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read(device->endpoint[ep].in_tsiz, offset,
                            DM_USB_DWC2_DIEPTSIZ0 + ep * DM_USB_DWC2_EP_STRIDE,
                            size);
        }
    }
    if (offset >= DM_USB_DWC2_DIEPDMA0 &&
        offset < DM_USB_DWC2_DIEPDMA0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DIEPDMA0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DIEPDMA0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read(device->endpoint[ep].in_dma, offset,
                            DM_USB_DWC2_DIEPDMA0 + ep * DM_USB_DWC2_EP_STRIDE,
                            size);
        }
    }
    if (offset >= DM_USB_DWC2_DTXFSTS0 &&
        offset < DM_USB_DWC2_DTXFSTS0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DTXFSTS0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DTXFSTS0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read((tx_capacity(device, tx_fifo(device, ep)) -
                             min_size(tx_capacity(device, tx_fifo(device, ep)),
                                      device->endpoint[tx_fifo(device, ep)].in_fifo_count)) / 4,
                            offset, DM_USB_DWC2_DTXFSTS0 +
                            ep * DM_USB_DWC2_EP_STRIDE, size);
        }
    }
    if (offset >= DM_USB_DWC2_DOEPCTL0 &&
        offset < DM_USB_DWC2_DOEPCTL0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DOEPCTL0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DOEPCTL0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read(device->endpoint[ep].out_ctl, offset,
                            DM_USB_DWC2_DOEPCTL0 + ep * DM_USB_DWC2_EP_STRIDE,
                            size);
        }
    }
    if (offset >= DM_USB_DWC2_DOEPINT0 &&
        offset < DM_USB_DWC2_DOEPINT0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DOEPINT0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DOEPINT0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read(device->endpoint[ep].out_int, offset,
                            DM_USB_DWC2_DOEPINT0 + ep * DM_USB_DWC2_EP_STRIDE,
                            size);
        }
    }
    if (offset >= DM_USB_DWC2_DOEPTSIZ0 &&
        offset < DM_USB_DWC2_DOEPTSIZ0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DOEPTSIZ0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DOEPTSIZ0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read(device->endpoint[ep].out_tsiz, offset,
                            DM_USB_DWC2_DOEPTSIZ0 + ep * DM_USB_DWC2_EP_STRIDE,
                            size);
        }
    }
    if (offset >= DM_USB_DWC2_DOEPDMA0 &&
        offset < DM_USB_DWC2_DOEPDMA0 + DM_USB_DWC2_MAX_ENDPOINTS *
                 DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DOEPDMA0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DOEPDMA0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            return reg_read(device->endpoint[ep].out_dma, offset,
                            DM_USB_DWC2_DOEPDMA0 + ep * DM_USB_DWC2_EP_STRIDE,
                            size);
        }
    }
    {
        unsigned fifo;

        if (dieptxf_access_valid(offset, size, &fifo)) {
            return reg_read(device->dieptxf[fifo], offset,
                            DM_USB_DWC2_DIEPTXF1 +
                            fifo * DM_USB_DWC2_DIEPTXF_STRIDE, size);
        }
    }
    switch (offset) {
    case DM_USB_DWC2_GRXSTSR:
    case DM_USB_DWC2_GRXSTSP: {
        uint32_t status = device->rx_status_count ? device->rx_status[device->rx_status_head] : 0;
        if (offset == DM_USB_DWC2_GRXSTSP && device->rx_status_count) {
            unsigned type = (status >> 17) & 15;
            ep = status & 15;
            device->rx_status_head = (device->rx_status_head + 1) % 64;
            device->rx_status_count--;
            if (type == 4) {
                device->endpoint[ep].out_int |= DM_USB_DXEPINT_STUP;
            } else if (type == 3) {
                device->endpoint[ep].out_int |= DM_USB_DXEPINT_XFRC;
            }
            device_update_irq(device);
        }
        return status;
    }
    case DM_USB_DWC2_GAHBCFG: return reg_read(device->gahbcfg, offset, offset, size);
    case DM_USB_DWC2_GRSTCTL: return reg_read(device->grstctl | DM_USB_DWC2_GRSTCTL_AHBIDL, offset, offset, size);
    case DM_USB_DWC2_GINTSTS: return reg_read(device_gintsts(device), offset, offset, size);
    case DM_USB_DWC2_GINTMSK: return reg_read(device->gintmsk, offset, offset, size);
    case DM_USB_DWC2_GRXFSIZ: return reg_read(device->grxfsiz, offset, offset, size);
    case DM_USB_DWC2_GNPTXFSIZ: return reg_read(device->gnptxfsiz, offset, offset, size);
    case DM_USB_DWC2_GSNPSID: return reg_read(0x4f54420a, offset, offset, size);
    case DM_USB_DWC2_DCFG: return reg_read(device->dcfg, offset, offset, size);
    case DM_USB_DWC2_DCTL: return reg_read(device->dctl, offset, offset, size);
    case DM_USB_DWC2_DSTS: return reg_read(device->dsts, offset, offset, size);
    case DM_USB_DWC2_DIEPMSK: return reg_read(device->diepmsk, offset, offset, size);
    case DM_USB_DWC2_DOEPMSK: return reg_read(device->doepmsk, offset, offset, size);
    case DM_USB_DWC2_DAINT: return reg_read(device_daint(device), offset, offset, size);
    case DM_USB_DWC2_DAINTMSK: return reg_read(device->daintmsk, offset, offset, size);
    case DM_USB_DWC2_DIEPEMPMSK: return reg_read(device->diepempmsk, offset, offset, size);
    default: return offset < sizeof(device->global) && !(offset & 3) ?
                    device->global[offset / 4] : 0;
    }
}

void dm_usb_dwc2_write(DmUsbDwc2Device *device,
                       uint32_t offset, uint64_t value, unsigned size)
{
    unsigned ep;

    if (!device || !access_size_valid(size)) {
        return;
    }
    ep = fifo_endpoint(offset);
    if (fifo_offset_valid(offset, size, ep)) {
        device_write_fifo(device, ep, value, size);
        return;
    }
    if (offset >= DM_USB_DWC2_DIEPCTL0 &&
        offset < DM_USB_DWC2_DIEPCTL0 + DM_USB_DWC2_MAX_ENDPOINTS * DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DIEPCTL0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DIEPCTL0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            uint32_t old = device->endpoint[ep].in_ctl;
            device->endpoint[ep].in_ctl = reg_write(old, offset,
                DM_USB_DWC2_DIEPCTL0 + ep * DM_USB_DWC2_EP_STRIDE, value, size);
            if ((old & DM_USB_DWC2_DXEPCTL_STALL) &&
                !(device->endpoint[ep].in_ctl & DM_USB_DWC2_DXEPCTL_STALL)) {
                dm_usb_transaction_clear_halt(&device->transaction, ep,
                                               DM_USB_ENDPOINT_IN);
            }
            if (!device->control) {
                uint32_t *ctl = &device->endpoint[ep].in_ctl;
                if (*ctl & DM_USB_DWC2_DXEPCTL_EPDIS) {
                    *ctl &= ~(DM_USB_DWC2_DXEPCTL_EPENA | DM_USB_DWC2_DXEPCTL_EPDIS);
                    device->endpoint[ep].in_int |= 2;
                }
                if (*ctl & (1u << 27)) { *ctl |= 1u << 17; }
                if (*ctl & (1u << 26)) { *ctl &= ~(1u << 17); }
                *ctl &= ~((1u << 26) | (1u << 27));
            }
            device_sync_endpoint(device, ep, true);
            device_update_irq(device);
            return;
        }
    }
    if (offset >= DM_USB_DWC2_DIEPINT0 &&
        offset < DM_USB_DWC2_DIEPINT0 + DM_USB_DWC2_MAX_ENDPOINTS * DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DIEPINT0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DIEPINT0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            uint32_t mask = ((uint32_t)value & access_mask(size)) <<
                            ((offset - (DM_USB_DWC2_DIEPINT0 +
                                        ep * DM_USB_DWC2_EP_STRIDE)) * 8);
            device->endpoint[ep].in_int &= ~mask;
            device_update_irq(device);
            return;
        }
    }
    if (offset >= DM_USB_DWC2_DIEPTSIZ0 &&
        offset < DM_USB_DWC2_DIEPTSIZ0 + DM_USB_DWC2_MAX_ENDPOINTS * DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DIEPTSIZ0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DIEPTSIZ0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            device->endpoint[ep].in_tsiz = reg_write(device->endpoint[ep].in_tsiz,
                offset, DM_USB_DWC2_DIEPTSIZ0 + ep * DM_USB_DWC2_EP_STRIDE, value, size);
            device_update_irq(device);
            return;
        }
    }
    if (offset >= DM_USB_DWC2_DIEPDMA0 &&
        offset < DM_USB_DWC2_DIEPDMA0 + DM_USB_DWC2_MAX_ENDPOINTS * DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DIEPDMA0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DIEPDMA0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            device->endpoint[ep].in_dma = reg_write(device->endpoint[ep].in_dma,
                offset, DM_USB_DWC2_DIEPDMA0 + ep * DM_USB_DWC2_EP_STRIDE, value, size);
            device_update_irq(device);
            return;
        }
    }
    if (offset >= DM_USB_DWC2_DOEPCTL0 &&
        offset < DM_USB_DWC2_DOEPCTL0 + DM_USB_DWC2_MAX_ENDPOINTS * DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DOEPCTL0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DOEPCTL0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            uint32_t old = device->endpoint[ep].out_ctl;
            device->endpoint[ep].out_ctl = reg_write(old, offset,
                DM_USB_DWC2_DOEPCTL0 + ep * DM_USB_DWC2_EP_STRIDE, value, size);
            if ((old & DM_USB_DWC2_DXEPCTL_STALL) &&
                !(device->endpoint[ep].out_ctl & DM_USB_DWC2_DXEPCTL_STALL)) {
                dm_usb_transaction_clear_halt(&device->transaction, ep,
                                               DM_USB_ENDPOINT_OUT);
            }
            if (!device->control) {
                uint32_t *ctl = &device->endpoint[ep].out_ctl;
                if (*ctl & DM_USB_DWC2_DXEPCTL_EPDIS) {
                    *ctl &= ~(DM_USB_DWC2_DXEPCTL_EPENA | DM_USB_DWC2_DXEPCTL_EPDIS);
                    device->endpoint[ep].out_int |= 2;
                }
                if (*ctl & (1u << 27)) { *ctl |= 1u << 17; }
                if (*ctl & (1u << 26)) { *ctl &= ~(1u << 17); }
                *ctl &= ~((1u << 26) | (1u << 27));
            }
            device_sync_endpoint(device, ep, false);
            device_update_irq(device);
            return;
        }
    }
    if (offset >= DM_USB_DWC2_DOEPINT0 &&
        offset < DM_USB_DWC2_DOEPINT0 + DM_USB_DWC2_MAX_ENDPOINTS * DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DOEPINT0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DOEPINT0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            uint32_t mask = ((uint32_t)value & access_mask(size)) <<
                            ((offset - (DM_USB_DWC2_DOEPINT0 +
                                        ep * DM_USB_DWC2_EP_STRIDE)) * 8);
            device->endpoint[ep].out_int &= ~mask;
            device_update_irq(device);
            return;
        }
    }
    if (offset >= DM_USB_DWC2_DOEPTSIZ0 &&
        offset < DM_USB_DWC2_DOEPTSIZ0 + DM_USB_DWC2_MAX_ENDPOINTS * DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DOEPTSIZ0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DOEPTSIZ0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            device->endpoint[ep].out_tsiz = reg_write(device->endpoint[ep].out_tsiz,
                offset, DM_USB_DWC2_DOEPTSIZ0 + ep * DM_USB_DWC2_EP_STRIDE, value, size);
            device_update_irq(device);
            return;
        }
    }
    if (offset >= DM_USB_DWC2_DOEPDMA0 &&
        offset < DM_USB_DWC2_DOEPDMA0 + DM_USB_DWC2_MAX_ENDPOINTS * DM_USB_DWC2_EP_STRIDE) {
        ep = (offset - DM_USB_DWC2_DOEPDMA0) / DM_USB_DWC2_EP_STRIDE;
        if (register_access_valid(offset, size,
                                  DM_USB_DWC2_DOEPDMA0 +
                                  ep * DM_USB_DWC2_EP_STRIDE)) {
            device->endpoint[ep].out_dma = reg_write(device->endpoint[ep].out_dma,
                offset, DM_USB_DWC2_DOEPDMA0 + ep * DM_USB_DWC2_EP_STRIDE, value, size);
            device_update_irq(device);
            return;
        }
    }
    {
        unsigned fifo;

        if (dieptxf_access_valid(offset, size, &fifo)) {
            device->dieptxf[fifo] = reg_write(
                device->dieptxf[fifo], offset,
                DM_USB_DWC2_DIEPTXF1 + fifo * DM_USB_DWC2_DIEPTXF_STRIDE,
                value, size);
            device_update_irq(device);
            return;
        }
    }
    switch (offset) {
    case DM_USB_DWC2_GAHBCFG:
        device->gahbcfg = reg_write(device->gahbcfg, offset, offset, value, size);
        device_update_irq(device);
        break;
    case DM_USB_DWC2_GRSTCTL:
        if ((uint32_t)value & (1u << 4)) {
            device->rx_head = device->rx_count = 0;
            device->rx_status_head = device->rx_status_count = 0;
        }
        if ((uint32_t)value & (1u << 5)) {
            unsigned fifo = ((uint32_t)value >> 6) & 31;
            for (unsigned i = 0; i < DM_USB_DWC2_MAX_ENDPOINTS; i++) {
                if (fifo == 16 || fifo == i) {
                    device->endpoint[i].in_fifo_head = 0;
                    device->endpoint[i].in_fifo_count = 0;
                }
            }
        }
        if ((uint32_t)value & DM_USB_DWC2_GRSTCTL_CSFTRST) {
            dm_usb_dwc2_reset(device);
            /* The core reset request is synchronous in this model.  Keep
             * the completion indication until the next reset. */
            device->grstctl |= DM_USB_DWC2_GRSTCTL_CSFTRSTDONE;
        }
        break;
    case DM_USB_DWC2_GINTSTS:
        device->gintsts &= ~((uint32_t)value);
        device_update_irq(device);
        break;
    case DM_USB_DWC2_GINTMSK:
        device->gintmsk = reg_write(device->gintmsk, offset, offset, value, size);
        device_update_irq(device);
        break;
    case DM_USB_DWC2_GRXFSIZ:
        device->grxfsiz = reg_write(device->grxfsiz, offset, offset, value, size);
        break;
    case DM_USB_DWC2_GNPTXFSIZ:
        device->gnptxfsiz = reg_write(device->gnptxfsiz, offset, offset, value, size);
        break;
    case DM_USB_DWC2_DCFG:
        device->dcfg = reg_write(device->dcfg, offset, offset, value, size);
        break;
    case DM_USB_DWC2_DCTL:
        device->dctl = reg_write(device->dctl, offset, offset, value, size);
        break;
    case DM_USB_DWC2_DIEPMSK:
        device->diepmsk = reg_write(device->diepmsk, offset, offset, value, size);
        device_update_irq(device);
        break;
    case DM_USB_DWC2_DOEPMSK:
        device->doepmsk = reg_write(device->doepmsk, offset, offset, value, size);
        device_update_irq(device);
        break;
    case DM_USB_DWC2_DAINTMSK:
        device->daintmsk = reg_write(device->daintmsk, offset, offset, value, size);
        device_update_irq(device);
        break;
    case DM_USB_DWC2_DIEPEMPMSK:
        device->diepempmsk = reg_write(device->diepempmsk, offset, offset, value, size);
        break;
    default:
        if (offset < sizeof(device->global) && !(offset & 3)) {
            device->global[offset / 4] = value;
        }
        break;
    }
    device_update_irq(device);
}
