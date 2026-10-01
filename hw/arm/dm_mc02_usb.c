/* Fast virtual USB CDC-style byte pipe over a QEMU chardev.  It is not a
 * physical USB PHY model, but provides a useful host/device data path while
 * keeping the DWC2 register compatibility window intact. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_usb.h"

#define USB_GRSTCTL 0x010
#define USB_GRXFSIZ 0x024
#define USB_GSNPSID 0x040
#define USB_GHWCFG1 0x044
#define USB_GHWCFG2 0x048
#define USB_GHWCFG3 0x04c
#define USB_GHWCFG4 0x050

#define USB_GRSTCTL_AHBIDL (1u << 31)
#define USB_GRSTCTL_CSRST (1u << 0)
#define USB_GRSTCTL_RXFFLSH (1u << 4)
#define USB_GRSTCTL_TXFFLSH (1u << 5)
#define USB_GRSTCTL_CSRSTDONE (1u << 29)
#define USB_GRSTCTL_TRANSIENT (USB_GRSTCTL_CSRST | USB_GRSTCTL_RXFFLSH | \
                               USB_GRSTCTL_TXFFLSH)
#define USB_GINTSTS 0x014
#define USB_GINTSTS_RXFLVL (1u << 4)
#define USB_FIFO0 0x1000

/* DWC2 device-mode register subset. */
#define USB_DCFG       0x800
#define USB_DCTL       0x804
#define USB_DSTS       0x808
#define USB_DIEPMSK    0x810
#define USB_DOEPMSK    0x814
#define USB_DAINT     0x818
#define USB_DAINTMSK  0x81c
#define USB_DIEPEMPMSK 0x834
#define USB_DIEPCTL0  0x900
#define USB_DIEPINT0  0x908
#define USB_DIEPTSIZ0 0x910
#define USB_DOEPCTL0  0xb00
#define USB_DOEPINT0  0xb08
#define USB_DOEPTSIZ0 0xb10
#define USB_EP_STRIDE 0x20
#define USB_FIFO_STRIDE 0x1000
#define USB_DIEPINT_XFRC (1u << 0)
#define USB_DOEPINT_XFRC (1u << 0)
#define USB_DIEPCTL_USBAEP (1u << 15)
#define USB_DIEPCTL_EPENA (1u << 31)
#define USB_DOEPCTL_EPENA (1u << 31)
#define USB_EP0_MPS 64
#define USB_BULK_MPS 64

#define CDC_REQ_SET_LINE_CODING       0x20
#define CDC_REQ_GET_LINE_CODING       0x21
#define CDC_REQ_SET_CONTROL_LINE_STATE 0x22

static void dm_mc02_usb_dwc2_irq(void *opaque, bool level)
{
    DmMc02Usb *s = opaque;

    if (s->irq) {
        qemu_set_irq(s->irq, level);
    }
}

/* These descriptors mirror trobot/bsp/src/usb.c: CherryUSB's device
 * descriptor, CDC_ACM_DESCRIPTOR_INIT and the WinUSB interface. The packet
 * hook below exposes them for protocol-acceptance tests only; this model does
 * not implement a USB PHY or bus. */
static const uint8_t usb_device_descriptor[] = {
    18, 1, 0x10, 0x02, 0xef, 0x02, 0x01, 64,
    0x83, 0x04, 0x41, 0x57, 0x01, 0x01, 1, 2, 3, 1,
};

static const uint8_t usb_config_descriptor[] = {
    /* 9 + CDC_ACM_DESCRIPTOR_LEN(66) + WinUSB(23) = 98 bytes. */
    9, 2, 0x62, 0x00, 3, 1, 0, 0x80, 50,
    8, 0x0b, 0, 2, 2, 2, 1, 0,
    9, 4, 0, 0, 1, 2, 2, 0, 5,
    5, 0x24, 0, 0x10, 0x01,
    5, 0x24, 1, 0, 1,
    4, 0x24, 2, 0x02,
    5, 0x24, 6, 0, 1,
    7, 5, 0x83, 3, 8, 0, 0x0a,
    9, 4, 1, 0, 2, 0x0a, 0, 0, 0,
    7, 5, 0x02, 2, 64, 0, 0,
    7, 5, 0x81, 2, 64, 0, 0,
    9, 4, 2, 0, 2, 0xff, 0, 0, 4,
    7, 5, 0x05, 2, 64, 0, 0,
    7, 5, 0x84, 2, 64, 0, 0,
};

