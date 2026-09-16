#include "dm_stm32h7_usb_host_periodic.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

typedef struct FakeTransfer {
    unsigned calls;
    uint8_t channels[4];
    bool lease_was_active[4];
    DmStm32H7UsbHostPeriodic *nested_periodic;
    bool nested_done;
    DmUsbHostPeriodicPollResult nested_result;
    DmUsbHostEndpointCompletion nested_completion;
    uint8_t data[8];
} FakeTransfer;

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static DmStm32H7UsbHostPipeResult fake_transfer(
    void *opaque, DmStm32H7UsbHostPipeClient *client,
    const DmUsbHostPipe *pipe, DmStm32H7UsbHostPipePid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length)
{
    FakeTransfer *fake = opaque;

    (void)pipe;
    (void)pid;
    (void)out_data;
    (void)out_length;
    (void)in_data;
    (void)in_capacity;
    fake->channels[fake->calls] = client->lease->channel;
    fake->lease_was_active[fake->calls] =
        dm_usb_host_channel_allocator_lease_active(
            client->allocator, client->lease);
    ++fake->calls;
    if (fake->nested_periodic && !fake->nested_done) {
        fake->nested_done = true;
        fake->nested_result = dm_stm32h7_usb_host_periodic_poll(
            fake->nested_periodic, 0, NULL, 0, fake->data,
            sizeof(fake->data),
            &fake->nested_completion);
    }
    *actual_length = 0;
    return DM_STM32H7_USB_HOST_PIPE_NAK;
}

static void init_endpoint(DmUsbHostEndpointState *state,
                          DmUsbHostPipe *pipe, uint8_t device_address,
                          uint8_t endpoint_number)
{
    *pipe = (DmUsbHostPipe) {
        .device_address = device_address,
        .endpoint_number = endpoint_number,
        .attributes = DM_USB_HOST_PIPE_INTERRUPT,
        .direction_in = true,
        .transfer_type = DM_USB_HOST_PIPE_INTERRUPT,
        .max_packet_size = 8,
        .transactions_per_microframe = 1,
        .interval = 1,
    };
    dm_usb_host_endpoint_state_init(state, pipe);
}

int main(void)
{
    uint8_t channels[] = { 2, 3 };
    uint8_t single_channel[] = { 2 };
    DmUsbHostChannelAllocator allocator;
    DmUsbHostChannelLease occupied;
    DmUsbHostEndpointState first_state;
    DmUsbHostEndpointState second_state;
    DmUsbHostPipe first_pipe;
    DmUsbHostPipe second_pipe;
    DmStm32H7UsbHostPeriodic first;
    DmStm32H7UsbHostPeriodic second;
    DmUsbHostEndpointCompletion completion;
    FakeTransfer fake = { 0 };
    uint8_t data[8] = { 0 };
    DmUsbHostRetryConfig retry_config = {
        .max_retries = 1,
        .timeout_ns = 0,
    };
    int owner;

    init_endpoint(&first_state, &first_pipe, 5, 1);
    init_endpoint(&second_state, &second_pipe, 6, 2);
    if (expect(dm_usb_host_channel_allocator_init(&allocator, channels, 2) &&
                   dm_stm32h7_usb_host_periodic_init_with_transfer(
                       &first, 0, &allocator, fake_transfer, &fake,
                       &first_state, DM_USB_HOST_SPEED_HIGH, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   dm_stm32h7_usb_host_periodic_init_with_transfer(
                       &second, 0, &allocator, fake_transfer, &fake,
                       &second_state, DM_USB_HOST_SPEED_HIGH, 0) ==
                       DM_USB_HOST_PERIODIC_SCHEDULE_OK,
               "initialize two periodic adapters on one allocator") ||
        expect((fake.nested_periodic = &second,
                dm_stm32h7_usb_host_periodic_poll(
                    &first, 0, NULL, 0, data, sizeof(data), &completion)) ==
                   DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   fake.nested_result == DM_USB_HOST_PERIODIC_POLL_SUBMITTED &&
                   fake.calls == 2 && fake.channels[0] == 2 &&
                   fake.channels[1] == 3 &&
                   fake.lease_was_active[0] && fake.lease_was_active[1] &&
                   !allocator.entry[0].owner && !allocator.entry[1].owner,
               "nested adapters receive distinct leases and release them")) {
        return 1;
    }

    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, single_channel, 1) &&
                   dm_usb_host_channel_allocator_acquire(
                       &allocator, &owner, &occupied),
               "occupy the only available channel")) {
        return 1;
    }
    init_endpoint(&first_state, &first_pipe, 5, 1);
    if (expect(dm_stm32h7_usb_host_periodic_init_with_transfer(
                   &first, 0, &allocator, fake_transfer, &fake,
                   &first_state, DM_USB_HOST_SPEED_HIGH, 0) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK &&
                   dm_stm32h7_usb_host_periodic_poll(
                       &first, 0, NULL, 0, data, sizeof(data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_DEFERRED && fake.calls == 2 &&
                   first.poller.schedule.next_slot_ns == 0 &&
                   first_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "resource exhaustion defers without changing endpoint")) {
        return 1;
    }
    if (expect(dm_usb_host_channel_allocator_release(&allocator, &occupied),
               "release externally occupied channel")) {
        return 1;
    }

    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, single_channel, 1),
               "reinitialize allocator for retry test")) {
        return 1;
    }
    init_endpoint(&first_state, &first_pipe, 5, 1);
    fake.calls = 0;
    fake.nested_periodic = NULL;
    fake.nested_done = true;
    if (expect(dm_stm32h7_usb_host_periodic_init_with_transfer_and_retry(
                   &first, 0, &allocator, fake_transfer, &fake,
                   &first_state, DM_USB_HOST_SPEED_HIGH, 0, &retry_config) ==
                   DM_USB_HOST_PERIODIC_SCHEDULE_OK,
               "initialize H723 periodic retry adapter") ||
        expect(dm_stm32h7_usb_host_periodic_poll(
                   &first, 0, NULL, 0, data, sizeof(data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_SUBMITTED && fake.calls == 1 &&
                   first.poller.schedule.next_slot_ns == 0 &&
                   first_state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   !allocator.entry[0].owner,
               "H723 NAK starts a retained retry without holding channel") ||
        expect(dm_stm32h7_usb_host_periodic_poll(
                   &first, 125000, NULL, 0, data, sizeof(data), &completion) ==
                   DM_USB_HOST_PERIODIC_POLL_RETRY_EXHAUSTED &&
                   fake.calls == 2 && fake.channels[0] == 2 &&
                   fake.channels[1] == 2 && !allocator.entry[0].owner &&
                   first.poller.schedule.next_slot_ns == 250000,
               "H723 retry reacquires and releases the same channel")) {
        return 1;
    }

    puts("RESULT: STM32H7 USB host periodic lease smoke passed");
    return 0;
}
