/* Board- and controller-independent USB endpoint pipe configuration. */
#include "dm_usb_host_pipe.h"

#define USB_ENDPOINT_NUMBER_MASK     0x0fu
#define USB_ENDPOINT_DIRECTION_IN    0x80u
#define USB_ENDPOINT_ADDRESS_MASK    0x8fu
#define USB_ENDPOINT_TRANSFER_MASK   0x03u
#define USB_ENDPOINT_MPS_MASK        0x07ffu
#define USB_ENDPOINT_TRANSACTIONS_SHIFT 11u
#define USB_ENDPOINT_RESERVED_MPS_MASK 0xe000u

DmUsbHostPipeResult dm_usb_host_pipe_from_endpoint(
    DmUsbHostPipe *pipe, uint8_t device_address,
    const DmUsbHostEndpoint *endpoint)
{
    uint8_t transfer_type;
    uint16_t max_packet_size;
    uint8_t transactions_per_microframe;

    if (!pipe || !endpoint || device_address > 127 ||
        (endpoint->address & ~USB_ENDPOINT_ADDRESS_MASK) ||
        !(endpoint->address & USB_ENDPOINT_NUMBER_MASK) ||
        (endpoint->max_packet_size & USB_ENDPOINT_RESERVED_MPS_MASK)) {
        return DM_USB_HOST_PIPE_INVALID;
    }

    transfer_type = endpoint->attributes & USB_ENDPOINT_TRANSFER_MASK;
    max_packet_size = endpoint->max_packet_size & USB_ENDPOINT_MPS_MASK;
    transactions_per_microframe =
        ((endpoint->max_packet_size >> USB_ENDPOINT_TRANSACTIONS_SHIFT) & 3u) +
        1u;
    if (!max_packet_size ||
        ((transfer_type == DM_USB_HOST_PIPE_CONTROL ||
          transfer_type == DM_USB_HOST_PIPE_BULK) &&
         transactions_per_microframe != 1)) {
        return DM_USB_HOST_PIPE_INVALID;
    }

    *pipe = (DmUsbHostPipe) {
        .device_address = device_address,
        .endpoint_number = endpoint->address & USB_ENDPOINT_NUMBER_MASK,
        .attributes = endpoint->attributes,
        .direction_in = endpoint->address & USB_ENDPOINT_DIRECTION_IN,
        .transfer_type = transfer_type,
        .max_packet_size = max_packet_size,
        .transactions_per_microframe = transactions_per_microframe,
        .interval = endpoint->interval,
    };
    return DM_USB_HOST_PIPE_OK;
}