static const uint8_t usb_string0_descriptor[] = { 4, 3, 0x09, 0x04 };
static const uint8_t usb_string1_descriptor[] = {
    14, 3, 't', 0, 'r', 0, 'o', 0, 'b', 0, 'o', 0, 't', 0,
};
static const uint8_t usb_string2_descriptor[] = {
    16, 3, 'd', 0, 'm', 0, '-', 0, 'm', 0, 'c', 0, '0', 0, '2', 0,
};
static const uint8_t usb_string3_descriptor[] = {
    10, 3, '0', 0, '0', 0, '0', 0, '1', 0,
};
static const uint8_t usb_string4_descriptor[] = {
    26, 3, 't', 0, 'r', 0, 'o', 0, 'b', 0, 'o', 0, 't', 0,
    ' ', 0, 'd', 0, 'e', 0, 'b', 0, 'u', 0, 'g', 0,
};
static const uint8_t usb_string5_descriptor[] = {
    22, 3, 't', 0, 'r', 0, 'o', 0, 'b', 0, 'o', 0, 't', 0,
    ' ', 0, 'c', 0, 'd', 0, 'c', 0,
};

static uint32_t usb_load(const DmMc02Usb *s, hwaddr offset, unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)s->regs[offset + i] << (i * 8);
    }
    return value;
}

static void usb_store(DmMc02Usb *s, hwaddr offset, uint32_t value,
                      unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        s->regs[offset + i] = value >> (i * 8);
    }
}

static void usb_clear_test_tx(DmMc02Usb *s)
{
    s->test_tx_len = 0;
    s->test_tx_pos = 0;
    s->test_tx_valid = false;
}

static void usb_publish_test_tx(DmMc02Usb *s, const uint8_t *data, size_t len)
{
    len = MIN(len, sizeof(s->test_tx));
    if (len) {
        memcpy(s->test_tx, data, len);
    }
    s->test_tx_len = len;
    s->test_tx_pos = 0;
    s->test_tx_valid = true;
}

static uint8_t usb_test_tx_pop(DmMc02Usb *s)
{
    if (!s->test_tx_valid || s->test_tx_pos == s->test_tx_len) {
        return 0;
    }
    return s->test_tx[s->test_tx_pos++];
}

static const uint8_t *usb_get_descriptor(uint8_t type, uint8_t index,
                                         size_t *len)
{
    if (index != 0 && type != 3) {
        return NULL;
    }
    switch (type) {
    case 1:
        *len = sizeof(usb_device_descriptor);
        return usb_device_descriptor;
    case 2:
        *len = sizeof(usb_config_descriptor);
        return usb_config_descriptor;
    case 3:
        switch (index) {
        case 0: *len = sizeof(usb_string0_descriptor); return usb_string0_descriptor;
        case 1: *len = sizeof(usb_string1_descriptor); return usb_string1_descriptor;
        case 2: *len = sizeof(usb_string2_descriptor); return usb_string2_descriptor;
        case 3: *len = sizeof(usb_string3_descriptor); return usb_string3_descriptor;
        case 4: *len = sizeof(usb_string4_descriptor); return usb_string4_descriptor;
        case 5: *len = sizeof(usb_string5_descriptor); return usb_string5_descriptor;
        default: return NULL;
        }
    default:
        return NULL;
    }
}

static bool usb_control_get_descriptor(void *opaque, uint8_t type,
                                       uint8_t index, const uint8_t **data,
                                       size_t *length)
{
    const uint8_t *descriptor = usb_get_descriptor(type, index, length);

    (void)opaque;
    if (!descriptor) {
        return false;
    }
    *data = descriptor;
    return true;
}

