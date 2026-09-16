#include "dm_usb_host_endpoint_state.h"

#include <stdbool.h>
#include <stdio.h>

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static DmUsbHostPipe make_pipe(DmUsbHostPipeTransferType transfer_type)
{
    return (DmUsbHostPipe) {
        .device_address = 5,
        .endpoint_number = 1,
        .attributes = transfer_type,
        .direction_in = true,
        .transfer_type = transfer_type,
        .max_packet_size = 8,
        .transactions_per_microframe = 1,
        .interval = 10,
    };
}

int main(void)
{
    DmUsbHostEndpointState state;
    DmUsbHostPipe bulk_pipe = make_pipe(DM_USB_HOST_PIPE_BULK);
    DmUsbHostPipe iso_pipe = make_pipe(DM_USB_HOST_PIPE_ISOCHRONOUS);
    DmUsbHostPipe out_pipe = make_pipe(DM_USB_HOST_PIPE_BULK);
    const DmUsbHostPipe *prepared_pipe;
    DmUsbHostEndpointDataPid pid;
    size_t requested_length;
    uint8_t data[8] = { 0 };

    out_pipe.direction_in = false;
    if (expect(dm_usb_host_endpoint_request_validate(
                   &bulk_pipe, NULL, 0, data, sizeof(data),
                   &requested_length) && requested_length == sizeof(data),
               "validate one IN packet") ||
        expect(dm_usb_host_endpoint_request_validate(
                   &out_pipe, data, sizeof(data), NULL, 0,
                   &requested_length) && requested_length == sizeof(data),
               "validate one OUT packet") ||
        expect(!dm_usb_host_endpoint_request_validate(
                   &bulk_pipe, data, 0, data, sizeof(data),
                   &requested_length),
               "reject direction-mismatched request") ||
        expect(!dm_usb_host_endpoint_request_validate(
                   &bulk_pipe, NULL, 0, data, sizeof(data) + 1,
                   &requested_length),
               "reject packet above MPS") ||
        expect(!dm_usb_host_endpoint_completion_validate(
                   DM_USB_HOST_ENDPOINT_NAK, 8, 1),
               "reject data on NAK") ||
        expect(!dm_usb_host_endpoint_completion_validate(
                   (DmUsbHostEndpointCompletion)-1, 0, 0),
               "reject invalid completion value")) {
        return 1;
    }

    dm_usb_host_endpoint_state_init(&state, &bulk_pipe);
    if (expect(dm_usb_host_endpoint_state_prepare(
                   &state, &prepared_pipe, &pid) == DM_USB_HOST_ENDPOINT_STATE_OK &&
                   prepared_pipe == &state.pipe &&
                   pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "initial bulk DATA0") ||
        expect(dm_usb_host_endpoint_state_complete(
                   &state, DM_USB_HOST_ENDPOINT_ACCEPTED, 8, 8) ==
                   DM_USB_HOST_ENDPOINT_STATE_OK,
               "accept bulk packet") ||
        expect(dm_usb_host_endpoint_state_prepare(
                   &state, &prepared_pipe, &pid) == DM_USB_HOST_ENDPOINT_STATE_OK &&
                   pid == DM_USB_HOST_ENDPOINT_PID_DATA1,
               "bulk success toggles DATA1") ||
        expect(dm_usb_host_endpoint_state_complete(
                   &state, DM_USB_HOST_ENDPOINT_NAK, 8, 0) ==
                   DM_USB_HOST_ENDPOINT_STATE_OK &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA1,
               "NAK preserves toggle") ||
        expect(dm_usb_host_endpoint_state_complete(
                   &state, DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR, 8, 0) ==
                   DM_USB_HOST_ENDPOINT_STATE_OK &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA1,
               "transaction error preserves toggle") ||
        expect(dm_usb_host_endpoint_state_complete(
                   &state, DM_USB_HOST_ENDPOINT_ACCEPTED, 8, 3) ==
                   DM_USB_HOST_ENDPOINT_STATE_OK &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "short successful packet toggles without policy")) {
        return 1;
    }

    if (expect(dm_usb_host_endpoint_state_complete(
                   &state, DM_USB_HOST_ENDPOINT_STALL, 8, 0) ==
                   DM_USB_HOST_ENDPOINT_STATE_OK && state.halted,
               "STALL halts endpoint") ||
        expect(dm_usb_host_endpoint_state_prepare(
                   &state, &prepared_pipe, &pid) == DM_USB_HOST_ENDPOINT_STATE_HALTED &&
                   dm_usb_host_endpoint_state_complete(
                       &state, DM_USB_HOST_ENDPOINT_ACCEPTED, 1, 1) ==
                   DM_USB_HOST_ENDPOINT_STATE_HALTED,
               "halt rejects packets") ||
        expect((dm_usb_host_endpoint_state_clear_halt(&state), true) &&
                   !state.halted &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "clear halt resets DATA0") ||
        expect(dm_usb_host_endpoint_state_complete(
                   &state, DM_USB_HOST_ENDPOINT_ACCEPTED, 1, 2) ==
                   DM_USB_HOST_ENDPOINT_STATE_INVALID &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "reject oversized completion without mutation")) {
        return 1;
    }

    dm_usb_host_endpoint_state_init(&state, &iso_pipe);
    if (expect(dm_usb_host_endpoint_state_complete(
                   &state, DM_USB_HOST_ENDPOINT_ACCEPTED, 8, 8) ==
                   DM_USB_HOST_ENDPOINT_STATE_OK &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "isochronous completion does not toggle") ||
        expect(dm_usb_host_endpoint_state_prepare(
                   NULL, &prepared_pipe, &pid) == DM_USB_HOST_ENDPOINT_STATE_INVALID,
               "reject missing state")) {
        return 1;
    }

    puts("RESULT: USB host endpoint state smoke passed");
    return 0;
}
