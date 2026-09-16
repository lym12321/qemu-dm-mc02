/* H723 host-channel IRQ dispatch for stable asynchronous PIO operations. */
#include "dm_stm32h7_usb_host_pipe_async_dispatch.h"

#define OTG_GAHBCFG    0x008u
#define OTG_GINTSTS    0x014u
#define OTG_GINTMSK    0x018u
#define OTG_HAINT      0x414u
#define OTG_HAINTMSK   0x418u

#define GAHBCFG_GINT   (1u << 0)
#define GINTSTS_HCINT  (1u << 25)

static volatile uint32_t *dm_stm32h7_usb_host_pipe_async_dispatch_reg(
    uintptr_t base, uint32_t offset)
{
    return (volatile uint32_t *)(base + offset);
}

static void dm_stm32h7_usb_host_pipe_async_dispatch_enter(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch)
{
    if (dispatch->enter) {
        dispatch->enter(dispatch->lock_opaque);
    }
}

static void dm_stm32h7_usb_host_pipe_async_dispatch_leave(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch)
{
    if (dispatch->leave) {
        dispatch->leave(dispatch->lock_opaque);
    }
}

static void dm_stm32h7_usb_host_pipe_async_dispatch_clear(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch, unsigned channel)
{
    dispatch->async[channel] = NULL;
    dispatch->token[channel] = (DmUsbHostChannelCompletionToken) { 0 };
}

static bool dm_stm32h7_usb_host_pipe_async_dispatch_register_locked(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token)
{
    DmUsbHostChannelLease lease;

    if (!dispatch->initialized || !async || !token ||
        async->base != dispatch->base ||
        !dm_usb_host_channel_completion_token_active(
            &async->completion, token) ||
        !dm_usb_host_channel_completion_lease(
            &async->completion, &lease) ||
        lease.channel >= DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY ||
        dispatch->async[lease.channel]) {
        return false;
    }
    dispatch->async[lease.channel] = async;
    dispatch->token[lease.channel] = *token;
    return true;
}

static bool dm_stm32h7_usb_host_pipe_async_dispatch_slot_matches(
    const DmStm32H7UsbHostPipeAsyncDispatch *dispatch, unsigned channel,
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token)
{
    return dispatch->async[channel] == async &&
           dispatch->token[channel].completion == token->completion &&
           dispatch->token[channel].generation == token->generation;
}

static bool dm_stm32h7_usb_host_pipe_async_dispatch_irq_enabled(
    const DmStm32H7UsbHostPipeAsyncDispatch *dispatch)
{
    return (*dm_stm32h7_usb_host_pipe_async_dispatch_reg(
                dispatch->base, OTG_GAHBCFG) & GAHBCFG_GINT) &&
           (*dm_stm32h7_usb_host_pipe_async_dispatch_reg(
                dispatch->base, OTG_GINTSTS) & GINTSTS_HCINT) &&
           (*dm_stm32h7_usb_host_pipe_async_dispatch_reg(
                dispatch->base, OTG_GINTMSK) & GINTSTS_HCINT);
}

static DmStm32H7UsbHostPipeAsyncDispatchResult
dm_stm32h7_usb_host_pipe_async_dispatch_handle_channel_locked(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch, unsigned channel,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns,
    uint32_t pending_channels)
{
    DmStm32H7UsbHostPipeAsync *async;
    DmStm32H7UsbHostPipeAsyncPollResult poll_result;

    if (channel >= DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY ||
        !token || !dispatch->async[channel]) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_INVALID;
    }
    async = dispatch->async[channel];
    if (!dm_stm32h7_usb_host_pipe_async_dispatch_slot_matches(
            dispatch, channel, async, token)) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_INVALID;
    }
    if (!dm_usb_host_channel_completion_token_active(
            &async->completion, token)) {
        dm_stm32h7_usb_host_pipe_async_dispatch_clear(dispatch, channel);
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_INVALID;
    }
    if (!(pending_channels & (1u << channel))) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_NO_EVENT;
    }
    poll_result = dm_stm32h7_usb_host_pipe_async_poll(
        async, token, timestamp_ns);
    if (poll_result == DM_STM32H7_USB_HOST_PIPE_ASYNC_PENDING) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_PENDING;
    }
    if (poll_result == DM_STM32H7_USB_HOST_PIPE_ASYNC_POLL_INVALID) {
        /* A malformed terminal register state must not strand the lease. */
        if (!dm_stm32h7_usb_host_pipe_async_cancel(
                async, token, timestamp_ns)) {
            dm_usb_host_channel_completion_cancel(
                &async->completion, token, timestamp_ns);
        }
        dm_stm32h7_usb_host_pipe_async_dispatch_clear(dispatch, channel);
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_INVALID;
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_clear(dispatch, channel);
    return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_COMPLETED;
}

bool dm_stm32h7_usb_host_pipe_async_dispatch_init(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch, uintptr_t base,
    DmStm32H7UsbHostPipeAsyncDispatchLock *enter,
    DmStm32H7UsbHostPipeAsyncDispatchLock *leave, void *lock_opaque)
{
    unsigned channel;

    if (!dispatch || (!!enter != !!leave)) {
        return false;
    }
    if (dispatch->initialized) {
        for (channel = 0;
             channel < DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY;
             ++channel) {
            if (dispatch->async[channel]) {
                return false;
            }
        }
    }
    dispatch->base = base;
    dispatch->enter = enter;
    dispatch->leave = leave;
    dispatch->lock_opaque = lock_opaque;
    dispatch->initialized = true;
    for (channel = 0;
         channel < DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY;
         ++channel) {
        dm_stm32h7_usb_host_pipe_async_dispatch_clear(dispatch, channel);
    }
    return true;
}

