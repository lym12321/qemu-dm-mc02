#include "dm_usb_host_channel_completion.h"

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

int main(void)
{
    const uint8_t channels[] = { 2, 5 };
    DmUsbHostChannelAllocator allocator;
    DmUsbHostChannelCompletion accepted = { 0 };
    DmUsbHostChannelCompletion cancelled = { 0 };
    DmUsbHostChannelCompletion exhausted = { 0 };
    DmUsbHostChannelCompletion migration_holder = { 0 };
    DmUsbHostChannelCompletionToken accepted_token;
    DmUsbHostChannelCompletionToken cancelled_token;
    DmUsbHostChannelCompletionToken exhausted_token;
    DmUsbHostChannelCompletionToken stale_token;
    DmUsbHostChannelCompletionToken reused_token;
    DmUsbHostChannelCompletionToken migration_holder_token;
    DmUsbHostChannelCompletionToken migrated_token;
    DmUsbHostChannelLease lease;
    DmUsbHostEndpointCompletion result;
    size_t actual_length;
    uint64_t timestamp_ns;

    if (expect(dm_usb_host_channel_allocator_init(
                   &allocator, channels, 2),
               "initialize allocator") ||
        expect(dm_usb_host_channel_completion_init(&accepted) &&
                   dm_usb_host_channel_completion_init(&cancelled) &&
                   dm_usb_host_channel_completion_init(&exhausted) &&
                   dm_usb_host_channel_completion_init(&migration_holder),
               "initialize completion records") ||
        expect(!dm_usb_host_channel_completion_pending(&accepted) &&
                   !dm_usb_host_channel_completion_lease(&accepted, &lease),
               "idle completion has no active lease") ||
        expect(dm_usb_host_channel_completion_begin(
                   &accepted, &allocator, 8, &accepted_token) &&
                   dm_usb_host_channel_completion_pending(&accepted) &&
                   dm_usb_host_channel_completion_lease(&accepted, &lease) &&
                   lease.channel == 2,
               "begin retains channel while controller is active") ||
        expect(dm_usb_host_channel_completion_begin(
                   &cancelled, &allocator, 0, &cancelled_token) &&
                   dm_usb_host_channel_completion_lease(&cancelled, &lease) &&
                   lease.channel == 5,
               "independent operation retains a second channel") ||
        expect(!dm_usb_host_channel_completion_begin(
                   &exhausted, &allocator, 1, &exhausted_token) &&
                   !dm_usb_host_channel_completion_pending(&exhausted),
               "channel exhaustion does not create a pending completion")) {
        return 1;
    }

    if (expect(!dm_usb_host_channel_completion_complete(
                   &accepted, &accepted_token,
                   DM_USB_HOST_ENDPOINT_ACCEPTED, 9, 100),
               "invalid actual length keeps completion pending") ||
        expect(dm_usb_host_channel_completion_complete(
                   &accepted, &accepted_token,
                   DM_USB_HOST_ENDPOINT_ACCEPTED, 8, 101) &&
                   dm_usb_host_channel_completion_state(&accepted) ==
                       DM_USB_HOST_CHANNEL_COMPLETION_COMPLETED &&
                   dm_usb_host_channel_completion_result(
                       &accepted, &result, &actual_length, &timestamp_ns) &&
                   result == DM_USB_HOST_ENDPOINT_ACCEPTED &&
                   actual_length == 8 && timestamp_ns == 101 &&
                   !dm_usb_host_channel_completion_pending(&accepted) &&
                   !allocator.entry[0].owner,
               "accepted completion records result and releases lease") ||
        expect(!dm_usb_host_channel_completion_complete(
                   &accepted, &accepted_token,
                   DM_USB_HOST_ENDPOINT_ACCEPTED, 0, 102),
               "terminal completion cannot complete twice") ||
        expect(dm_usb_host_channel_completion_cancel(
                   &cancelled, &cancelled_token, 103) &&
                   dm_usb_host_channel_completion_state(&cancelled) ==
                       DM_USB_HOST_CHANNEL_COMPLETION_CANCELLED &&
                   !dm_usb_host_channel_completion_result(
                       &cancelled, &result, &actual_length, &timestamp_ns) &&
                   dm_usb_host_channel_completion_actual_length(&cancelled) == 0 &&
                   dm_usb_host_channel_completion_timestamp_ns(&cancelled) == 103 &&
                   !dm_usb_host_channel_completion_pending(&cancelled) &&
                   !allocator.entry[1].owner,
               "cancel records timestamp and releases lease") ||
        expect(!dm_usb_host_channel_completion_cancel(
                   &cancelled, &cancelled_token, 104),
               "terminal cancellation cannot cancel twice")) {
        return 1;
    }

    stale_token = accepted_token;
    if (expect(dm_usb_host_channel_completion_begin(
                   &accepted, &allocator, 3, &reused_token),
               "terminal record can be reused") ||
        expect(stale_token.generation != reused_token.generation,
               "reuse creates a new generation token") ||
        expect(!dm_usb_host_channel_completion_complete(
                   &accepted, &stale_token,
                   DM_USB_HOST_ENDPOINT_ACCEPTED, 0, 104) &&
                   !dm_usb_host_channel_completion_cancel(
                       &accepted, &stale_token, 104) &&
                   dm_usb_host_channel_completion_pending(&accepted),
               "stale completion and cancellation cannot affect reuse") ||
        expect(dm_usb_host_channel_completion_complete(
                       &accepted, &reused_token,
                       DM_USB_HOST_ENDPOINT_NAK, 0, 105) &&
                   dm_usb_host_channel_completion_result(
                       &accepted, &result, &actual_length, &timestamp_ns) &&
                   result == DM_USB_HOST_ENDPOINT_NAK && actual_length == 0,
               "reused record accepts the current NAK")) {
        return 1;
    }

    stale_token = reused_token;
    if (expect(dm_usb_host_channel_completion_begin(
                   &migration_holder, &allocator, 0, &migration_holder_token) &&
                   dm_usb_host_channel_completion_lease(
                       &migration_holder, &lease) && lease.channel == 2,
               "occupy the first channel before migration") ||
        expect(dm_usb_host_channel_completion_begin(
                   &accepted, &allocator, 0, &migrated_token) &&
                   dm_usb_host_channel_completion_lease(
                       &accepted, &lease) && lease.channel == 5 &&
                   stale_token.generation != migrated_token.generation,
               "completion generation remains unique across channel migration") ||
        expect(!dm_usb_host_channel_completion_complete(
                   &accepted, &stale_token,
                   DM_USB_HOST_ENDPOINT_ACCEPTED, 0, 106) &&
                   dm_usb_host_channel_completion_pending(&accepted),
               "a delayed token cannot complete a migrated channel") ||
        expect(dm_usb_host_channel_completion_complete(
                   &migration_holder, &migration_holder_token,
                   DM_USB_HOST_ENDPOINT_ACCEPTED, 0, 107) &&
                   dm_usb_host_channel_completion_complete(
                       &accepted, &migrated_token,
                       DM_USB_HOST_ENDPOINT_ACCEPTED, 0, 108) &&
                   !allocator.entry[0].owner && !allocator.entry[1].owner,
               "migrated and holding completions release both channels")) {
        return 1;
    }

    puts("RESULT: USB host channel completion smoke passed");
    return 0;
}
