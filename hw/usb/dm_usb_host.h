/* Board-independent synthetic USB upstream host. */
#ifndef DM_USB_HOST_H
#define DM_USB_HOST_H

#include "hw/usb/dm_usb_transaction.h"

#include <stddef.h>
#include <stdint.h>

typedef DmUsbTransactionResult DmUsbHostSubmitTransaction(
    void *opaque, const DmUsbTransaction *transaction);

typedef struct DmUsbHostResult {
    DmUsbTransactionStatus status;
    size_t actual_length;
    unsigned transactions;
} DmUsbHostResult;

typedef struct DmUsbHost {
    DmUsbHostSubmitTransaction *submit;
    void *opaque;
    uint16_t ep0_max_packet_size;
} DmUsbHost;

/* A resumable transfer borrows its host and buffer. A pump submits at most
 * one packet. NAK preserves its stage/offset; cancel is a caller-owned removal
 * of the request and never rolls back packets already accepted by the device. */
typedef struct DmUsbHostRequest {
    DmUsbHost *host;
    uint8_t setup[8];
    uint8_t *data;
    size_t length, offset;
    unsigned stage, transactions;
    uint16_t max_packet;
    uint8_t endpoint;
    bool in, control, zero_packet;
    DmUsbTransactionStatus status;
} DmUsbHostRequest;

bool dm_usb_host_request_init(DmUsbHostRequest *request, DmUsbHost *host,
    uint8_t endpoint, bool in, uint16_t max_packet, const uint8_t *setup,
    uint8_t *data, size_t length, bool zero_packet);
DmUsbTransactionStatus dm_usb_host_request_pump(DmUsbHostRequest *request,
                                               uint64_t timestamp_ns);

void dm_usb_host_init(DmUsbHost *host,
                      DmUsbHostSubmitTransaction *submit,
                      void *opaque, uint16_t ep0_max_packet_size);

DmUsbHostResult dm_usb_host_control_transfer(
    DmUsbHost *host, const uint8_t setup[8], const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    uint64_t timestamp_ns);

DmUsbHostResult dm_usb_host_bulk_out(
    DmUsbHost *host, uint8_t endpoint, uint16_t max_packet_size,
    const uint8_t *data, size_t length, uint64_t timestamp_ns);

DmUsbHostResult dm_usb_host_bulk_in(
    DmUsbHost *host, uint8_t endpoint, uint16_t max_packet_size,
    uint8_t *data, size_t capacity, uint64_t timestamp_ns);

#endif /* DM_USB_HOST_H */
