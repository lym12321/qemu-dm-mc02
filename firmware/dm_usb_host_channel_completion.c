/* Controller-independent asynchronous USB host channel completion state. */
#include "dm_usb_host_channel_completion.h"

typedef struct DmUsbHostChannelCompletionInternal {
    DmUsbHostChannelOperation operation;
    uint32_t generation;
    DmUsbHostChannelCompletionState state;
    size_t requested_length;
    size_t actual_length;
    uint64_t timestamp_ns;
    DmUsbHostEndpointCompletion completion;
    bool has_completion;
    bool initialized;
} DmUsbHostChannelCompletionInternal;

_Static_assert(sizeof(DmUsbHostChannelCompletionInternal) <=
                   DM_USB_HOST_CHANNEL_COMPLETION_STORAGE_SIZE,
               "completion storage is too small");

static DmUsbHostChannelCompletionInternal *dm_usb_host_channel_completion_data(
    DmUsbHostChannelCompletion *completion)
{
    return (DmUsbHostChannelCompletionInternal *)(void *)
        completion->storage.bytes;
}

static const DmUsbHostChannelCompletionInternal *
dm_usb_host_channel_completion_const_data(
    const DmUsbHostChannelCompletion *completion)
{
    return (const DmUsbHostChannelCompletionInternal *)(const void *)
        completion->storage.bytes;
}

static void dm_usb_host_channel_completion_clear_result(
    DmUsbHostChannelCompletionInternal *completion)
{
    completion->requested_length = 0;
    completion->actual_length = 0;
    completion->timestamp_ns = 0;
    completion->completion = DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR;
    completion->has_completion = false;
}

bool dm_usb_host_channel_completion_init(
    DmUsbHostChannelCompletion *completion)
{
    DmUsbHostChannelCompletionInternal *data;
    uint32_t generation;

    if (!completion) {
        return false;
    }
    data = dm_usb_host_channel_completion_data(completion);
    if (data->initialized &&
        data->state == DM_USB_HOST_CHANNEL_COMPLETION_PENDING) {
        return false;
    }
    generation = data->initialized ? data->generation : 0;
    /* Keep this module usable by the freestanding M7 smoke image.  A large
     * aggregate assignment may become an implicit memset at -O0. */
    data->operation.allocator = NULL;
    data->operation.lease.channel = 0;
    data->operation.lease.generation = 0;
    data->operation.lease.owner = NULL;
    data->operation.state = DM_USB_HOST_CHANNEL_OPERATION_IDLE;
    data->operation.initialized = false;
    data->generation = generation;
    data->state = DM_USB_HOST_CHANNEL_COMPLETION_IDLE;
    data->initialized = false;
    if (!dm_usb_host_channel_operation_init(&data->operation)) {
        return false;
    }
    data->state = DM_USB_HOST_CHANNEL_COMPLETION_IDLE;
    dm_usb_host_channel_completion_clear_result(data);
    data->initialized = true;
    return true;
}

bool dm_usb_host_channel_completion_initialized(
    const DmUsbHostChannelCompletion *completion)
{
    return completion &&
           dm_usb_host_channel_completion_const_data(completion)->initialized;
}

DmUsbHostChannelCompletionState dm_usb_host_channel_completion_state(
    const DmUsbHostChannelCompletion *completion)
{
    const DmUsbHostChannelCompletionInternal *data;

    if (!completion) {
        return DM_USB_HOST_CHANNEL_COMPLETION_IDLE;
    }
    data = dm_usb_host_channel_completion_const_data(completion);
    return data->initialized ? data->state :
           DM_USB_HOST_CHANNEL_COMPLETION_IDLE;
}

bool dm_usb_host_channel_completion_begin(
    DmUsbHostChannelCompletion *completion,
    DmUsbHostChannelAllocator *allocator, size_t requested_length,
    DmUsbHostChannelCompletionToken *token)
{
    DmUsbHostChannelCompletionInternal *data;

    if (!completion || !token ||
        !dm_usb_host_channel_completion_initialized(completion)) {
        return false;
    }
    data = dm_usb_host_channel_completion_data(completion);
    if ((data->state != DM_USB_HOST_CHANNEL_COMPLETION_IDLE &&
         data->state != DM_USB_HOST_CHANNEL_COMPLETION_COMPLETED &&
         data->state != DM_USB_HOST_CHANNEL_COMPLETION_CANCELLED) ||
        !dm_usb_host_channel_operation_begin(
            &data->operation, allocator)) {
        return false;
    }
    ++data->generation;
    if (!data->generation) {
        ++data->generation;
    }
    data->state = DM_USB_HOST_CHANNEL_COMPLETION_PENDING;
    data->requested_length = requested_length;
    data->actual_length = 0;
    data->timestamp_ns = 0;
    data->completion = DM_USB_HOST_ENDPOINT_TRANSACTION_ERROR;
    data->has_completion = false;
    *token = (DmUsbHostChannelCompletionToken) {
        .completion = completion,
        .generation = data->generation,
    };
    return true;
}

