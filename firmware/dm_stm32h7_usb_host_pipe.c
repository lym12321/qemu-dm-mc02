/* Single-packet STM32H7 DWC2 host client driven by a generic endpoint pipe. */
#include "dm_stm32h7_usb_host_pipe.h"

#include <stdbool.h>

#define OTG_HCCHAR(channel)        (0x500u + 0x20u * (channel))
#define OTG_HCINT(channel)         (0x508u + 0x20u * (channel))
#define OTG_HCTSIZ(channel)        (0x510u + 0x20u * (channel))
#define OTG_HCFIFO(channel)        (0x1000u + 0x1000u * (channel))

#define HCCHAR_CHENA               (1u << 31)
#define HCCHAR_CHDIS               (1u << 30)
#define HCCHAR_DEVADDR_SHIFT       22u
#define HCCHAR_EPTYPE_SHIFT        18u
#define HCCHAR_EPDIR               (1u << 15)
#define HCCHAR_EPNUM_SHIFT         11u

#define HCINT_XFRC                 (1u << 0)
#define HCINT_CHHLTD               (1u << 1)
#define HCINT_STALL                (1u << 3)
#define HCINT_NAK                  (1u << 4)
#define HCINT_XACTERR              (1u << 7)
#define HCINT_ALL                  0x3fffu

#define HCTSIZ_PID_SHIFT           29u
#define HCTSIZ_PKT_SHIFT           19u
#define HCTSIZ_XFERSIZE_MASK       0x7ffffu

#define STM32H7_USB_HOST_CHANNELS  12u

static volatile uint32_t *dm_stm32h7_usb_host_pipe_reg(
    const DmStm32H7UsbHostPipeClient *client, uint32_t offset)
{
    return (volatile uint32_t *)(client->base + offset);
}

static void dm_stm32h7_usb_host_pipe_fifo_write(
    const DmStm32H7UsbHostPipeClient *client, const uint8_t *data,
    size_t length)
{
    volatile uint8_t *fifo = (volatile uint8_t *)(client->base +
                                                   OTG_HCFIFO(client->lease->channel));

    for (size_t index = 0; index < length; ++index) {
        fifo[0] = data[index];
    }
}

static void dm_stm32h7_usb_host_pipe_fifo_read(
    const DmStm32H7UsbHostPipeClient *client, uint8_t *data, size_t length)
{
    volatile uint8_t *fifo = (volatile uint8_t *)(client->base +
                                                   OTG_HCFIFO(client->lease->channel));

    for (size_t index = 0; index < length; ++index) {
        data[index] = fifo[0];
    }
}

DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_pipe_encode(
    const DmUsbHostPipe *pipe, DmStm32H7UsbHostPipePid pid, size_t length,
    uint32_t *hcchar, uint32_t *hctsiz)
{
    if (!pipe || !hcchar || !hctsiz || pipe->device_address > 127 ||
        !pipe->endpoint_number || pipe->endpoint_number > 15 ||
        pipe->transfer_type > DM_USB_HOST_PIPE_INTERRUPT ||
        (pipe->attributes & 3u) != pipe->transfer_type ||
        !pipe->max_packet_size || pipe->max_packet_size > 0x7ffu ||
        pipe->transactions_per_microframe != 1 || length > pipe->max_packet_size ||
        (pid != DM_STM32H7_USB_HOST_PIPE_PID_DATA0 &&
         pid != DM_STM32H7_USB_HOST_PIPE_PID_DATA1)) {
        return DM_STM32H7_USB_HOST_PIPE_INVALID;
    }

    *hcchar = ((uint32_t)pipe->device_address << HCCHAR_DEVADDR_SHIFT) |
              ((uint32_t)pipe->transfer_type << HCCHAR_EPTYPE_SHIFT) |
              (pipe->direction_in ? HCCHAR_EPDIR : 0) |
              ((uint32_t)pipe->endpoint_number << HCCHAR_EPNUM_SHIFT) |
              pipe->max_packet_size;
    *hctsiz = (uint32_t)length | (1u << HCTSIZ_PKT_SHIFT) |
              ((uint32_t)pid << HCTSIZ_PID_SHIFT);
    return DM_STM32H7_USB_HOST_PIPE_OK;
}