bool dm_stm32h7_usb_host_pipe_async_dispatch_register(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token)
{
    bool registered;

    if (!dispatch) {
        return false;
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_enter(dispatch);
    registered = dm_stm32h7_usb_host_pipe_async_dispatch_register_locked(
        dispatch, async, token);
    dm_stm32h7_usb_host_pipe_async_dispatch_leave(dispatch);
    return registered;
}

DmStm32H7UsbHostPipeAsyncStartResult
dm_stm32h7_usb_host_pipe_async_dispatch_start(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    DmStm32H7UsbHostPipeAsync *async, uintptr_t base,
    DmUsbHostChannelAllocator *allocator, const DmUsbHostPipe *pipe,
    DmStm32H7UsbHostPipePid pid, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    DmUsbHostChannelCompletionToken *token)
{
    DmStm32H7UsbHostPipeAsyncStartResult result;

    if (!dispatch || !dispatch->initialized || base != dispatch->base) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID;
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_enter(dispatch);
    result = dm_stm32h7_usb_host_pipe_async_start(
        async, base, allocator, pipe, pid, out_data, out_length, in_data,
        in_capacity, token);
    if (result == DM_STM32H7_USB_HOST_PIPE_ASYNC_STARTED &&
        !dm_stm32h7_usb_host_pipe_async_dispatch_register_locked(
            dispatch, async, token)) {
        dm_stm32h7_usb_host_pipe_async_cancel(async, token, 0);
        result = DM_STM32H7_USB_HOST_PIPE_ASYNC_START_INVALID;
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_leave(dispatch);
    return result;
}

DmStm32H7UsbHostPipeAsyncDispatchResult
dm_stm32h7_usb_host_pipe_async_dispatch_handle_channel(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch, unsigned channel,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns)
{
    uint32_t pending_channels;
    DmStm32H7UsbHostPipeAsyncDispatchResult result;

    if (!dispatch || !dispatch->initialized || channel >=
            DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY || !token) {
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_INVALID;
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_enter(dispatch);
    if (!dm_stm32h7_usb_host_pipe_async_dispatch_irq_enabled(dispatch)) {
        dm_stm32h7_usb_host_pipe_async_dispatch_leave(dispatch);
        return DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_NO_EVENT;
    }
    pending_channels =
        *dm_stm32h7_usb_host_pipe_async_dispatch_reg(
            dispatch->base, OTG_HAINT) &
        *dm_stm32h7_usb_host_pipe_async_dispatch_reg(
            dispatch->base, OTG_HAINTMSK);
    result = dm_stm32h7_usb_host_pipe_async_dispatch_handle_channel_locked(
        dispatch, channel, token, timestamp_ns, pending_channels);
    dm_stm32h7_usb_host_pipe_async_dispatch_leave(dispatch);
    return result;
}

unsigned dm_stm32h7_usb_host_pipe_async_dispatch_handle(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch, uint64_t timestamp_ns)
{
    uint32_t pending_channels;
    unsigned channel;
    unsigned completed = 0;

    if (!dispatch || !dispatch->initialized) {
        return 0;
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_enter(dispatch);
    if (!dm_stm32h7_usb_host_pipe_async_dispatch_irq_enabled(dispatch)) {
        dm_stm32h7_usb_host_pipe_async_dispatch_leave(dispatch);
        return 0;
    }
    pending_channels =
        *dm_stm32h7_usb_host_pipe_async_dispatch_reg(
            dispatch->base, OTG_HAINT) &
        *dm_stm32h7_usb_host_pipe_async_dispatch_reg(
            dispatch->base, OTG_HAINTMSK);
    for (channel = 0;
         channel < DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY;
         ++channel) {
        DmUsbHostChannelCompletionToken token;
        DmStm32H7UsbHostPipeAsyncDispatchResult result;

        if (!(pending_channels & (1u << channel)) ||
            !dispatch->async[channel]) {
            continue;
        }
        token = dispatch->token[channel];
        result = dm_stm32h7_usb_host_pipe_async_dispatch_handle_channel_locked(
            dispatch, channel, &token, timestamp_ns, pending_channels);
        if (result == DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_COMPLETED) {
            ++completed;
        }
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_leave(dispatch);
    return completed;
}

bool dm_stm32h7_usb_host_pipe_async_dispatch_cancel(
    DmStm32H7UsbHostPipeAsyncDispatch *dispatch,
    DmStm32H7UsbHostPipeAsync *async,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns)
{
    DmUsbHostChannelLease lease;
    bool cancelled = false;

    if (!dispatch || !async || !token) {
        return false;
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_enter(dispatch);
    if (dispatch->initialized &&
        dm_usb_host_channel_completion_lease(&async->completion, &lease) &&
        lease.channel < DM_STM32H7_USB_HOST_PIPE_ASYNC_DISPATCH_CAPACITY &&
        dm_stm32h7_usb_host_pipe_async_dispatch_slot_matches(
            dispatch, lease.channel, async, token)) {
        cancelled = dm_stm32h7_usb_host_pipe_async_cancel(
            async, token, timestamp_ns);
        if (cancelled) {
            dm_stm32h7_usb_host_pipe_async_dispatch_clear(
                dispatch, lease.channel);
        }
    }
    dm_stm32h7_usb_host_pipe_async_dispatch_leave(dispatch);
    return cancelled;
}