bool dm_usb_host_channel_completion_pending(
    const DmUsbHostChannelCompletion *completion)
{
    const DmUsbHostChannelCompletionInternal *data;

    if (!completion) {
        return false;
    }
    data = dm_usb_host_channel_completion_const_data(completion);
    return data->initialized &&
           data->state == DM_USB_HOST_CHANNEL_COMPLETION_PENDING &&
           dm_usb_host_channel_operation_pending(&data->operation);
}

bool dm_usb_host_channel_completion_token_active(
    const DmUsbHostChannelCompletion *completion,
    const DmUsbHostChannelCompletionToken *token)
{
    const DmUsbHostChannelCompletionInternal *data;

    if (!dm_usb_host_channel_completion_pending(completion) || !token ||
        token->completion != completion) {
        return false;
    }
    data = dm_usb_host_channel_completion_const_data(completion);
    return token->generation == data->generation;
}

bool dm_usb_host_channel_completion_lease(
    const DmUsbHostChannelCompletion *completion,
    DmUsbHostChannelLease *lease)
{
    const DmUsbHostChannelCompletionInternal *data;

    if (!dm_usb_host_channel_completion_pending(completion) || !lease) {
        return false;
    }
    data = dm_usb_host_channel_completion_const_data(completion);
    *lease = data->operation.lease;
    return true;
}

bool dm_usb_host_channel_completion_result(
    const DmUsbHostChannelCompletion *completion,
    DmUsbHostEndpointCompletion *result, size_t *actual_length,
    uint64_t *timestamp_ns)
{
    const DmUsbHostChannelCompletionInternal *data;

    if (!completion || !result || !actual_length || !timestamp_ns) {
        return false;
    }
    data = dm_usb_host_channel_completion_const_data(completion);
    if (!data->initialized || data->state !=
            DM_USB_HOST_CHANNEL_COMPLETION_COMPLETED ||
        !data->has_completion) {
        return false;
    }
    *result = data->completion;
    *actual_length = data->actual_length;
    *timestamp_ns = data->timestamp_ns;
    return true;
}

size_t dm_usb_host_channel_completion_actual_length(
    const DmUsbHostChannelCompletion *completion)
{
    return completion ?
           dm_usb_host_channel_completion_const_data(completion)->actual_length :
           0;
}

uint64_t dm_usb_host_channel_completion_timestamp_ns(
    const DmUsbHostChannelCompletion *completion)
{
    return completion ?
           dm_usb_host_channel_completion_const_data(completion)->timestamp_ns :
           0;
}

bool dm_usb_host_channel_completion_complete(
    DmUsbHostChannelCompletion *completion,
    const DmUsbHostChannelCompletionToken *token,
    DmUsbHostEndpointCompletion result, size_t actual_length,
    uint64_t timestamp_ns)
{
    DmUsbHostChannelCompletionInternal *data;

    if (!dm_usb_host_channel_completion_token_active(completion, token)) {
        return false;
    }
    data = dm_usb_host_channel_completion_data(completion);
    if (!dm_usb_host_endpoint_completion_validate(
            result, data->requested_length, actual_length) ||
        !dm_usb_host_channel_operation_complete(&data->operation)) {
        return false;
    }
    data->state = DM_USB_HOST_CHANNEL_COMPLETION_COMPLETED;
    data->actual_length = actual_length;
    data->timestamp_ns = timestamp_ns;
    data->completion = result;
    data->has_completion = true;
    return true;
}

bool dm_usb_host_channel_completion_cancel(
    DmUsbHostChannelCompletion *completion,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns)
{
    DmUsbHostChannelCompletionInternal *data;

    if (!dm_usb_host_channel_completion_token_active(completion, token)) {
        return false;
    }
    data = dm_usb_host_channel_completion_data(completion);
    if (!dm_usb_host_channel_operation_cancel(&data->operation)) {
        return false;
    }
    data->state = DM_USB_HOST_CHANNEL_COMPLETION_CANCELLED;
    data->actual_length = 0;
    data->timestamp_ns = timestamp_ns;
    data->has_completion = false;
    return true;
}
