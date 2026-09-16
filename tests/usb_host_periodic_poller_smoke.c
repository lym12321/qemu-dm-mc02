#include "dm_usb_host_periodic_poller.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define USB_MICROFRAME_NS 125000ull

typedef struct TestSubmit {
    unsigned calls;
    DmUsbHostEndpointDataPid last_pid;
    DmUsbHostPeriodicSubmitResult result;
    DmUsbHostEndpointCompletion completion;
    size_t actual_length;
} TestSubmit;

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static DmUsbHostPeriodicSubmitResult submit(
    void *opaque, const DmUsbHostPipe *pipe, DmUsbHostEndpointDataPid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion,
    size_t *actual_length)
{
    TestSubmit *test = opaque;

    (void)pipe;
    (void)out_data;
    (void)out_length;
    (void)in_data;
    (void)in_capacity;
    ++test->calls;
    test->last_pid = pid;
    *completion = test->completion;
    *actual_length = test->actual_length;
    return test->result;
}

int main(void)
{
    DmUsbHostPipe pipe = {
        .device_address = 5,
        .endpoint_number = 1,
        .attributes = DM_USB_HOST_PIPE_INTERRUPT,
        .direction_in = true,
        .transfer_type = DM_USB_HOST_PIPE_INTERRUPT,
        .max_packet_size = 8,
        .transactions_per_microframe = 1,
        .interval = 1,
    };
    DmUsbHostEndpointState endpoint_state;
    DmUsbHostPeriodicPoller poller;
    DmUsbHostEndpointCompletion completion;
    DmUsbHostRetryConfig retry_config;
    uint8_t in_data[8] = { 0 };
    TestSubmit test = {
        .result = DM_USB_HOST_PERIODIC_SUBMIT_OK,
        .completion = DM_USB_HOST_ENDPOINT_NAK,
    };

    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    if (expect(dm_usb_host_periodic_poller_init(
                   &poller, &endpoint_state, DM_USB_HOST_SPEED_HIGH,
                   1000, submit, &test) == DM_USB_HOST_PERIODIC_SCHEDULE_OK,
               "initialize high-speed periodic poller") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, 999, NULL, 0, in_data, sizeof(in_data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_NOT_DUE && test.calls == 0,
               "early timestamp does not submit") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, 1000, NULL, 0, in_data, sizeof(in_data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   completion == DM_USB_HOST_ENDPOINT_NAK && test.calls == 1 &&
                   test.last_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   endpoint_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   poller.schedule.next_slot_ns == 1000 + USB_MICROFRAME_NS,
               "NAK submits once and preserves PID") ||
               expect(dm_usb_host_periodic_poller_poll(
                   &poller, 1000, NULL, 0, in_data, sizeof(in_data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_NOT_DUE && test.calls == 1,
               "same timestamp cannot submit twice")) {
        return 1;
    }

    test.result = DM_USB_HOST_PERIODIC_SUBMIT_DEFERRED;
    if (expect(dm_usb_host_periodic_poller_poll(
                   &poller, 1000 + USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_DEFERRED &&
                   test.calls == 2 &&
                   endpoint_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   poller.schedule.next_slot_ns ==
                       1000 + USB_MICROFRAME_NS,
               "deferred submit preserves endpoint and schedule")) {
        return 1;
    }

    test.completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
    test.result = DM_USB_HOST_PERIODIC_SUBMIT_OK;
    test.actual_length = 8;
    if (expect(dm_usb_host_periodic_poller_poll(
                   &poller, 1000 + USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   completion == DM_USB_HOST_ENDPOINT_ACCEPTED && test.calls == 3 &&
                   test.last_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   endpoint_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA1,
               "accepted packet advances endpoint PID") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, 1000 + 4 * USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   test.calls == 4 && test.last_pid == DM_USB_HOST_ENDPOINT_PID_DATA1 &&
                   poller.schedule.next_slot_ns == 1000 + 5 * USB_MICROFRAME_NS,
               "late poll submits once and skips elapsed slots")) {
        return 1;
    }

    test.completion = DM_USB_HOST_ENDPOINT_STALL;
    test.actual_length = 0;
    if (expect(dm_usb_host_periodic_poller_poll(
                   &poller, 1000 + 5 * USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   endpoint_state.halted && test.calls == 5,
               "submitted STALL halts endpoint") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, 1000 + 6 * USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_HALTED &&
                   test.calls == 5,
               "halted endpoint does not submit")) {
        return 1;
    }

    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    test.result = DM_USB_HOST_PERIODIC_SUBMIT_INVALID;
    if (expect(dm_usb_host_periodic_poller_init(
                   &poller, &endpoint_state, DM_USB_HOST_SPEED_HIGH, 0,
                   submit, &test) == DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   dm_usb_host_periodic_poller_poll(
                       &poller, 0, NULL, 0, in_data, sizeof(in_data),
                       &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_INVALID &&
                   endpoint_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   poller.schedule.next_slot_ns == 0,
               "invalid submit does not advance endpoint or schedule")) {
        return 1;
    }

    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    test.calls = 0;
    test.result = DM_USB_HOST_PERIODIC_SUBMIT_OK;
    test.completion = DM_USB_HOST_ENDPOINT_NAK;
    test.actual_length = 0;
    retry_config = (DmUsbHostRetryConfig) {
        .max_retries = 2,
        .timeout_ns = 0,
    };
    if (expect(dm_usb_host_periodic_poller_init_with_retry(
                   &poller, &endpoint_state, DM_USB_HOST_SPEED_HIGH, 0,
                   &retry_config, submit, &test) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK,
               "initialize retryable periodic poller") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, 0, NULL, 0, in_data, sizeof(in_data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_SUBMITTED && test.calls == 1 &&
                   completion == DM_USB_HOST_ENDPOINT_NAK &&
                   test.last_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   endpoint_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   poller.schedule.next_slot_ns == 0,
               "first NAK retains retry slot and PID") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   test.calls == 2 && test.last_pid ==
                   DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   poller.schedule.next_slot_ns == 0,
               "NAK retry uses the retained packet") ) {
        return 1;
    }

    test.completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
    test.actual_length = 8;
    if (expect(dm_usb_host_periodic_poller_poll(
                   &poller, 2 * USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   test.calls == 3 && test.last_pid ==
                   DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   endpoint_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA1 &&
                   poller.schedule.next_slot_ns == 3 * USB_MICROFRAME_NS,
               "successful retry completes packet and advances schedule")) {
        return 1;
    }

    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    test.calls = 0;
    test.completion = DM_USB_HOST_ENDPOINT_NAK;
    test.actual_length = 0;
    retry_config.max_retries = 1;
    if (expect(dm_usb_host_periodic_poller_init_with_retry(
                   &poller, &endpoint_state, DM_USB_HOST_SPEED_HIGH, 0,
                   &retry_config, submit, &test) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK,
               "initialize bounded retry poller") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, 0, NULL, 0, in_data, sizeof(in_data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_SUBMITTED && test.calls == 1,
               "bounded retry first NAK") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_RETRY_EXHAUSTED &&
                   completion == DM_USB_HOST_ENDPOINT_NAK && test.calls == 2 &&
                   endpoint_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   poller.schedule.next_slot_ns == 2 * USB_MICROFRAME_NS,
               "retry budget exhausts at the terminal NAK")) {
        return 1;
    }

    dm_usb_host_endpoint_state_init(&endpoint_state, &pipe);
    test.calls = 0;
    retry_config = (DmUsbHostRetryConfig) {
        .max_retries = 5,
        .timeout_ns = USB_MICROFRAME_NS,
    };
    if (expect(dm_usb_host_periodic_poller_init_with_retry(
                   &poller, &endpoint_state, DM_USB_HOST_SPEED_HIGH, 0,
                   &retry_config, submit, &test) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK,
               "initialize timeout retry poller") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, 0, NULL, 0, in_data, sizeof(in_data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_SUBMITTED && test.calls == 1,
               "timeout policy first NAK") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_RETRY_TIMEOUT &&
                   completion == DM_USB_HOST_ENDPOINT_NAK && test.calls == 1 &&
                   poller.schedule.next_slot_ns == 2 * USB_MICROFRAME_NS,
               "timeout is checked before a retry submit")) {
        return 1;
    }

    test.completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
    test.actual_length = 8;
    if (expect(dm_usb_host_periodic_poller_poll(
                   &poller, 2 * USB_MICROFRAME_NS, NULL, 0, in_data,
                   sizeof(in_data),
                   &completion) == DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   test.calls == 2,
               "timeout terminal state permits the next packet")) {
        return 1;
    }

    if (expect(dm_usb_host_periodic_poller_poll(
                   &poller, 3 * USB_MICROFRAME_NS, (const uint8_t *)1, 0,
                   in_data, sizeof(in_data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_INVALID && test.calls == 2,
               "reject direction-mismatched periodic request") ||
        expect(dm_usb_host_periodic_poller_poll(
                   &poller, 3 * USB_MICROFRAME_NS, NULL, 0, in_data, 9,
                   &completion) == DM_USB_HOST_PERIODIC_POLL_INVALID &&
                   test.calls == 2,
               "reject periodic packet above endpoint MPS")) {
        return 1;
    }

    puts("RESULT: USB host periodic poller smoke passed");
    return 0;
}
