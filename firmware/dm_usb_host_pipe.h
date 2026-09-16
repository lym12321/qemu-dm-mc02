/* Board- and controller-independent USB endpoint pipe configuration. */
#ifndef DM_USB_HOST_PIPE_H
#define DM_USB_HOST_PIPE_H

#include "dm_usb_host_descriptor.h"

#include <stdbool.h>
#include <stdint.h>

typedef enum DmUsbHostPipeResult {
    DM_USB_HOST_PIPE_OK,
    DM_USB_HOST_PIPE_INVALID,
} DmUsbHostPipeResult;

typedef enum DmUsbHostPipeTransferType {
    DM_USB_HOST_PIPE_CONTROL = 0,
    DM_USB_HOST_PIPE_ISOCHRONOUS = 1,
    DM_USB_HOST_PIPE_BULK = 2,
    DM_USB_HOST_PIPE_INTERRUPT = 3,
} DmUsbHostPipeTransferType;

typedef struct DmUsbHostPipe {
    uint8_t device_address;
    uint8_t endpoint_number;
    uint8_t attributes;
    bool direction_in;
    DmUsbHostPipeTransferType transfer_type;
    uint16_t max_packet_size;
    uint8_t transactions_per_microframe;
    uint8_t interval;
} DmUsbHostPipe;

/* Build a runtime pipe from one non-control endpoint descriptor. */
DmUsbHostPipeResult dm_usb_host_pipe_from_endpoint(
    DmUsbHostPipe *pipe, uint8_t device_address,
    const DmUsbHostEndpoint *endpoint);

#endif /* DM_USB_HOST_PIPE_H */
