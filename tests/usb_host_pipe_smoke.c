#include "dm_usb_host_pipe.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

int main(void)
{
    DmUsbHostEndpoint endpoint = {
        .interface_number = 0,
        .alternate_setting = 0,
        .address = 0x81,
        .attributes = 3,
        .max_packet_size = 8,
        .interval = 10,
    };
    DmUsbHostPipe pipe = { .device_address = 99 };

    if (expect(dm_usb_host_pipe_from_endpoint(
                   &pipe, 5, &endpoint) == DM_USB_HOST_PIPE_OK,
               "build interrupt IN pipe") ||
        expect(pipe.device_address == 5 && pipe.endpoint_number == 1 &&
                   pipe.direction_in &&
                   pipe.transfer_type == DM_USB_HOST_PIPE_INTERRUPT &&
                   pipe.max_packet_size == 8 &&
                   pipe.transactions_per_microframe == 1 &&
                   pipe.interval == 10,
               "interrupt pipe fields")) {
        return 1;
    }

    endpoint.address = 2;
    endpoint.attributes = DM_USB_HOST_PIPE_ISOCHRONOUS;
    endpoint.max_packet_size = 0x1040;
    endpoint.interval = 4;
    if (expect(dm_usb_host_pipe_from_endpoint(
                   &pipe, 127, &endpoint) == DM_USB_HOST_PIPE_OK,
               "build high-speed isochronous pipe") ||
        expect(!pipe.direction_in &&
                   pipe.transfer_type == DM_USB_HOST_PIPE_ISOCHRONOUS &&
                   pipe.max_packet_size == 64 &&
                   pipe.transactions_per_microframe == 3 &&
                   pipe.interval == 4,
               "isochronous pipe fields")) {
        return 1;
    }

    endpoint.address = 0x91;
    if (expect(dm_usb_host_pipe_from_endpoint(
                   &pipe, 1, &endpoint) == DM_USB_HOST_PIPE_INVALID &&
                   pipe.device_address == 127,
               "reject reserved endpoint-address bits without mutation")) {
        return 1;
    }
    endpoint.address = 0;
    if (expect(dm_usb_host_pipe_from_endpoint(
                   &pipe, 1, &endpoint) == DM_USB_HOST_PIPE_INVALID,
               "reject endpoint zero descriptor")) {
        return 1;
    }
    endpoint.address = 2;
    endpoint.max_packet_size = 0;
    if (expect(dm_usb_host_pipe_from_endpoint(
                   &pipe, 1, &endpoint) == DM_USB_HOST_PIPE_INVALID,
               "reject zero packet size")) {
        return 1;
    }
    endpoint.max_packet_size = 0x2040;
    if (expect(dm_usb_host_pipe_from_endpoint(
                   &pipe, 1, &endpoint) == DM_USB_HOST_PIPE_INVALID,
               "reject reserved packet-size bits")) {
        return 1;
    }
    endpoint.max_packet_size = 64;
    endpoint.attributes = DM_USB_HOST_PIPE_BULK;
    endpoint.max_packet_size = 0x0840;
    if (expect(dm_usb_host_pipe_from_endpoint(
                   &pipe, 1, &endpoint) == DM_USB_HOST_PIPE_INVALID,
               "reject bulk transactions-per-microframe")) {
        return 1;
    }
    if (expect(dm_usb_host_pipe_from_endpoint(
                   &pipe, 128, &endpoint) == DM_USB_HOST_PIPE_INVALID,
               "reject out-of-range device address")) {
        return 1;
    }

    puts("RESULT: USB host endpoint pipe smoke passed");
    return 0;
}
