#include "dm_usb_host_periodic_registry.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define USB_MICROFRAME_NS 125000ull

typedef struct TestSubmit {
    unsigned calls;
    DmUsbHostEndpointDataPid last_pid;
    DmUsbHostPeriodicSubmitResult result;
    DmUsbHostEndpointCompletion completion;
} TestSubmit;

typedef struct TestCompletion {
    unsigned calls;
    DmUsbHostEndpointCompletion last;
} TestCompletion;

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
    *actual_length = 0;
    return test->result;
}

static void complete(void *opaque, DmUsbHostEndpointCompletion completion)
{
    TestCompletion *test = opaque;

    ++test->calls;
    test->last = completion;
}

static void init_poller(DmUsbHostPeriodicPoller *poller,
                        DmUsbHostEndpointState *endpoint_state,
                        DmUsbHostPipe *pipe, uint8_t interval,
                        DmUsbHostSpeed speed, TestSubmit *submit_state)
{
    *pipe = (DmUsbHostPipe) {
        .device_address = 5,
        .endpoint_number = 1,
        .attributes = DM_USB_HOST_PIPE_INTERRUPT,
        .direction_in = true,
        .transfer_type = DM_USB_HOST_PIPE_INTERRUPT,
        .max_packet_size = 8,
        .transactions_per_microframe = 1,
        .interval = interval,
    };
    dm_usb_host_endpoint_state_init(endpoint_state, pipe);
    dm_usb_host_periodic_poller_init(poller, endpoint_state, speed, 0,
                                     submit, submit_state);
}

