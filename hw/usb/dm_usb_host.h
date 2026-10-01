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