static DmUsbControlResult usb_control_class_request(
    void *opaque, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *in_length)
{
    DmMc02Usb *s = opaque;

    switch (request->request) {
    case CDC_REQ_GET_LINE_CODING:
        if (!(request->request_type & 0x80) || request->length != 7 ||
            out_length != 0 || !in_data || in_capacity < 7 || !in_length) {
            return DM_USB_CONTROL_STALL;
        }
        memcpy(in_data, s->line_coding, 7);
        *in_length = 7;
        return DM_USB_CONTROL_ACCEPTED;
    case CDC_REQ_SET_LINE_CODING:
        if ((request->request_type & 0x80) || request->length != 7) {
            return DM_USB_CONTROL_STALL;
        }
        if (out_length == 0) {
            return DM_USB_CONTROL_ACCEPTED;
        }
        if (!out_data || out_length != 7) {
            return DM_USB_CONTROL_STALL;
        }
        memcpy(s->line_coding, out_data, 7);
        return DM_USB_CONTROL_ACCEPTED;
    case CDC_REQ_SET_CONTROL_LINE_STATE:
        if ((request->request_type & 0x80) || request->length != 0 ||
            out_length != 0) {
            return DM_USB_CONTROL_STALL;
        }
        s->control_line_state = request->value;
        return DM_USB_CONTROL_ACCEPTED;
    default:
        return DM_USB_CONTROL_STALL;
    }
}

static void usb_control_set_address(void *opaque, uint8_t address)
{
    DmMc02Usb *s = opaque;

    s->address = address;
    dm_usb_dwc2_write(&s->dwc2, DM_USB_DWC2_DCFG,
                      (dm_usb_dwc2_read(&s->dwc2, DM_USB_DWC2_DCFG, 4) &
                       ~DM_USB_DWC2_DCFG_DAD_MASK) |
                      ((uint32_t)address << DM_USB_DWC2_DCFG_DAD_SHIFT),
                      sizeof(uint32_t));
}

static void usb_control_set_configuration(void *opaque, uint8_t configuration)
{
    DmMc02Usb *s = opaque;

    s->configuration = configuration;
    s->configured = configuration != 0;
}

static const DmUsbControlOps usb_control_ops = {
    .get_descriptor = usb_control_get_descriptor,
    .class_request = usb_control_class_request,
    .set_address = usb_control_set_address,
    .set_configuration = usb_control_set_configuration,
};

static void usb_arm_dwc2_endpoint(DmMc02Usb *s, unsigned ep, bool in,
                                  size_t length)
{
    uint32_t control = DM_USB_DWC2_DXEPCTL_USBAEP |
                       (2u << 18) | DM_MC02_USB_EP_PACKET_SIZE |
                       DM_USB_DWC2_DXEPCTL_EPENA;
    uint32_t transfer_size;
    unsigned packets;
    uint32_t ctl_base = in ? DM_USB_DWC2_DIEPCTL0 :
                             DM_USB_DWC2_DOEPCTL0;
    uint32_t tsiz_base = in ? DM_USB_DWC2_DIEPTSIZ0 :
                              DM_USB_DWC2_DOEPTSIZ0;

    length = MIN(length, (size_t)DM_USB_DXEPTSIZ_XFERSIZE_MASK);
    packets = length ? (length + DM_MC02_USB_EP_PACKET_SIZE - 1) /
                              DM_MC02_USB_EP_PACKET_SIZE : 1;
    packets = MIN(packets, 0x3ffu);
    transfer_size = (uint32_t)length |
                    ((uint32_t)packets << DM_USB_DXEPTSIZ_PKTCNT_SHIFT);
    dm_usb_dwc2_write(&s->dwc2,
                      ctl_base + ep * DM_USB_DWC2_EP_STRIDE,
                      control, sizeof(control));
    dm_usb_dwc2_write(&s->dwc2,
                      tsiz_base + ep * DM_USB_DWC2_EP_STRIDE,
                      transfer_size, sizeof(transfer_size));
}

