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

    if (offset < DM_USB_DWC2_DIEPTXF0) {
        return false;
    }
    relative = offset - DM_USB_DWC2_DIEPTXF0;
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

static uint32_t device_daint(const DmUsbDwc2Device *device)
{
    uint32_t daint = 0;

    for (unsigned ep = 0; ep < DM_USB_DWC2_MAX_ENDPOINTS; ++ep) {
        if (device->endpoint[ep].in_int) {
            daint |= 1u << ep;
        }
        if (device->endpoint[ep].out_int) {
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
        if ((device->endpoint[ep].in_int & device->diepmsk) &&
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
    return status;
}

static void device_update_irq(DmUsbDwc2Device *device)
{
    bool level = (device_gintsts(device) & device->gintmsk) &&
                 (device->gahbcfg & DM_USB_DWC2_GAHBCFG_GINT);

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
        endpoint->out_tsiz &= ~DM_USB_DXEPTSIZ_XFERSIZE_MASK;
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
    DmUsbDwc2Endpoint *endpoint;
    uint16_t mps;
    size_t packet_length;

    (void)timestamp_ns;
    if (ep >= DM_USB_DWC2_MAX_ENDPOINTS) {
        return DM_USB_TRANSACTION_INVALID;
    }
    endpoint = &device->endpoint[ep];
    if (endpoint->in_ctl & DM_USB_DWC2_DXEPCTL_STALL) {
        return DM_USB_TRANSACTION_STALL;
    }
    if (!(endpoint->in_ctl & DM_USB_DWC2_DXEPCTL_USBAEP) ||
        !(endpoint->in_ctl & DM_USB_DWC2_DXEPCTL_EPENA)) {
        return DM_USB_TRANSACTION_NAK;
    }
    mps = endpoint_mps(endpoint->in_ctl, ep, device->ep0_max_packet_size);
    if (!mps || !(endpoint->in_tsiz & DM_USB_DXEPTSIZ_PKTCNT_MASK)) {
        return DM_USB_TRANSACTION_NAK;
    }
    if (!endpoint->in_fifo_count) {
        if ((endpoint->in_tsiz & DM_USB_DXEPTSIZ_XFERSIZE_MASK) != 0) {
            return DM_USB_TRANSACTION_NAK;
        }
        *length = 0;
        endpoint_advance(device, ep, true, 0);
        device_update_irq(device);
        return DM_USB_TRANSACTION_ACCEPTED;
    }
    packet_length = min_size(endpoint->in_fifo_count, mps);
    packet_length = min_size(packet_length, capacity);
    packet_length = min_size(packet_length,
                             endpoint->in_tsiz &
                             DM_USB_DXEPTSIZ_XFERSIZE_MASK);
    if (!packet_length) {
        return DM_USB_TRANSACTION_INVALID;
    }
    fifo_pop(endpoint->in_fifo, &endpoint->in_fifo_head,
             &endpoint->in_fifo_count, data, packet_length);
    *length = packet_length;
    endpoint_advance(device, ep, true, packet_length);
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
    if (!(endpoint->out_ctl & DM_USB_DWC2_DXEPCTL_USBAEP) ||
        !(endpoint->out_ctl & DM_USB_DWC2_DXEPCTL_EPENA)) {
        return DM_USB_TRANSACTION_NAK;
    }
    mps = endpoint_mps(endpoint->out_ctl, ep, device->ep0_max_packet_size);
    if (!mps || !(endpoint->out_tsiz & DM_USB_DXEPTSIZ_PKTCNT_MASK) ||
        length > mps || length >
        (endpoint->out_tsiz & DM_USB_DXEPTSIZ_XFERSIZE_MASK)) {
        return DM_USB_TRANSACTION_INVALID;
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

    for (unsigned i = 0; i < size; ++i) {
        bytes[i] = value >> (i * 8);
    }
    if (fifo_push(endpoint->in_fifo, &endpoint->in_fifo_head,
                  &endpoint->in_fifo_count, bytes, size) != size) {
        ++device->fifo_overflow;
    }
}

static uint32_t device_read_fifo(DmUsbDwc2Device *device, unsigned ep,
                                 unsigned size)
{
    uint8_t bytes[4] = { 0 };
    DmUsbDwc2Endpoint *endpoint = &device->endpoint[ep];

    fifo_pop(endpoint->out_fifo, &endpoint->out_fifo_head,
             &endpoint->out_fifo_count, bytes, size);
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

void dm_usb_dwc2_init(DmUsbDwc2Device *device,
                      DmUsbControlDevice *control,
                      DmUsbDwc2Irq *irq, void *irq_opaque,
                      uint16_t ep0_max_packet_size)
{
    if (!device) {
        return;
    }
    memset(device, 0, sizeof(*device));
    device->control = control;
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
    bool was_irq_level;

    if (!device) {
        return;
    }
    irq = device->irq;
    irq_opaque = device->irq_opaque;
    control = device->control;
    ep0_max_packet_size = device->ep0_max_packet_size;
    was_irq_level = device->irq_level;
    memset(device, 0, sizeof(*device));
    device->irq = irq;
    device->irq_opaque = irq_opaque;
    device->control = control;
    device->ep0_max_packet_size = ep0_max_packet_size ? ep0_max_packet_size : 64;
    device->grstctl = DM_USB_DWC2_GRSTCTL_AHBIDL;
    device->grxfsiz = DM_USB_DWC2_FIFO_BYTES / 4;
    device->gnptxfsiz = (DM_USB_DWC2_FIFO_BYTES / 4) << 16;
    for (unsigned i = 0; i < DM_USB_DWC2_DIEPTXF_COUNT; ++i) {
        /* The power-on depth is a generous compatibility value.  Dynamic
         * FIFO sizing remains guest-owned and rewrites these registers. */
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
    device->gintsts = 0;
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
            return reg_read(device->endpoint[ep].in_int, offset,
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
            return reg_read((DM_USB_DWC2_FIFO_BYTES -
                             device->endpoint[ep].in_fifo_count) / 4,
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
                            DM_USB_DWC2_DIEPTXF0 +
                            fifo * DM_USB_DWC2_DIEPTXF_STRIDE, size);
        }
    }
    switch (offset) {
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
    default: return 0;
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
            return;
        }
    }
    {
        unsigned fifo;

        if (dieptxf_access_valid(offset, size, &fifo)) {
            device->dieptxf[fifo] = reg_write(
                device->dieptxf[fifo], offset,
                DM_USB_DWC2_DIEPTXF0 + fifo * DM_USB_DWC2_DIEPTXF_STRIDE,
                value, size);
            return;
        }
    }
    switch (offset) {
    case DM_USB_DWC2_GAHBCFG:
        device->gahbcfg = reg_write(device->gahbcfg, offset, offset, value, size);
        device_update_irq(device);
        break;
    case DM_USB_DWC2_GRSTCTL:
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
        break;
    }
}
