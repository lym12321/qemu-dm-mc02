#include "dm_stm32h7_usb_host_pipe.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define HCCHAR_DEVADDR_SHIFT 22u
#define HCCHAR_EPTYPE_SHIFT  18u
#define HCCHAR_EPDIR         (1u << 15)
#define HCCHAR_EPNUM_SHIFT   11u
#define HCTSIZ_PID_SHIFT     29u
#define HCTSIZ_PKT_SHIFT     19u

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
    uint8_t channels[] = { 2 };
    uint8_t invalid_channels[] = { 12 };
    DmUsbHostChannelAllocator allocator;
    DmUsbHostChannelLease lease;
    DmStm32H7UsbHostPipeClient client;
    DmUsbHostPipe pipe = {
        .device_address = 5,
        .endpoint_number = 1,
        .attributes = DM_USB_HOST_PIPE_INTERRUPT,
        .direction_in = true,
        .transfer_type = DM_USB_HOST_PIPE_INTERRUPT,
        .max_packet_size = 8,
        .transactions_per_microframe = 1,
        .interval = 10,
    };
    uint32_t hcchar = 0;
    uint32_t hctsiz = 0;
    size_t actual_length = 99;

    if (expect(dm_stm32h7_usb_host_pipe_encode(
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, 8,
                   &hcchar, &hctsiz) == DM_STM32H7_USB_HOST_PIPE_OK,
               "encode interrupt IN packet") ||
        expect(hcchar == ((5u << HCCHAR_DEVADDR_SHIFT) |
                          (3u << HCCHAR_EPTYPE_SHIFT) | HCCHAR_EPDIR |
                          (1u << HCCHAR_EPNUM_SHIFT) | 8u) &&
                   hctsiz == (8u | (1u << HCTSIZ_PKT_SHIFT)),
               "interrupt IN register fields")) {
        return 1;
    }

    pipe.direction_in = false;
    pipe.attributes = DM_USB_HOST_PIPE_BULK;
    pipe.transfer_type = DM_USB_HOST_PIPE_BULK;
    pipe.max_packet_size = 64;
    if (expect(dm_stm32h7_usb_host_pipe_encode(
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA1, 0,
                   &hcchar, &hctsiz) == DM_STM32H7_USB_HOST_PIPE_OK,
               "encode bulk OUT zero packet") ||
        expect(!(hcchar & HCCHAR_EPDIR) &&
                   hctsiz == ((1u << HCTSIZ_PKT_SHIFT) |
                               (2u << HCTSIZ_PID_SHIFT)),
               "bulk OUT register fields")) {
        return 1;
    }

    pipe.transactions_per_microframe = 2;
    if (expect(dm_stm32h7_usb_host_pipe_encode(
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, 1,
                   &hcchar, &hctsiz) == DM_STM32H7_USB_HOST_PIPE_INVALID,
               "reject multi-transaction packet") ||
        expect(dm_stm32h7_usb_host_pipe_encode(
                   &pipe, (DmStm32H7UsbHostPipePid)1, 1,
                   &hcchar, &hctsiz) == DM_STM32H7_USB_HOST_PIPE_INVALID,
               "reject unsupported PID")) {
        return 1;
    }
    pipe.transactions_per_microframe = 1;
    pipe.attributes = DM_USB_HOST_PIPE_INTERRUPT;
    if (expect(dm_stm32h7_usb_host_pipe_encode(
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, 1,
                   &hcchar, &hctsiz) == DM_STM32H7_USB_HOST_PIPE_INVALID,
               "reject mismatched attributes") ||
        expect(dm_stm32h7_usb_host_pipe_encode(
                   &pipe, DM_STM32H7_USB_HOST_PIPE_PID_DATA0, 65,
                   &hcchar, &hctsiz) == DM_STM32H7_USB_HOST_PIPE_INVALID,
               "reject packet larger than MPS")) {
        return 1;
    }

    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, invalid_channels, 1) &&
                   dm_usb_host_channel_allocator_acquire(
                       &allocator, &client, &lease) &&
                   !dm_stm32h7_usb_host_pipe_client_init(
                       &client, 0, &allocator, &lease) &&
                   dm_usb_host_channel_allocator_release(&allocator, &lease),
               "reject active lease outside H723 channel range") ||
        expect(dm_usb_host_channel_allocator_init(&allocator, channels, 1) &&
                   dm_usb_host_channel_allocator_acquire(
                       &allocator, &client, &lease) &&
                   dm_stm32h7_usb_host_pipe_client_init(
                       &client, 0, &allocator, &lease) &&
                   client.allocator == &allocator && client.lease == &lease &&
                   client.lease->channel == 2,
               "bind an active generic lease to H723 PIO client")) {
        return 1;
    }

    if (expect(dm_usb_host_channel_allocator_release(&allocator, &lease) &&
                   dm_stm32h7_usb_host_pipe_transfer(
                       &client, &pipe,
                       DM_STM32H7_USB_HOST_PIPE_PID_DATA0, NULL, 0,
                       NULL, 0, &actual_length) ==
                   DM_STM32H7_USB_HOST_PIPE_INVALID &&
                   actual_length == 99,
               "reject released lease before touching PIO registers")) {
        return 1;
    }

    puts("RESULT: STM32H7 USB host pipe packet encoding smoke passed");
    return 0;
}
