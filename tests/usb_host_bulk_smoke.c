#include "dm_usb_host_bulk.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

typedef struct FakeSubmit {
    unsigned calls;
    size_t out_lengths[8];
    size_t in_capacities[8];
    size_t requested[8];
    DmUsbHostEndpointDataPid pid[8];
    DmUsbHostBulkSubmitResult result;
    DmUsbHostEndpointCompletion completion;
    size_t actual[8];
    bool actual_is_set[8];
    bool nak_on_first;
    bool nak_on_second;
} FakeSubmit;

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static DmUsbHostBulkSubmitResult submit(
    void *opaque, const DmUsbHostPipe *pipe, DmUsbHostEndpointDataPid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion,
    size_t *actual_length)
{
    FakeSubmit *fake = opaque;
    bool nak;

    (void)pipe;
    (void)out_data;
    (void)in_data;
    fake->out_lengths[fake->calls] = out_length;
    fake->in_capacities[fake->calls] = in_capacity;
    fake->requested[fake->calls] = out_length ? out_length : in_capacity;
    fake->pid[fake->calls] = pid;
    nak = (fake->nak_on_first && fake->calls == 0) ||
          (fake->nak_on_second && fake->calls == 1) ||
          fake->completion == DM_USB_HOST_ENDPOINT_NAK;
    *completion = nak ? DM_USB_HOST_ENDPOINT_NAK : fake->completion;
    *actual_length = (nak || fake->completion != DM_USB_HOST_ENDPOINT_ACCEPTED) ?
                     0 : fake->actual_is_set[fake->calls] ?
                     fake->actual[fake->calls] :
                     fake->requested[fake->calls];
    ++fake->calls;
    return fake->result;
}

static void init_bulk(DmUsbHostEndpointState *state, DmUsbHostPipe *pipe,
                      bool direction_in)
{
    *pipe = (DmUsbHostPipe) {
        .device_address = 5,
        .endpoint_number = 2,
        .attributes = DM_USB_HOST_PIPE_BULK,
        .direction_in = direction_in,
        .transfer_type = DM_USB_HOST_PIPE_BULK,
        .max_packet_size = 64,
        .transactions_per_microframe = 1,
    };
    dm_usb_host_endpoint_state_init(state, pipe);
}