static void usb_submit_test_packet(DmMc02Usb *s, uint32_t submit)
{
    unsigned ep = submit & DM_MC02_USB_TEST_EP_MASK;
    bool in = submit & DM_MC02_USB_TEST_DIR_IN;
    bool setup = submit & DM_MC02_USB_TEST_SETUP;

    if (ep >= DM_MC02_USB_EP_COUNT) {
        s->test_rx_pos = 0;
        s->test_rx_len = 0;
        return;
    }
    if (in) {
        uint8_t packet[USB_EP0_MPS];
        size_t pending;
        DmUsbTransaction transaction = {
            .token = DM_USB_TRANSACTION_IN,
            .pid = DM_USB_TRANSACTION_PID_AUTO,
            .endpoint = ep,
            .in_data = packet,
            .capacity = sizeof(packet),
            .timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
        };
        DmUsbTransactionResult result;

        if (ep != 0) {
            if (!dm_usb_dwc2_endpoint_fifo_count(&s->dwc2, ep, true,
                                                 &pending) || !pending) {
                usb_clear_test_tx(s);
                return;
            }
            usb_arm_dwc2_endpoint(s, ep, true, pending);
        }
        result = dm_usb_dwc2_submit(&s->dwc2, &transaction);

        s->ep0_stalled = ep == 0 &&
                          result.status == DM_USB_TRANSACTION_STALL;
        if (result.status == DM_USB_TRANSACTION_ACCEPTED) {
            usb_publish_test_tx(s, packet, result.actual_length);
        } else {
            usb_clear_test_tx(s);
        }
        return;
    }

    if (ep == 0) {
        DmUsbTransaction transaction = {
            .token = setup ? DM_USB_TRANSACTION_SETUP :
                             DM_USB_TRANSACTION_OUT,
            .pid = DM_USB_TRANSACTION_PID_AUTO,
            .endpoint = 0,
            .out_data = s->test_rx,
            .length = s->test_rx_len,
            .timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
        };
        DmUsbTransactionResult result = dm_usb_dwc2_submit(
            &s->dwc2, &transaction);

        s->ep0_stalled = result.status == DM_USB_TRANSACTION_STALL;
        s->test_rx_pos = 0;
        s->test_rx_len = 0;
        return;
    } else {
        size_t offset = 0;
        uint64_t timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

        while (offset < s->test_rx_len) {
            size_t packet_len = MIN((size_t)DM_MC02_USB_EP_PACKET_SIZE,
                                    s->test_rx_len - offset);
            DmUsbTransaction transaction = {
                .token = DM_USB_TRANSACTION_OUT,
                .pid = DM_USB_TRANSACTION_PID_AUTO,
                .endpoint = ep,
                .out_data = s->test_rx + offset,
                .length = packet_len,
                .timestamp_ns = timestamp_ns,
            };
            DmUsbTransactionResult result;

            usb_arm_dwc2_endpoint(s, ep, false, packet_len);
            result = dm_usb_dwc2_submit(&s->dwc2, &transaction);

            if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
                s->ep_rx_dropped_bytes += s->test_rx_len - offset;
                break;
            }
            offset += packet_len;
        }
        if (s->test_rx_len == 0) {
            DmUsbTransaction transaction = {
                .token = DM_USB_TRANSACTION_OUT,
                .pid = DM_USB_TRANSACTION_PID_AUTO,
                .endpoint = ep,
                .timestamp_ns = timestamp_ns,
            };
            DmUsbTransactionResult result;

            usb_arm_dwc2_endpoint(s, ep, false, 0);
            result = dm_usb_dwc2_submit(&s->dwc2, &transaction);
            (void)result;
        }
    }
    s->test_rx_pos = 0;
    s->test_rx_len = 0;
}

static uint32_t usb_test_status(const DmMc02Usb *s)
{
    size_t pending;

    return (uint32_t)s->address | ((uint32_t)s->configuration << 8) |
           (s->configured ? (1u << 16) : 0) |
           (s->test_tx_valid ? (1u << 17) : 0) |
           (dm_usb_dwc2_endpoint_fifo_count(&s->dwc2, 1, false, &pending) &&
            pending ? (1u << 18) : 0) |
           (s->ep0_stalled ? DM_MC02_USB_TEST_EP0_STALLED : 0);
}

