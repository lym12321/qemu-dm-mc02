#include "dm_usb_host_retry.h"

#include <stdbool.h>
#include <stdint.h>
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
    DmUsbHostRetryPolicy policy;
    const DmUsbHostRetryConfig unlimited = { .max_retries = 0,
                                             .timeout_ns = 0 };

    if (expect(!dm_usb_host_retry_begin(NULL, 0), "reject begin with NULL") ||
        expect(!dm_usb_host_retry_finish(NULL), "reject finish with NULL") ||
        expect(!dm_usb_host_retry_complete(NULL),
               "reject complete with NULL") ||
        expect(dm_usb_host_retry_check(NULL, 0) == DM_USB_HOST_RETRY_INVALID,
               "reject check with NULL") ||
        expect(dm_usb_host_retry_on_nak(NULL, 0) ==
                   DM_USB_HOST_RETRY_INVALID,
               "reject NAK with NULL") ||
        expect(!dm_usb_host_retry_reset(NULL), "reject reset with NULL")) {
        return 1;
    }

    if (expect(dm_usb_host_retry_init(&policy, unlimited), "initialize") ||
        expect(dm_usb_host_retry_on_nak(&policy, 0) ==
                   DM_USB_HOST_RETRY_INVALID,
               "reject NAK before begin") ||
        expect(dm_usb_host_retry_check(&policy, 0) ==
                   DM_USB_HOST_RETRY_INVALID,
               "reject check before begin") ||
        expect(dm_usb_host_retry_finish(&policy) == false,
               "reject finish before begin") ||
        expect(dm_usb_host_retry_begin(&policy, 100), "begin zero budget") ||
        expect(dm_usb_host_retry_check(&policy, 100) ==
                   DM_USB_HOST_RETRY_RETRY,
               "check active attempt") ||
        expect(dm_usb_host_retry_on_nak(&policy, 100) ==
                   DM_USB_HOST_RETRY_EXHAUSTED,
               "zero budget exhausts immediately") ||
        expect(dm_usb_host_retry_on_nak(&policy, 100) ==
                   DM_USB_HOST_RETRY_INVALID,
               "reject NAK after exhausted") ||
        expect(dm_usb_host_retry_begin(&policy, 100) == false,
               "reject begin while terminal") ||
        expect(dm_usb_host_retry_finish(&policy), "finish exhausted attempt")) {
        return 1;
    }

    if (expect(dm_usb_host_retry_init(
                   &policy, (DmUsbHostRetryConfig) { .max_retries = 2 }),
               "initialize retry budget") ||
        expect(dm_usb_host_retry_begin(&policy, 1000), "begin retry attempt") ||
        expect(dm_usb_host_retry_on_nak(&policy, 1000) ==
                   DM_USB_HOST_RETRY_RETRY,
               "first NAK retries") ||
        expect(dm_usb_host_retry_on_nak(&policy, 1001) ==
                   DM_USB_HOST_RETRY_RETRY,
               "second NAK retries") ||
        expect(!dm_usb_host_retry_finish(&policy),
               "finish cannot end retryable attempt") ||
        expect(dm_usb_host_retry_on_nak(&policy, 1002) ==
                   DM_USB_HOST_RETRY_EXHAUSTED,
               "third NAK exhausts") ||
        expect(dm_usb_host_retry_finish(&policy), "finish exhausted attempt") ||
        expect(dm_usb_host_retry_begin(&policy, 2000), "restart after finish")) {
        return 1;
    }
    if (expect(dm_usb_host_retry_reset(&policy), "reset active attempt") ||
        expect(dm_usb_host_retry_on_nak(&policy, 2000) ==
                   DM_USB_HOST_RETRY_INVALID,
               "reset clears active attempt") ||
        expect(dm_usb_host_retry_begin(&policy, 3000), "begin after reset") ||
        expect(dm_usb_host_retry_complete(&policy), "complete successful attempt") ||
        expect(!dm_usb_host_retry_finish(&policy),
               "finish cannot end completed attempt") ||
        expect(dm_usb_host_retry_begin(&policy, 4000),
               "begin after successful completion") ||
        expect(dm_usb_host_retry_reset(&policy), "cancel restarted attempt")) {
        return 1;
    }

    if (expect(dm_usb_host_retry_init(
                   &policy, (DmUsbHostRetryConfig) { .max_retries = 5,
                                                    .timeout_ns = 10 }),
               "initialize timeout policy") ||
        expect(dm_usb_host_retry_begin(&policy, 100), "begin timeout attempt") ||
        expect(dm_usb_host_retry_check(&policy, 109) ==
                   DM_USB_HOST_RETRY_RETRY,
               "check before timeout") ||
        expect(dm_usb_host_retry_check(&policy, 110) ==
                   DM_USB_HOST_RETRY_TIMEOUT,
               "check timeout without NAK") ||
        expect(dm_usb_host_retry_finish(&policy), "finish timeout attempt") ||
        expect(dm_usb_host_retry_begin(&policy, 1000),
               "begin timestamp validation attempt") ||
        expect(dm_usb_host_retry_check(&policy, 999) ==
                   DM_USB_HOST_RETRY_INVALID,
               "reject invalid backward timestamp") ||
        expect(dm_usb_host_retry_check(
                   &policy, UINT64_C(0x80000000000003e8)) ==
                   DM_USB_HOST_RETRY_INVALID,
               "reject timestamp rollback beyond half range") ||
        expect(dm_usb_host_retry_on_nak(&policy, 1000) ==
                   DM_USB_HOST_RETRY_RETRY,
               "valid timestamp remains usable") ||
        expect(dm_usb_host_retry_check(&policy, 1001) ==
                   DM_USB_HOST_RETRY_RETRY,
               "forward timestamp recovers after invalid timestamp") ||
        expect(dm_usb_host_retry_reset(&policy), "cancel active attempt") ||
        expect(policy.config.max_retries == 5 &&
                   policy.config.timeout_ns == 10 && policy.retries == 0 &&
                   !policy.active && !policy.terminal && !policy.has_now,
               "reset preserves limits and clears attempt state") ||
        expect(dm_usb_host_retry_init(
                   &policy, (DmUsbHostRetryConfig) { .max_retries = 1,
                                                    .timeout_ns = 10 }),
               "initialize wrap policy") ||
        expect(dm_usb_host_retry_begin(&policy, UINT64_MAX - 4),
               "begin near timestamp wrap") ||
        expect(dm_usb_host_retry_on_nak(&policy, 3) ==
                   DM_USB_HOST_RETRY_RETRY,
               "wrap-safe elapsed retry") ||
        expect(dm_usb_host_retry_on_nak(&policy, 6) ==
                   DM_USB_HOST_RETRY_TIMEOUT,
               "wrap-safe elapsed timeout") ||
        expect(dm_usb_host_retry_finish(&policy), "finish wrapped attempt")) {
        return 1;
    }

    puts("RESULT: USB host retry smoke passed");
    return 0;
}
