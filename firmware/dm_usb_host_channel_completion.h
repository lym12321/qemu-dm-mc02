/* Controller-independent asynchronous USB host channel completion state. */
#ifndef DM_USB_HOST_CHANNEL_COMPLETION_H
#define DM_USB_HOST_CHANNEL_COMPLETION_H

#include "dm_usb_host_channel_operation.h"
#include "dm_usb_host_endpoint_state.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_USB_HOST_CHANNEL_COMPLETION_STORAGE_SIZE 128u

typedef enum DmUsbHostChannelCompletionState {
    DM_USB_HOST_CHANNEL_COMPLETION_IDLE,
    DM_USB_HOST_CHANNEL_COMPLETION_PENDING,
    DM_USB_HOST_CHANNEL_COMPLETION_COMPLETED,
    DM_USB_HOST_CHANNEL_COMPLETION_CANCELLED,
} DmUsbHostChannelCompletionState;

typedef struct DmUsbHostChannelCompletion DmUsbHostChannelCompletion;

/* Copyable identity for one begin() generation and its later event. */
typedef struct DmUsbHostChannelCompletionToken {
    const DmUsbHostChannelCompletion *completion;
    uint32_t generation;
} DmUsbHostChannelCompletionToken;

typedef union DmUsbHostChannelCompletionStorage {
    max_align_t alignment;
    unsigned char bytes[DM_USB_HOST_CHANNEL_COMPLETION_STORAGE_SIZE];
} DmUsbHostChannelCompletionStorage;

struct DmUsbHostChannelCompletion {
    DmUsbHostChannelCompletionStorage storage;
};

/* Zero-initialize the object before its first init(); init() rejects pending reinit. */
bool dm_usb_host_channel_completion_init(
    DmUsbHostChannelCompletion *completion);

/* Query lifecycle state without exposing internal allocator ownership. */
bool dm_usb_host_channel_completion_initialized(
    const DmUsbHostChannelCompletion *completion);
DmUsbHostChannelCompletionState dm_usb_host_channel_completion_state(
    const DmUsbHostChannelCompletion *completion);

/* Acquire a channel and retain the caller-owned packet buffers. */
bool dm_usb_host_channel_completion_begin(
    DmUsbHostChannelCompletion *completion,
    DmUsbHostChannelAllocator *allocator, size_t requested_length,
    DmUsbHostChannelCompletionToken *token);

/* Query whether a controller event may still complete the operation. */
bool dm_usb_host_channel_completion_pending(
    const DmUsbHostChannelCompletion *completion);

/* Check that an event token still identifies this pending generation. */
bool dm_usb_host_channel_completion_token_active(
    const DmUsbHostChannelCompletion *completion,
    const DmUsbHostChannelCompletionToken *token);

/* Borrow the active channel lease for controller programming. */
/* Copy the active lease into caller storage; the internal lease is immutable. */
bool dm_usb_host_channel_completion_lease(
    const DmUsbHostChannelCompletion *completion,
    DmUsbHostChannelLease *lease);

/* Read a terminal controller result; cancelled records have no result. */
bool dm_usb_host_channel_completion_result(
    const DmUsbHostChannelCompletion *completion,
    DmUsbHostEndpointCompletion *result, size_t *actual_length,
    uint64_t *timestamp_ns);
size_t dm_usb_host_channel_completion_actual_length(
    const DmUsbHostChannelCompletion *completion);
uint64_t dm_usb_host_channel_completion_timestamp_ns(
    const DmUsbHostChannelCompletion *completion);

/* Commit one controller completion and release the channel lease. */
bool dm_usb_host_channel_completion_complete(
    DmUsbHostChannelCompletion *completion,
    const DmUsbHostChannelCompletionToken *token,
    DmUsbHostEndpointCompletion result, size_t actual_length,
    uint64_t timestamp_ns);

/* Cancel an in-flight operation and release its channel lease. */
bool dm_usb_host_channel_completion_cancel(
    DmUsbHostChannelCompletion *completion,
    const DmUsbHostChannelCompletionToken *token, uint64_t timestamp_ns);

#endif /* DM_USB_HOST_CHANNEL_COMPLETION_H */