static void usb_update_rx_status(DmMc02Usb *s)
{
    uint32_t status = usb_load(s, USB_GINTSTS, sizeof(uint32_t));

    if (s->rx_count) {
        status |= USB_GINTSTS_RXFLVL;
    } else {
        status &= ~USB_GINTSTS_RXFLVL;
    }
    usb_store(s, USB_GINTSTS, status, sizeof(uint32_t));
}

static void dm_mc02_usb_flush_tx(void *opaque)
{
    DmMc02Usb *s = opaque;
    size_t contiguous;
    int written;

    if (!s->enabled || !s->opened || !s->tx_count) {
        return;
    }
    contiguous = MIN(s->tx_count, sizeof(s->tx_fifo) - s->tx_head);
    written = qemu_chr_fe_write(&s->chr, s->tx_fifo + s->tx_head,
                                contiguous);
    if (written <= 0) {
        timer_mod(s->tx_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000 * 1000);
        return;
    }
    s->tx_head = (s->tx_head + written) % sizeof(s->tx_fifo);
    s->tx_count -= written;
    s->tx_bytes += written;
    if (s->tx_count) {
        timer_mod(s->tx_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000 * 1000);
    }
}

static void dm_mc02_usb_queue_tx(DmMc02Usb *s, const uint8_t *buf,
                                 size_t size)
{
    size_t room = sizeof(s->tx_fifo) - s->tx_count;
    size_t tail;
    size_t first;

    if (size > room) {
        s->tx_dropped += size - room;
        size = room;
    }
    tail = (s->tx_head + s->tx_count) % sizeof(s->tx_fifo);
    first = MIN(size, sizeof(s->tx_fifo) - tail);
    memcpy(s->tx_fifo + tail, buf, first);
    memcpy(s->tx_fifo, buf + first, size - first);
    s->tx_count += size;
    dm_mc02_usb_flush_tx(s);
}

static int dm_mc02_usb_can_receive(void *opaque)
{
    DmMc02Usb *s = opaque;

    return sizeof(s->rx_fifo) - s->rx_count;
}

static void dm_mc02_usb_receive(void *opaque, const uint8_t *buf, int size)
{
    DmMc02Usb *s = opaque;
    size_t room;
    size_t first;
    size_t tail;

    if (size <= 0) {
        return;
    }
    room = sizeof(s->rx_fifo) - s->rx_count;
    if ((size_t)size > room) {
        s->rx_dropped += (size_t)size - room;
        size = room;
    }
    tail = (s->rx_head + s->rx_count) % sizeof(s->rx_fifo);
    first = MIN((size_t)size, sizeof(s->rx_fifo) - tail);
    memcpy(s->rx_fifo + tail, buf, first);
    memcpy(s->rx_fifo, buf + first, (size_t)size - first);
    s->rx_count += size;
    s->rx_bytes += size;
    usb_update_rx_status(s);
    qemu_chr_fe_accept_input(&s->chr);
}

static void dm_mc02_usb_event(void *opaque, QEMUChrEvent event)
{
    DmMc02Usb *s = opaque;

    if (event == CHR_EVENT_OPENED) {
        s->opened = true;
        dm_mc02_usb_flush_tx(s);
    } else if (event == CHR_EVENT_CLOSED) {
        s->opened = false;
        s->rx_head = 0;
        s->rx_count = 0;
        s->tx_head = 0;
        s->tx_count = 0;
        if (s->tx_timer) {
            timer_del(s->tx_timer);
        }
        usb_update_rx_status(s);
    }
}