bool dm_stm32h7_usb_host_pipe_client_init(
    DmStm32H7UsbHostPipeClient *client, uintptr_t base,
    DmUsbHostChannelAllocator *allocator,
    const DmUsbHostChannelLease *lease)
{
    if (!client || !allocator || !lease ||
        !dm_usb_host_channel_allocator_lease_active(allocator, lease) ||
        lease->channel >= STM32H7_USB_HOST_CHANNELS) {
        return false;
    }
    *client = (DmStm32H7UsbHostPipeClient) {
        .base = base,
        .allocator = allocator,
        .lease = lease,
    };
    return true;
}

DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_pipe_transfer(
    DmStm32H7UsbHostPipeClient *client, const DmUsbHostPipe *pipe,
    DmStm32H7UsbHostPipePid pid, const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, size_t *actual_length)
{
    volatile uint32_t *hcint;
    volatile uint32_t *hctsiz;
    volatile uint32_t *hcchar;
    DmStm32H7UsbHostPipeResult result;
    uint32_t channel_config;
    uint32_t size_config;
    uint32_t interrupt;
    size_t length;
    size_t actual;

    if (!client || !pipe || !actual_length || !client->allocator ||
        !client->lease ||
        !dm_usb_host_channel_allocator_lease_active(client->allocator,
                                                     client->lease) ||
        client->lease->channel >= STM32H7_USB_HOST_CHANNELS ||
        (pipe->direction_in && (out_data || out_length ||
                                (!in_data && in_capacity))) ||
        (!pipe->direction_in && (in_data || in_capacity ||
                                 (!out_data && out_length)))) {
        return DM_STM32H7_USB_HOST_PIPE_INVALID;
    }
    length = pipe->direction_in ? in_capacity : out_length;
    result = dm_stm32h7_usb_host_pipe_encode(
        pipe, pid, length, &channel_config, &size_config);
    if (result != DM_STM32H7_USB_HOST_PIPE_OK) {
        return result;
    }

    hcint = dm_stm32h7_usb_host_pipe_reg(client,
                                         OTG_HCINT(client->lease->channel));
    hctsiz = dm_stm32h7_usb_host_pipe_reg(client,
                                           OTG_HCTSIZ(client->lease->channel));
    hcchar = dm_stm32h7_usb_host_pipe_reg(client,
                                           OTG_HCCHAR(client->lease->channel));
    *hcint = HCINT_ALL;
    if (!pipe->direction_in && length) {
        dm_stm32h7_usb_host_pipe_fifo_write(client, out_data, length);
    }
    *hctsiz = size_config;
    *hcchar = channel_config | HCCHAR_CHENA;
    do {
        interrupt = *hcint;
    } while (!(interrupt & (HCINT_CHHLTD | HCINT_NAK)));

    *actual_length = 0;
    if (interrupt & HCINT_XFRC) {
        actual = length - (*hctsiz & HCTSIZ_XFERSIZE_MASK);
        if (actual > length) {
            *hcint = HCINT_ALL;
            return DM_STM32H7_USB_HOST_PIPE_TRANSACTION_ERROR;
        }
        if (pipe->direction_in && actual) {
            dm_stm32h7_usb_host_pipe_fifo_read(client, in_data, actual);
        }
        *actual_length = actual;
        *hcint = HCINT_ALL;
        return DM_STM32H7_USB_HOST_PIPE_OK;
    }
    if (interrupt & HCINT_NAK) {
        /* The controller keeps bulk channels enabled for a later SOF retry.
         * This synchronous client is releasing its channel lease now, so
         * stop that attempt before another lease can reuse the channel. */
        *hcchar = channel_config | HCCHAR_CHDIS;
    }
    *hcint = HCINT_ALL;
    if (interrupt & HCINT_NAK) {
        return DM_STM32H7_USB_HOST_PIPE_NAK;
    }
    if (interrupt & HCINT_STALL) {
        return DM_STM32H7_USB_HOST_PIPE_STALL;
    }
    if (interrupt & HCINT_XACTERR) {
        return DM_STM32H7_USB_HOST_PIPE_TRANSACTION_ERROR;
    }
    return DM_STM32H7_USB_HOST_PIPE_TRANSACTION_ERROR;
}

DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_endpoint_transfer(
    DmStm32H7UsbHostPipeClient *client, DmUsbHostEndpointState *state,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length)
{
    const DmUsbHostPipe *pipe;
    DmUsbHostEndpointDataPid endpoint_pid;
    DmUsbHostEndpointStateResult state_result;
    DmStm32H7UsbHostPipeResult result;
    DmUsbHostEndpointCompletion completion;
    size_t requested_length;

    state_result = dm_usb_host_endpoint_state_prepare(state, &pipe,
                                                       &endpoint_pid);
    if (state_result == DM_USB_HOST_ENDPOINT_STATE_HALTED) {
        return DM_STM32H7_USB_HOST_PIPE_STALL;
    }
    if (state_result != DM_USB_HOST_ENDPOINT_STATE_OK) {
        return DM_STM32H7_USB_HOST_PIPE_INVALID;
    }
    result = dm_stm32h7_usb_host_pipe_transfer(
        client, pipe,
        endpoint_pid == DM_USB_HOST_ENDPOINT_PID_DATA0 ?
        DM_STM32H7_USB_HOST_PIPE_PID_DATA0 :
        DM_STM32H7_USB_HOST_PIPE_PID_DATA1,
        out_data, out_length, in_data, in_capacity, actual_length);
    if (result == DM_STM32H7_USB_HOST_PIPE_INVALID) {
        return result;
    }
    requested_length = pipe->direction_in ? in_capacity : out_length;
    switch (result) {
    case DM_STM32H7_USB_HOST_PIPE_OK:
        completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
        break;
    case DM_STM32H7_USB_HOST_PIPE_NAK:
        completion = DM_USB_HOST_ENDPOINT_NAK;
        break;
    case DM_STM32H7_USB_HOST_PIPE_STALL:
        completion = DM_USB_HOST_ENDPOINT_STALL;
        break;
    default:
        completion = DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR;
        break;
    }
    state_result = dm_usb_host_endpoint_state_complete(
        state, completion, requested_length,
        result == DM_STM32H7_USB_HOST_PIPE_OK ? *actual_length : 0);
    if (state_result == DM_USB_HOST_ENDPOINT_STATE_INVALID) {
        return DM_STM32H7_USB_HOST_PIPE_TRANSACTION_ERROR;
    }
    return result;
}

bool dm_stm32h7_usb_host_pipe_async_init(
    DmStm32H7UsbHostPipeAsync *async)
{
    return async && dm_usb_host_channel_completion_init(
                         &async->completion);
}

DmStm32H7UsbHostPipeAsyncStartResult
dm_stm32h7_usb_host_pipe_async_start(
    DmStm32H7UsbHostPipeAsync *async, uintptr_t base,
    DmUsbHostChannelAllocator *allocator, const DmUsbHostPipe *pipe,
    DmStm32H7UsbHostPipePid pid, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    DmUsbHostChannelCompletionToken *token)
{
    DmUsbHostChannelLease lease;
    DmStm32H7UsbHostPipeClient client;
    DmStm32H7UsbHostPipeResult encode_result;
    size_t length;
    uint32_t channel_config;
    uint32_t size_config;

    if (!async ||
        !dm_usb_host_channel_completion_initialized(&async->completion) ||
        !allocator || !pipe ||
        !token ||
        (dm_usb_host_channel_completion_state(&async->completion) !=
             DM_USB_HOST_CHANNEL_COMPLETION_IDLE &&
         dm_usb_host_channel_completion_state(&async->completion) !=
             DM_USB_HOST_CHANNEL_COMPLETION_COMPLETED &&
         dm_usb_host_channel_completion_state(&async->completion) !=
             DM_USB_HOST_CHANNEL_COMPLETION_CANCELLED) ||
        !dm_usb_host_endpoint_request_validate(
            pipe, out_data, out_length, in_data, in_capacity, &length)) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID;
    }
    encode_result = dm_stm32h7_usb_host_pipe_encode(
        pipe, pid, length, &channel_config, &size_config);
    if (encode_result != DM_STM32H7_USB_HOST_PIPE_OK) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID;
    }
    if (!dm_usb_host_channel_completion_begin(
            &async->completion, allocator, length, token)) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DEFERRED;
    }
    if (!dm_usb_host_channel_completion_lease(
            &async->completion, &lease)) {
        dm_usb_host_channel_completion_cancel(&async->completion, token, 0);
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID;
    }
    if (!dm_stm32h7_usb_host_pipe_client_init(
            &client, base, allocator, &lease)) {
        dm_usb_host_channel_completion_cancel(&async->completion, token, 0);
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID;
    }
    async->base = base;
    async->allocator = allocator;
    async->channel_config = channel_config;
    async->requested_length = length;
    async->in_data = in_data;
    async->direction_in = pipe->direction_in;

    *dm_stm32h7_usb_host_pipe_reg(
        &client, OTG_HCINT(lease.channel)) = HCINT_ALL;
    if (!pipe->direction_in && length) {
        dm_stm32h7_usb_host_pipe_fifo_write(&client, out_data, length);
    }
    *dm_stm32h7_usb_host_pipe_reg(
        &client, OTG_HCTSIZ(lease.channel)) = size_config;
    *dm_stm32h7_usb_host_pipe_reg(
        &client, OTG_HCCHAR(lease.channel)) =
        channel_config | HCCHAR_CHENA;
    return DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED;
}