int main(void)
{
    DmUsbHostPeriodicRegistry registry;
    DmUsbHostPeriodicPoller first;
    DmUsbHostPeriodicPoller second;
    DmUsbHostPeriodicPoller mismatched;
    DmUsbHostPeriodicPoller retry_poller;
    DmUsbHostEndpointState first_state;
    DmUsbHostEndpointState second_state;
    DmUsbHostEndpointState mismatched_state;
    DmUsbHostPipe first_pipe;
    DmUsbHostPipe second_pipe;
    DmUsbHostPipe mismatched_pipe;
    DmUsbHostPipe retry_pipe;
    uint8_t first_data[8];
    uint8_t second_data[8];
    uint8_t retry_data[8];
    TestSubmit first_submit = {
        .result = DM_USB_HOST_PERIODIC_SUBMIT_OK,
        .completion = DM_USB_HOST_ENDPOINT_NAK,
    };
    TestSubmit second_submit = {
        .result = DM_USB_HOST_PERIODIC_SUBMIT_OK,
        .completion = DM_USB_HOST_ENDPOINT_NAK,
    };
    TestSubmit mismatched_submit = {
        .result = DM_USB_HOST_PERIODIC_SUBMIT_OK,
        .completion = DM_USB_HOST_ENDPOINT_NAK,
    };
    TestCompletion first_completion = { 0 };
    TestCompletion second_completion = { 0 };
    TestCompletion retry_completion = { 0 };
    DmUsbHostRetryConfig retry_config = {
        .max_retries = 1,
        .timeout_ns = 0,
    };
    DmUsbHostPeriodicRegistryEntry first_entry;
    DmUsbHostPeriodicRegistryEntry second_entry;
    DmUsbHostPeriodicRegistryEntry mismatched_entry;

    init_poller(&first, &first_state, &first_pipe, 1,
                DM_USB_HOST_SPEED_HIGH, &first_submit);
    init_poller(&second, &second_state, &second_pipe, 2,
                DM_USB_HOST_SPEED_HIGH, &second_submit);
    init_poller(&mismatched, &mismatched_state, &mismatched_pipe, 1,
                DM_USB_HOST_SPEED_FULL, &mismatched_submit);
    first_entry = (DmUsbHostPeriodicRegistryEntry) {
        .poller = &first,
        .in_data = first_data,
        .in_capacity = sizeof(first_data),
        .completion = complete,
        .completion_opaque = &first_completion,
    };
    second_entry = (DmUsbHostPeriodicRegistryEntry) {
        .poller = &second,
        .in_data = second_data,
        .in_capacity = sizeof(second_data),
        .completion = complete,
        .completion_opaque = &second_completion,
    };
    mismatched_entry = (DmUsbHostPeriodicRegistryEntry) {
        .poller = &mismatched,
    };

    dm_usb_host_periodic_registry_init(&registry, DM_USB_HOST_SPEED_HIGH);
    if (expect(dm_usb_host_periodic_registry_add(&registry, &first_entry) &&
                   dm_usb_host_periodic_registry_add(&registry, &second_entry) &&
                   !dm_usb_host_periodic_registry_add(&registry, &first_entry) &&
                   !dm_usb_host_periodic_registry_add(
                       &registry, &mismatched_entry) && registry.count == 2,
               "register distinct matching-speed endpoints")) {
        return 1;
    }

    if (expect(dm_usb_host_periodic_registry_dispatch(&registry, 0) == 2 &&
                   first_submit.calls == 1 && second_submit.calls == 1 &&
                   first_completion.calls == 1 && second_completion.calls == 1,
               "first shared timestamp submits both endpoints") ||
        expect(dm_usb_host_periodic_registry_dispatch(
                   &registry, USB_MICROFRAME_NS) == 1 &&
                   first_submit.calls == 2 && second_submit.calls == 1,
               "intermediate SOF submits only the first endpoint") ||
        expect(dm_usb_host_periodic_registry_dispatch(
                   &registry, 2 * USB_MICROFRAME_NS) == 2 &&
                   first_submit.calls == 3 && second_submit.calls == 2,
               "later shared SOF submits both due endpoints")) {
        return 1;
    }

    second_submit.completion = DM_USB_HOST_ENDPOINT_STALL;
    if (expect(dm_usb_host_periodic_registry_dispatch(
                   &registry, 4 * USB_MICROFRAME_NS) == 2 &&
                   second_completion.last == DM_USB_HOST_ENDPOINT_STALL &&
                   registry.count == 1,
               "submitted STALL notifies then removes endpoint") ||
        expect(dm_usb_host_periodic_registry_remove(&registry, &first) &&
                   registry.count == 0 &&
                   dm_usb_host_periodic_registry_dispatch(
                       &registry, 5 * USB_MICROFRAME_NS) == 0,
               "explicit removal leaves no active endpoint")) {
        return 1;
    }

    init_poller(&retry_poller, &first_state, &retry_pipe, 1,
                DM_USB_HOST_SPEED_HIGH, &first_submit);
    if (expect(dm_usb_host_periodic_poller_init_with_retry(
                   &retry_poller, &first_state, DM_USB_HOST_SPEED_HIGH, 0,
                   &retry_config, submit, &first_submit) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK,
               "initialize registry retry endpoint")) {
        return 1;
    }
    first_submit.calls = 0;
    first_submit.completion = DM_USB_HOST_ENDPOINT_NAK;
    first_entry = (DmUsbHostPeriodicRegistryEntry) {
        .poller = &retry_poller,
        .in_data = retry_data,
        .in_capacity = sizeof(retry_data),
        .completion = complete,
        .completion_opaque = &retry_completion,
    };
    if (expect(dm_usb_host_periodic_registry_add(&registry, &first_entry),
               "register retry endpoint") ||
        expect(dm_usb_host_periodic_registry_dispatch(&registry, 0) == 1 &&
                   first_submit.calls == 1 && retry_completion.calls == 1 &&
                   registry.count == 1,
               "registry reports initial retryable NAK") ||
        expect(dm_usb_host_periodic_registry_dispatch(
                   &registry, USB_MICROFRAME_NS) == 0 &&
                   first_submit.calls == 2 && retry_completion.calls == 2 &&
                   retry_completion.last == DM_USB_HOST_ENDPOINT_NAK &&
                   registry.count == 1,
               "registry retains endpoint after retry exhaustion")) {
        return 1;
    }

    puts("RESULT: USB host periodic registry smoke passed");
    return 0;
}