static void dm_mc02_usb_reset_registers(DmMc02Usb *s)
{
    memset(s->regs, 0, sizeof(s->regs));
    /* Power-on FIFO depths used by the board's DWC2 glue configuration. */
    usb_store(s, USB_GRXFSIZ, 1024, sizeof(uint32_t));
    usb_store(s, USB_GSNPSID, 0x4f54420a, sizeof(uint32_t));
    usb_store(s, USB_GHWCFG1, 0, sizeof(uint32_t));
    usb_store(s, USB_GHWCFG2,
              (1u << 19) | (8u << 10) | (1u << 8) | (2u << 3) | 4u,
              sizeof(uint32_t));
    usb_store(s, USB_GHWCFG3, 4096u << 16, sizeof(uint32_t));
    usb_store(s, USB_GHWCFG4, (8u << 26) | (1u << 16),
              sizeof(uint32_t));
    usb_store(s, 0x028, (1024u << 16), sizeof(uint32_t));
    usb_store(s, 0x100, (1024u << 16), sizeof(uint32_t));
    for (unsigned i = 0; i < 15; ++i) {
        usb_store(s, 0x104 + i * 4,
                  (1024u << 16) | (16u + i * 1024u), sizeof(uint32_t));
    }
    /* Device mode is the reset personality of the STM32 OTG HS block. */
    usb_store(s, USB_DCFG, 0, 4);
    usb_store(s, USB_DCTL, 0, 4);
    usb_store(s, USB_DSTS, 0, 4);
    /* Enable the endpoint interrupt summary for every endpoint advertised
     * by the board descriptors (EP0..EP5). */
    usb_store(s, USB_DAINTMSK, 0x003f003fu, 4);
    usb_store(s, USB_DIEPMSK, 1u, 4);
    usb_store(s, USB_DOEPMSK, 1u, 4);
    /* Keep the reusable DWC2 core as the source of standard register and
     * endpoint state.  The legacy register array above only retains the
     * hardware-configuration values and the raw EP0 compatibility pipe. */
    dm_usb_dwc2_write(&s->dwc2, DM_USB_DWC2_DAINTMSK,
                      0x003f003fu, sizeof(uint32_t));
    dm_usb_dwc2_write(&s->dwc2, DM_USB_DWC2_DIEPMSK,
                      1u, sizeof(uint32_t));
    dm_usb_dwc2_write(&s->dwc2, DM_USB_DWC2_DOEPMSK,
                      1u, sizeof(uint32_t));
}

static uint8_t usb_pop_byte(DmMc02Usb *s)
{
    uint8_t value = 0;

    if (s->rx_count) {
        value = s->rx_fifo[s->rx_head];
        s->rx_head = (s->rx_head + 1) % sizeof(s->rx_fifo);
        s->rx_count--;
        usb_update_rx_status(s);
    }
    return value;
}

static bool usb_fifo_endpoint(hwaddr offset, unsigned *ep)
{
    if (offset < USB_FIFO0 ||
        offset >= USB_FIFO0 + DM_MC02_USB_EP_COUNT * USB_FIFO_STRIDE) {
        return false;
    }
    *ep = (offset - USB_FIFO0) / USB_FIFO_STRIDE;
    return offset == USB_FIFO0 + *ep * USB_FIFO_STRIDE;
}

