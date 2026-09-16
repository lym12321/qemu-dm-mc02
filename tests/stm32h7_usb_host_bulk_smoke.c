#include "dm_stm32h7_usb_host_bulk.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

typedef struct FakePacket {
    unsigned calls;
    uint8_t channels[8];
    DmUsbHostEndpointDataPid pid[8];
    size_t lengths[8];
    bool lease_was_active[8];
    DmStm32H7UsbHostPipeResult result;
    unsigned nak_call;
} FakePacket;

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static DmStm32H7UsbHostPipeResult fake_packet(
    void *opaque, DmStm32H7UsbHostPipeClient *client,
    const DmUsbHostPipe *pipe, DmStm32H7UsbHostPipePid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length)
{
    FakePacket *fake = opaque;
    size_t length = pipe->direction_in ? in_capacity : out_length;
    unsigned index = fake->calls;

    (void)out_data;
    (void)in_data;
    fake->channels[index] = client->lease->channel;
    fake->pid[index] = pid == DM_STM32H7_USB_HOST_PIPE_PID_DATA0 ?
                       DM_USB_HOST_ENDPOINT_PID_DATA0 :
                       DM_USB_HOST_ENDPOINT_PID_DATA1;
    fake->lengths[index] = length;
    fake->lease_was_active[index] =
        dm_usb_host_channel_allocator_lease_active(
            client->allocator, client->lease);
    ++fake->calls;
    if (index == fake->nak_call) {
        *actual_length = 0;
        return DM_STM32H7_USB_HOST_PIPE_NAK;
    }
    if (fake->result != DM_STM32H7_USB_HOST_PIPE_OK) {
        *actual_length = 0;
        return fake->result;
    }
    *actual_length = length;
    return fake->result;
}