DmStm32H7UsbHostPipeAsyncPollResult
dm_stm32h7_usb_host_pipe_async_poll(
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns)
{
    DmUsbHostChannelLease lease;
    DmStm32H7UsbHostPipeClient client;
    DmUsbHostEndpointCompletion completion;
    DmStm32H7UsbHostPipeAsyncPollResult poll_result;
    volatile uint32_t *hcint;
    volatile uint32_t *hctsiz;
    volatile uint32_t *hcchar;
    uint32_t interrupt;
    uint32_t remaining;
    size_t actual_length = 0;

    if (!async || !dm_usb_host_channel_completion_token_active(
                       &async->completion, token)) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID;
    }
    if (!dm_usb_host_channel_completion_lease(
            &async->completion, &lease)) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID;
    }
    if (!dm_stm32h7_usb_host_pipe_client_init(
            &client, async->base, async->allocator, &lease)) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID;
    }
    hcint = dm_stm32h7_usb_host_pipe_reg(
        &client, OTG_HCINT(lease.channel));
    hctsiz = dm_stm32h7_usb_host_pipe_reg(
        &client, OTG_HCTSIZ(lease.channel));
    hcchar = dm_stm32h7_usb_host_pipe_reg(
        &client, OTG_HCCHAR(lease.channel));
    interrupt = *hcint;
    if (!(interrupt & (HCINT_CHHLTD | HCINT_NAK))) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_PENDING;
    }

    if (interrupt & HCINT_XFRC) {
        remaining = *hctsiz & HCTSIZ_XFERSIZE_MASK;
        if (remaining > async->requested_length) {
            return DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID;
        }
        actual_length = async->requested_length - remaining;
        if (async->direction_in && actual_length) {
            if (!async->in_data) {
                return DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID;
            }
            dm_stm32h7_usb_host_pipe_fifo_read(
                &client, async->in_data, actual_length);
        }
        completion = DM_USB_HOST_ENDPOINT_ACCEPTED;
        poll_result = DM_STM32H7_USB_HOST_PIPE_ASYNC_OK;
    } else if (interrupt & HCINT_NAK) {
        *hcchar = async->channel_config | HCCHAR_CHDIS;
        completion = DM_USB_HOST_ENDPOINT_NAK;
        poll_result = DM_STM32H7_USB_HOST_PIPE_ASYNC_NAK;
    } else if (interrupt & HCINT_STALL) {
        completion = DM_USB_HOST_ENDPOINT_STALL;
        poll_result = DM_STM32H7_USB_HOST_PIPE_ASYNC_STALL;
    } else {
        completion = DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR;
        poll_result = DM_STM32H7_USB_HOST_PIPE_ASYNC_TRANSACTION_ERROR;
    }
    if (!dm_usb_host_channel_completion_complete(
            &async->completion, token, completion, actual_length,
            timestamp_ns)) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID;
    }
    *hcint = HCINT_ALL;
    return poll_result;
}

bool dm_stm32h7_usb_host_pipe_async_cancel(
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns)
{
    DmUsbHostChannelLease lease;
    DmStm32H7UsbHostPipeClient client;
    volatile uint32_t *hcint;
    volatile uint32_t *hcchar;

    if (!async || !dm_usb_host_channel_completion_token_active(
                       &async->completion, token)) {
        return false;
    }
    if (!dm_usb_host_channel_completion_lease(
            &async->completion, &lease)) {
        return false;
    }
    if (!dm_stm32h7_usb_host_pipe_client_init(
            &client, async->base, async->allocator, &lease)) {
        return false;
    }
    hcint = dm_stm32h7_usb_host_pipe_reg(
        &client, OTG_HCINT(lease.channel));
    hcchar = dm_stm32h7_usb_host_pipe_reg(
        &client, OTG_HCCHAR(lease.channel));
    *hcchar = async->channel_config | HCCHAR_CHDIS;
    *hcint = HCINT_ALL;
    return dm_usb_host_channel_completion_cancel(
        &async->completion, token, timestamp_ns);
}