static uint64_t dm_mc02_usb_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Usb *s = opaque;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_USB_REGION_SIZE ||
        size > DM_MC02_USB_REGION_SIZE - offset) {
        return 0;
    }
    if (offset == DM_MC02_USB_TEST_RX_LEN && size == 4) {
        return s->test_rx_len;
    }
    if (offset == DM_MC02_USB_TEST_TX_LEN && size == 4) {
        return s->test_tx_valid ? s->test_tx_len -
               MIN(s->test_tx_pos, s->test_tx_len) : 0;
    }
    if (offset == DM_MC02_USB_TEST_STATUS && size == 4) {
        return usb_test_status(s);
    }
    if (offset == DM_MC02_USB_TEST_EP1_RX_COUNT && size == 4) {
        size_t pending;

        return dm_usb_dwc2_endpoint_fifo_count(&s->dwc2, 1, false, &pending) ?
               pending : 0;
    }
    if (offset == DM_MC02_USB_TEST_TX_DATA) {
        uint32_t value = 0;

        for (unsigned i = 0; i < size; ++i) {
            value |= (uint32_t)usb_test_tx_pop(s) << (i * 8);
        }
        if (s->test_tx_valid && s->test_tx_pos == s->test_tx_len) {
            usb_clear_test_tx(s);
        }
        return value;
    }
    if (offset == USB_FIFO0) {
        uint32_t value = 0;
        for (unsigned i = 0; i < size; ++i) {
            value |= (uint32_t)usb_pop_byte(s) << (i * 8);
        }
        return value;
    }
    if (offset == USB_GINTSTS) {
        uint32_t value = dm_usb_dwc2_read(&s->dwc2, offset, size);

        /* FIFO0 is a legacy raw byte pipe.  Its RXFLVL status remains owned
         * by the adapter and must be visible alongside core GINTSTS bits. */
        if (s->rx_count) {
            value |= USB_GINTSTS_RXFLVL;
        } else {
            value &= ~USB_GINTSTS_RXFLVL;
        }
        return value;
    }
    {
        unsigned ep;

        if (usb_fifo_endpoint(offset, &ep) && ep > 0 &&
            ep < DM_USB_DWC2_MAX_ENDPOINTS) {
            return dm_usb_dwc2_read(&s->dwc2, offset, size);
        }
    }
    if (offset >= USB_GHWCFG1 && offset <= USB_GHWCFG4) {
        return usb_load(s, offset, size);
    }
    return dm_usb_dwc2_read(&s->dwc2, offset, size);
}

static void dm_mc02_usb_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    DmMc02Usb *s = opaque;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_USB_REGION_SIZE ||
        size > DM_MC02_USB_REGION_SIZE - offset) {
        return;
    }
    if (offset == DM_MC02_USB_TEST_RX_LEN && size == 4) {
        s->test_rx_len = MIN((size_t)value, sizeof(s->test_rx));
        s->test_rx_pos = 0;
        return;
    }
    if (offset == DM_MC02_USB_TEST_RX_DATA) {
        for (unsigned i = 0; i < size &&
             s->test_rx_pos < sizeof(s->test_rx); ++i) {
            s->test_rx[s->test_rx_pos++] = value >> (i * 8);
        }
        s->test_rx_len = MAX(s->test_rx_len, s->test_rx_pos);
        return;
    }
    if (offset == DM_MC02_USB_TEST_RX_SUBMIT && size == 4) {
        usb_submit_test_packet(s, (uint32_t)value);
        return;
    }
    if (offset == USB_FIFO0) {
        uint8_t bytes[sizeof(uint32_t)];
        unsigned count = MIN(size, (unsigned)sizeof(bytes));

        for (unsigned i = 0; i < count; ++i) {
            bytes[i] = value >> (i * 8);
        }
        if (s->enabled && s->opened) {
            dm_mc02_usb_queue_tx(s, bytes, count);
        }
        return;
    }
    {
        unsigned ep;

        if (usb_fifo_endpoint(offset, &ep) && ep > 0 &&
            ep < DM_USB_DWC2_MAX_ENDPOINTS) {
            dm_usb_dwc2_write(&s->dwc2, offset, value, size);
            return;
        }
    }
    if (offset >= USB_GHWCFG1 && offset <= USB_GHWCFG4) {
        usb_store(s, offset, value, size);
        return;
    }
    if (offset == USB_GRSTCTL && size == sizeof(uint32_t) &&
        ((uint32_t)value & (USB_GRSTCTL_CSRST | USB_GRSTCTL_RXFFLSH))) {
        s->rx_head = 0;
        s->rx_count = 0;
    }
    if (offset == USB_GRSTCTL && size == sizeof(uint32_t) &&
        ((uint32_t)value & (USB_GRSTCTL_CSRST | USB_GRSTCTL_TXFFLSH))) {
        s->tx_head = 0;
        s->tx_count = 0;
        if (s->tx_timer) {
            timer_del(s->tx_timer);
        }
    }
    dm_usb_dwc2_write(&s->dwc2, offset, value, size);
}