int main(void)
{
    DmUsbHostEndpointState state;
    DmUsbHostPipe pipe;
    DmUsbHostBulkTransfer transfer;
    DmUsbHostBulkTransferResult result;
    DmUsbHostRetryPolicy retry_policy;
    FakeSubmit fake = {
        .result = DM_USB_HOST_BULK_SUBMIT_OK,
        .completion = DM_USB_HOST_ENDPOINT_ACCEPTED,
    };
    uint8_t out_data[130] = { 0 };
    uint8_t in_data[130] = { 0 };
    size_t actual_length = 99;

    init_bulk(&state, &pipe, false);
    if (expect(dm_usb_host_bulk_transfer_init(
                   &transfer, &state, submit, &fake),
               "initialize bulk OUT transfer") ||
        expect(dm_usb_host_bulk_transfer_run(
                   &transfer, out_data, sizeof(out_data), NULL, 0, false,
                   &actual_length) == DM_USB_HOST_BULK_OK &&
                   actual_length == sizeof(out_data) && fake.calls == 3 &&
                   fake.requested[0] == 64 && fake.requested[1] == 64 &&
                   fake.requested[2] == 2 &&
                   fake.out_lengths[0] == 64 && fake.in_capacities[0] == 0 &&
                   fake.pid[0] == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   fake.pid[1] == DM_USB_HOST_ENDPOINT_PID_DATA1 &&
                   fake.pid[2] == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA1,
               "bulk OUT packetizes and toggles PID")) {
        return 1;
    }

    init_bulk(&state, &pipe, true);
    fake.calls = 0;
    fake.actual[0] = 64;
    fake.actual[1] = 10;
    fake.actual_is_set[0] = true;
    fake.actual_is_set[1] = true;
    if (expect(dm_usb_host_bulk_transfer_init(
                   &transfer, &state, submit, &fake),
               "initialize bulk IN transfer") ||
        expect(dm_usb_host_bulk_transfer_run(
                   &transfer, NULL, 0, in_data, sizeof(in_data), false,
                   &actual_length) == DM_USB_HOST_BULK_OK &&
                   actual_length == 74 && fake.calls == 2 &&
                   fake.requested[0] == 64 && fake.requested[1] == 64 &&
                   fake.out_lengths[0] == 0 && fake.out_lengths[1] == 0 &&
                   fake.in_capacities[0] == 64 &&
                   fake.in_capacities[1] == 64 &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "bulk IN stops on short packet")) {
        return 1;
    }

    init_bulk(&state, &pipe, false);
    fake.calls = 0;
    fake.actual[0] = 0;
    fake.actual[1] = 0;
    fake.actual_is_set[0] = false;
    fake.actual_is_set[1] = false;
    if (expect(dm_usb_host_bulk_transfer_init(
                   &transfer, &state, submit, &fake),
               "reinitialize exact bulk OUT transfer") ||
        expect(dm_usb_host_bulk_transfer_run(
                   &transfer, out_data, 64, NULL, 0, true, &actual_length) ==
                   DM_USB_HOST_BULK_OK &&
                   actual_length == 64 && fake.calls == 2 &&
                   fake.requested[0] == 64 && fake.requested[1] == 0 &&
                   fake.pid[1] == DM_USB_HOST_ENDPOINT_PID_DATA1,
               "append explicit zero packet on exact multiple")) {
        return 1;
    }

    init_bulk(&state, &pipe, false);
    fake.calls = 0;
    fake.actual[0] = 0;
    fake.actual[1] = 0;
    fake.actual_is_set[0] = true;
    fake.actual_is_set[1] = true;
    fake.completion = DM_USB_HOST_ENDPOINT_NAK;
    if (expect(dm_usb_host_bulk_transfer_init(
                   &transfer, &state, submit, &fake),
               "initialize NAK bulk transfer") ||
        expect(dm_usb_host_bulk_transfer_run(
                   &transfer, out_data, sizeof(out_data), NULL, 0, false,
                   &actual_length) == DM_USB_HOST_BULK_NAK &&
                   actual_length == 0 && fake.calls == 1 &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "NAK preserves current packet PID")) {
        return 1;
    }

    init_bulk(&state, &pipe, false);
    fake.calls = 0;
    fake.actual_is_set[0] = false;
    fake.actual_is_set[1] = false;
    fake.nak_on_first = true;
    fake.completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
    if (expect(dm_usb_host_retry_init(
                   &retry_policy, (DmUsbHostRetryConfig) {
                       .max_retries = 1,
                       .timeout_ns = 100,
                   }) &&
                   dm_usb_host_bulk_transfer_init(
                       &transfer, &state, submit, &fake),
               "initialize generic retry policy") ||
        expect(dm_usb_host_bulk_transfer_run_with_retry(
                   &transfer, out_data, 1, NULL, 0, false, &retry_policy,
                   1000, &actual_length) == DM_USB_HOST_BULK_RETRY &&
                   actual_length == 0 && fake.calls == 1 &&
                   fake.pid[0] == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   retry_policy.active,
               "generic bulk OUT returns retry after first NAK") ||
        expect(dm_usb_host_bulk_transfer_run_with_retry(
                   &transfer, out_data, 1, NULL, 0, false, &retry_policy,
                   1001, &actual_length) == DM_USB_HOST_BULK_OK &&
                   actual_length == 1 && fake.calls == 2 &&
                   fake.pid[1] == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   !retry_policy.active,
               "generic retry resubmits the same PID at later virtual time")) {
        return 1;
    }

    init_bulk(&state, &pipe, false);
    fake.calls = 0;
    fake.nak_on_first = true;
    if (expect(dm_usb_host_retry_init(
                   &retry_policy, (DmUsbHostRetryConfig) {
                       .max_retries = 0,
                       .timeout_ns = 100,
                   }) &&
                   dm_usb_host_bulk_transfer_init(
                       &transfer, &state, submit, &fake),
               "initialize exhausted retry policy") ||
        expect(dm_usb_host_bulk_transfer_run_with_retry(
                   &transfer, out_data, 1, NULL, 0, false, &retry_policy,
                   2000, &actual_length) ==
                   DM_USB_HOST_BULK_RETRY_EXHAUSTED &&
                   actual_length == 0 && fake.calls == 1 &&
                   !retry_policy.active,
               "max retries zero returns retry exhausted")) {
        return 1;
    }

    init_bulk(&state, &pipe, false);
    fake.calls = 0;
    fake.nak_on_first = true;
    if (expect(dm_usb_host_retry_init(
                   &retry_policy, (DmUsbHostRetryConfig) {
                       .max_retries = 2,
                       .timeout_ns = 10,
                   }) &&
                   dm_usb_host_bulk_transfer_init(
                       &transfer, &state, submit, &fake),
               "initialize timeout retry policy") ||
        expect(dm_usb_host_bulk_transfer_run_with_retry(
                   &transfer, out_data, 1, NULL, 0, false, &retry_policy,
                   3000, &actual_length) == DM_USB_HOST_BULK_RETRY &&
                   actual_length == 0 && fake.calls == 1,
               "timeout setup returns retry after NAK") ||
        expect(dm_usb_host_bulk_transfer_run_with_retry(
                   &transfer, out_data, 1, NULL, 0, false, &retry_policy,
                   3010, &actual_length) == DM_USB_HOST_BULK_RETRY_TIMEOUT &&
                   actual_length == 0 && fake.calls == 1 &&
                   !retry_policy.active,
               "timeout returns before resubmitting the packet")) {
        return 1;
    }

    fake.nak_on_first = false;
    fake.result = DM_USB_HOST_BULK_SUBMIT_DEFERRED;
    if (expect(dm_usb_host_bulk_transfer_run(
                   &transfer, out_data, sizeof(out_data), NULL, 0, false,
                   &actual_length) == DM_USB_HOST_BULK_DEFERRED &&
                   actual_length == 0 && fake.calls == 2 &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "deferred bulk submit preserves state")) {
        return 1;
    }

    init_bulk(&state, &pipe, false);
    fake.calls = 0;
    fake.result = DM_USB_HOST_BULK_SUBMIT_OK;
    fake.completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
    fake.nak_on_second = true;
    if (expect(dm_usb_host_bulk_transfer_init(
                   &transfer, &state, submit, &fake),
               "initialize partial bulk OUT transfer") ||
        expect(dm_usb_host_bulk_transfer_run(
                   &transfer, out_data, sizeof(out_data), NULL, 0, false,
                   &actual_length) == DM_USB_HOST_BULK_NAK &&
                   actual_length == 64 && fake.calls == 2 &&
                   fake.pid[1] == DM_USB_HOST_ENDPOINT_PID_DATA1 &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA1,
               "partial bulk transfer reports NAK and retains next PID")) {
        return 1;
    }

    init_bulk(&state, &pipe, false);
    fake.calls = 0;
    fake.nak_on_second = false;
    fake.completion = DM_USB_HOST_ENDPOINT_STALL;
    if (expect(dm_usb_host_bulk_transfer_init(
                   &transfer, &state, submit, &fake),
               "initialize stalled bulk transfer") ||
        expect(dm_usb_host_bulk_transfer_run(
                   &transfer, out_data, 64, NULL, 0, false, &actual_length) ==
                   DM_USB_HOST_BULK_STALL && state.halted && fake.calls == 1 &&
                   dm_usb_host_bulk_transfer_run(
                       &transfer, out_data, 64, NULL, 0, false,
                       &actual_length) == DM_USB_HOST_BULK_STALL &&
                   fake.calls == 1,
               "STALL halts bulk endpoint and stops later submits")) {
        return 1;
    }

    fake.result = DM_USB_HOST_BULK_SUBMIT_OK;
    fake.completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
    state.pipe.transfer_type = DM_USB_HOST_PIPE_INTERRUPT;
    if (expect(!dm_usb_host_bulk_transfer_init(
                   &transfer, &state, submit, &fake),
               "reject non-bulk endpoint")) {
        return 1;
    }

    result = dm_usb_host_bulk_transfer_run(
        NULL, NULL, 0, NULL, 0, false, &actual_length);
    if (expect(result == DM_USB_HOST_BULK_INVALID,
               "reject null bulk transfer")) {
        return 1;
    }

    puts("RESULT: USB host bulk transfer smoke passed");
    return 0;
}
