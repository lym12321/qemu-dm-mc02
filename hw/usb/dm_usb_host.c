/* Board-independent USB host transfers. */
#include "hw/usb/dm_usb_host.h"
#include <string.h>

void dm_usb_host_init(DmUsbHost *host, DmUsbHostSubmitTransaction *submit,
                      void *opaque, uint16_t ep0_max_packet_size)
{
    *host = (DmUsbHost){ .submit = submit, .opaque = opaque,
        .ep0_max_packet_size = ep0_max_packet_size ? ep0_max_packet_size : 64 };
}

bool dm_usb_host_request_init(DmUsbHostRequest *r, DmUsbHost *host,
    uint8_t endpoint, bool in, uint16_t max_packet, const uint8_t *setup,
    uint8_t *data, size_t length, bool zero_packet)
{
    if (!r || !host || !host->submit || !max_packet || endpoint > 15 ||
        (length && !data) || (setup && (endpoint ||
        length != (size_t)(setup[6] | ((unsigned)setup[7] << 8)) ||
        in != ((setup[0] & 128) != 0)))) {
        return false;
    }
    *r = (DmUsbHostRequest){ .host = host, .data = data, .length = length,
        .stage = setup ? 0 : 1, .max_packet = max_packet,
        .endpoint = endpoint, .in = in, .control = setup != NULL,
        .zero_packet = zero_packet && !in && length && length % max_packet == 0,
        .status = DM_USB_TRANSACTION_DEFERRED };
    if (setup) {
        memcpy(r->setup, setup, 8);
    }
    return true;
}

DmUsbTransactionStatus dm_usb_host_request_pump(DmUsbHostRequest *r,
                                               uint64_t timestamp_ns)
{
    DmUsbTransaction t = { .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = r->endpoint, .timestamp_ns = timestamp_ns };
    DmUsbTransactionResult result;
    size_t count = 0;
    bool in = r->in;
    if (r->stage == 3) {
        return r->status;
    }
    if (r->stage == 0) {
        t.token = DM_USB_TRANSACTION_SETUP;
        t.out_data = r->setup;
        t.length = 8;
    } else {
        if (r->stage == 2) {
            in = r->length ? !r->in : true;
        } else {
            count = r->length - r->offset;
            if (count > r->max_packet) { count = r->max_packet; }
        }
        t.token = in ? DM_USB_TRANSACTION_IN : DM_USB_TRANSACTION_OUT;
        if (in) {
            t.in_data = count ? r->data + r->offset : NULL;
            t.capacity = count;
        } else {
            t.out_data = count ? r->data + r->offset : NULL;
            t.length = count;
        }
    }
    result = r->host->submit(r->host->opaque, &t);
    r->transactions++;
    if (result.status == DM_USB_TRANSACTION_NAK) {
        return result.status;
    }
    if (result.status != DM_USB_TRANSACTION_ACCEPTED ||
        (r->stage != 0 && (result.actual_length > count ||
         (!in && result.actual_length != count)))) {
        r->stage = 3;
        r->status = result.status == DM_USB_TRANSACTION_ACCEPTED ?
                    DM_USB_TRANSACTION_INVALID : result.status;
        return r->status;
    }
    if (r->stage == 0) {
        r->stage = r->length ? 1 : 2;
    } else if (r->stage == 2) {
        r->stage = 3;
    } else {
        r->offset += result.actual_length;
        if (result.actual_length < r->max_packet || r->offset == r->length) {
            if (r->zero_packet) {
                r->zero_packet = false;
                return DM_USB_TRANSACTION_DEFERRED;
            }
            r->stage = r->control ? 2 : 3;
        }
    }
    if (r->stage == 3) {
        r->status = DM_USB_TRANSACTION_ACCEPTED;
    }
    return r->stage == 3 ? r->status : DM_USB_TRANSACTION_DEFERRED;
}

static DmUsbHostResult run(DmUsbHostRequest *r, uint64_t timestamp_ns)
{
    DmUsbTransactionStatus status;
    do {
        status = dm_usb_host_request_pump(r, timestamp_ns);
    } while (status == DM_USB_TRANSACTION_DEFERRED);
    return (DmUsbHostResult){ .status = status, .actual_length = r->offset,
                             .transactions = r->transactions };
}

DmUsbHostResult dm_usb_host_control_transfer(DmUsbHost *host,
    const uint8_t setup[8], const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, uint64_t timestamp_ns)
{
    DmUsbHostRequest r;
    bool in;
    size_t length;
    if (!setup) { return (DmUsbHostResult){ .status = DM_USB_TRANSACTION_INVALID }; }
    in = (setup[0] & 128) != 0;
    length = setup[6] | ((unsigned)setup[7] << 8);
    if ((in && (out_data || out_length || in_capacity < length)) ||
        (!in && (in_data || in_capacity || out_length != length)) ||
        !dm_usb_host_request_init(&r, host, 0, in,
            host ? host->ep0_max_packet_size : 0, setup,
            in ? in_data : (uint8_t *)out_data, length, false)) {
        return (DmUsbHostResult){ .status = DM_USB_TRANSACTION_INVALID };
    }
    return run(&r, timestamp_ns);
}

DmUsbHostResult dm_usb_host_bulk_out(DmUsbHost *host, uint8_t endpoint,
    uint16_t max_packet_size, const uint8_t *data, size_t length,
    uint64_t timestamp_ns)
{
    DmUsbHostRequest r;
    if (!dm_usb_host_request_init(&r, host, endpoint, false, max_packet_size,
        NULL, (uint8_t *)data, length, false)) {
        return (DmUsbHostResult){ .status = DM_USB_TRANSACTION_INVALID };
    }
    return run(&r, timestamp_ns);
}

DmUsbHostResult dm_usb_host_bulk_in(DmUsbHost *host, uint8_t endpoint,
    uint16_t max_packet_size, uint8_t *data, size_t capacity,
    uint64_t timestamp_ns)
{
    DmUsbHostRequest r;
    if (!dm_usb_host_request_init(&r, host, endpoint, true, max_packet_size,
        NULL, data, capacity, false)) {
        return (DmUsbHostResult){ .status = DM_USB_TRANSACTION_INVALID };
    }
    return run(&r, timestamp_ns);
}