static const MemoryRegionOps dm_mc02_usb_ops = {
    .read = dm_mc02_usb_read,
    .write = dm_mc02_usb_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    /* Keep an invalid unaligned transaction whole; otherwise QEMU's
     * AddressSpace layer may split it and let a cross-register tail reach
     * the next DWC2 register. */
    .impl.unaligned = true,
};

bool dm_mc02_usb_init(DmMc02Usb *state, Object *owner, Chardev *chardev,
                      Error **errp)
{
    memset(state, 0, sizeof(*state));
    state->line_coding[0] = 0x00;
    state->line_coding[1] = 0xc2;
    state->line_coding[2] = 0x01; /* 115200 baud, 8-N-1 */
    state->line_coding[3] = 0x00;
    state->line_coding[4] = 0;
    state->line_coding[5] = 0;
    state->line_coding[6] = 8;
    dm_usb_control_init(&state->control, &usb_control_ops, state,
                        USB_EP0_MPS);
    dm_usb_dwc2_init(&state->dwc2, &state->control,
                     dm_mc02_usb_dwc2_irq, state, USB_EP0_MPS);
    /* Power-on values must accommodate the board's DWC2 glue checks.  The
     * glue later repartitions this FIFO dynamically. */
    dm_mc02_usb_reset_registers(state);
    memory_region_init_io(&state->iomem, owner, &dm_mc02_usb_ops, state,
                          "dm-mc02.usb-otg-hs", DM_MC02_USB_REGION_SIZE);
    state->tx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                   dm_mc02_usb_flush_tx, state);
    if (!chardev) {
        return true;
    }
    if (!qemu_chr_fe_init(&state->chr, chardev, errp)) {
        dm_mc02_usb_cleanup(state);
        return false;
    }
    state->enabled = true;
    qemu_chr_fe_set_handlers(&state->chr, dm_mc02_usb_can_receive,
                             dm_mc02_usb_receive, dm_mc02_usb_event, NULL,
                             state, NULL, true);
    if (qemu_chr_fe_backend_open(&state->chr)) {
        dm_mc02_usb_event(state, CHR_EVENT_OPENED);
    }
    return true;
}

void dm_mc02_usb_reset(DmMc02Usb *state)
{
    if (!state) {
        return;
    }
    dm_usb_dwc2_reset(&state->dwc2);
    dm_mc02_usb_reset_registers(state);
    state->rx_head = 0;
    state->rx_count = 0;
    state->tx_head = 0;
    state->tx_count = 0;
    dm_usb_control_reset(&state->control);
    state->ep0_stalled = false;
    state->test_rx_len = 0;
    state->test_rx_pos = 0;
    state->test_tx_len = 0;
    state->test_tx_pos = 0;
    state->test_tx_valid = false;
    state->address = 0;
    state->configuration = 0;
    state->configured = false;
    memset(state->line_coding, 0, sizeof(state->line_coding));
    state->control_line_state = 0;
    usb_clear_test_tx(state);
    state->line_coding[0] = 0x00;
    state->line_coding[1] = 0xc2;
    state->line_coding[2] = 0x01;
    state->line_coding[3] = 0x00;
    state->line_coding[4] = 0;
    state->line_coding[5] = 0;
    state->line_coding[6] = 8;
    if (state->tx_timer) {
        timer_del(state->tx_timer);
    }
    usb_update_rx_status(state);
}

void dm_mc02_usb_set_irq(DmMc02Usb *state, qemu_irq irq)
{
    if (!state) {
        return;
    }
    state->irq = irq;
    if (state->irq) {
        qemu_set_irq(state->irq, state->dwc2.irq_level);
    }
}

void dm_mc02_usb_cleanup(DmMc02Usb *state)
{
    if (!state) {
        return;
    }
    if (state->enabled) {
        qemu_chr_fe_set_handlers(&state->chr, NULL, NULL, NULL, NULL, NULL,
                                 NULL, false);
        qemu_chr_fe_deinit(&state->chr, false);
    }
    if (state->tx_timer) {
        timer_free(state->tx_timer);
        state->tx_timer = NULL;
    }
    state->enabled = false;
    state->opened = false;
}