static void init_bulk_endpoint(DmUsbHostEndpointState *state,
                               DmUsbHostPipe *pipe, bool direction_in)
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
    uint8_t channel_ids[] = { 2 };
    DmUsbHostChannelAllocator allocator;
    DmUsbHostEndpointState state;
    DmUsbHostPipe pipe;
    DmStm32H7UsbHostBulk bulk;
    DmUsbHostChannelLease occupied;
    DmUsbHostBulkTransferResult result;
    DmUsbHostRetryPolicy retry_policy;
    FakePacket fake = {
        .result = DM_STM32H7_USB_HOST_PIPE_OK,
        .nak_call = 99,
    };
    uint8_t out_data[130] = { 0 };
    uint8_t in_data[80] = { 0 };
    size_t actual_length = 99;
    int owner;

    init_bulk_endpoint(&state, &pipe, false);
    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, channel_ids, 1) &&
                   dm_stm32h7_usb_host_bulk_init_with_transfer(
                       &bulk, 0, &allocator, fake_packet, &fake, &state),
               "initialize H723 bulk adapter") ||
        expect(dm_stm32h7_usb_host_bulk_run(
                   &bulk, out_data, sizeof(out_data), NULL, 0, false,
                   &actual_length) == DM_USB_HOST_BULK_OK &&
                   actual_length == sizeof(out_data) && fake.calls == 3 &&
                   fake.lengths[0] == 64 && fake.lengths[1] == 64 &&
                   fake.lengths[2] == 2 && fake.pid[0] ==
                   DM_USB_HOST_ENDPOINT_PID_DATA0 && fake.pid[1] ==
                   DM_USB_HOST_ENDPOINT_PID_DATA1 && fake.pid[2] ==
                   DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   fake.lease_was_active[0] && fake.lease_was_active[1] &&
                   fake.lease_was_active[2] &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA1 &&
                   !allocator.entry[0].owner,
               "H723 bulk submits MPS packets and releases each lease")) {
        return 1;
    }

    init_bulk_endpoint(&state, &pipe, true);
    fake.calls = 0;
    if (expect(dm_stm32h7_usb_host_bulk_init_with_transfer(
                   &bulk, 0, &allocator, fake_packet, &fake, &state) &&
                   dm_stm32h7_usb_host_bulk_run(
                       &bulk, NULL, 0, in_data, sizeof(in_data), false,
                       &actual_length) == DM_USB_HOST_BULK_OK &&
                   actual_length == 80 && fake.calls == 2 &&
                   fake.lengths[0] == 64 && fake.lengths[1] == 16,
               "H723 bulk maps accepted IN packets")) {
        return 1;
    }

    init_bulk_endpoint(&state, &pipe, false);
    fake.calls = 0;
    fake.nak_call = 1;
    if (expect(dm_stm32h7_usb_host_bulk_init_with_transfer(
                   &bulk, 0, &allocator, fake_packet, &fake, &state) &&
                   dm_stm32h7_usb_host_bulk_run(
                       &bulk, out_data, sizeof(out_data), NULL, 0, false,
                       &actual_length) == DM_USB_HOST_BULK_NAK &&
                   actual_length == 64 && fake.calls == 2 &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA1 &&
                   !allocator.entry[0].owner,
               "H723 bulk maps NAK after partial progress")) {
        return 1;
    }

    init_bulk_endpoint(&state, &pipe, false);
    fake.calls = 0;
    fake.nak_call = 0;
    fake.result = DM_STM32H7_USB_HOST_PIPE_OK;
    if (expect(dm_usb_host_retry_init(
                   &retry_policy, (DmUsbHostRetryConfig) {
                       .max_retries = 1,
                       .timeout_ns = 100,
                   }) &&
                   dm_stm32h7_usb_host_bulk_init_with_transfer(
                       &bulk, 0, &allocator, fake_packet, &fake, &state),
               "initialize H723 retry policy") ||
        expect(dm_stm32h7_usb_host_bulk_run_with_retry(
                   &bulk, out_data, 1, NULL, 0, false, &retry_policy, 4000,
                   &actual_length) == DM_USB_HOST_BULK_RETRY &&
                   actual_length == 0 && fake.calls == 1 &&
                   fake.pid[0] == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   fake.lease_was_active[0] && !allocator.entry[0].owner,
               "H723 wrapper releases lease after retryable NAK") ||
        expect(dm_stm32h7_usb_host_bulk_run_with_retry(
                   &bulk, out_data, 1, NULL, 0, false, &retry_policy, 4001,
                   &actual_length) == DM_USB_HOST_BULK_OK &&
                   actual_length == 1 && fake.calls == 2 &&
                   fake.pid[1] == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   fake.lease_was_active[1] && !allocator.entry[0].owner &&
                   !retry_policy.active,
               "H723 wrapper retries same PID and releases success lease")) {
        return 1;
    }

    fake.nak_call = 99;
    if (expect(dm_stm32h7_usb_host_bulk_run(
                   &bulk, out_data + 64, sizeof(out_data) - 64, NULL, 0,
                   false, &actual_length) == DM_USB_HOST_BULK_OK &&
                   actual_length == sizeof(out_data) - 64 && fake.calls == 4 &&
                   fake.pid[2] == DM_USB_HOST_ENDPOINT_PID_DATA1 &&
                   fake.pid[3] == DM_USB_HOST_ENDPOINT_PID_DATA0,
               "caller can retry remaining bulk span without replay")) {
        return 1;
    }

    init_bulk_endpoint(&state, &pipe, false);
    fake.calls = 0;
    fake.result = DM_STM32H7_USB_HOST_PIPE_OK;
    if (expect(dm_usb_host_channel_allocator_acquire(
                   &allocator, &owner, &occupied) &&
                   dm_stm32h7_usb_host_bulk_init_with_transfer(
                       &bulk, 0, &allocator, fake_packet, &fake, &state) &&
                   dm_stm32h7_usb_host_bulk_run(
                       &bulk, out_data, 1, NULL, 0, false, &actual_length) ==
                   DM_USB_HOST_BULK_DEFERRED && actual_length == 0 &&
                   fake.calls == 0 &&
                   state.next_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 &&
                   dm_usb_host_channel_allocator_release(
                       &allocator, &occupied),
               "channel exhaustion defers bulk without state mutation")) {
        return 1;
    }

    init_bulk_endpoint(&state, &pipe, false);
    fake.calls = 0;
    fake.result = DM_STM32H7_USB_HOST_PIPE_STALL;
    if (expect(dm_stm32h7_usb_host_bulk_init_with_transfer(
                   &bulk, 0, &allocator, fake_packet, &fake, &state) &&
                   dm_stm32h7_usb_host_bulk_run(
                       &bulk, out_data, 1, NULL, 0, false, &actual_length) ==
                   DM_USB_HOST_BULK_STALL && state.halted &&
                   !allocator.entry[0].owner &&
                   dm_stm32h7_usb_host_bulk_run(
                       &bulk, out_data, 1, NULL, 0, false, &actual_length) ==
                   DM_USB_HOST_BULK_STALL && fake.calls == 1,
               "H723 bulk releases a stalled packet and halts endpoint")) {
        return 1;
    }

    result = dm_stm32h7_usb_host_bulk_run(
        NULL, NULL, 0, NULL, 0, false, &actual_length);
    if (expect(result == DM_USB_HOST_BULK_INVALID,
               "reject null H723 bulk adapter")) {
        return 1;
    }

    puts("RESULT: STM32H7 USB host bulk adapter smoke passed");
    return 0;
}
