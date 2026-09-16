/* Controller-independent NAK retry policy for one USB packet attempt. */
#include "dm_usb_host_retry.h"

bool dm_usb_host_retry_init(DmUsbHostRetryPolicy *policy,
                            DmUsbHostRetryConfig config)
{
    if (!policy) {
        return false;
    }
    policy->config = config;
    policy->started_at_ns = 0;
    policy->last_now_ns = 0;
    policy->retries = 0;
    policy->initialized = true;
    policy->active = false;
    policy->terminal = false;
    policy->has_now = false;
    return true;
}

bool dm_usb_host_retry_begin(DmUsbHostRetryPolicy *policy,
                             uint64_t started_at_ns)
{
    if (!policy || !policy->initialized || policy->active) {
        return false;
    }
    policy->started_at_ns = started_at_ns;
    policy->last_now_ns = started_at_ns;
    policy->retries = 0;
    policy->active = true;
    policy->terminal = false;
    policy->has_now = true;
    return true;
}

DmUsbHostRetryResult dm_usb_host_retry_check(DmUsbHostRetryPolicy *policy,
                                              uint64_t now_ns)
{
    uint64_t elapsed_ns;
    uint64_t since_last_ns;

    if (!policy || !policy->initialized || !policy->active ||
        policy->terminal) {
        return DM_USB_HOST_RETRY_INVALID;
    }

    since_last_ns = now_ns - policy->last_now_ns;
    /* Differences at or beyond half the counter range are ambiguous. */
    if (!policy->has_now || since_last_ns >= (UINT64_MAX / 2u + 1u)) {
        return DM_USB_HOST_RETRY_INVALID;
    }
    policy->last_now_ns = now_ns;
    elapsed_ns = now_ns - policy->started_at_ns;
    if (policy->config.timeout_ns != 0 &&
        elapsed_ns >= policy->config.timeout_ns) {
        policy->terminal = true;
        return DM_USB_HOST_RETRY_TIMEOUT;
    }
    return DM_USB_HOST_RETRY_RETRY;
}

DmUsbHostRetryResult dm_usb_host_retry_on_nak(DmUsbHostRetryPolicy *policy,
                                               uint64_t now_ns)
{
    DmUsbHostRetryResult result;

    result = dm_usb_host_retry_check(policy, now_ns);
    if (result != DM_USB_HOST_RETRY_RETRY) {
        return result;
    }
    if (policy->retries >= policy->config.max_retries) {
        policy->terminal = true;
        return DM_USB_HOST_RETRY_EXHAUSTED;
    }
    ++policy->retries;
    return DM_USB_HOST_RETRY_RETRY;
}

bool dm_usb_host_retry_complete(DmUsbHostRetryPolicy *policy)
{
    if (!policy || !policy->initialized || !policy->active ||
        policy->terminal) {
        return false;
    }
    policy->active = false;
    policy->retries = 0;
    policy->has_now = false;
    return true;
}

bool dm_usb_host_retry_finish(DmUsbHostRetryPolicy *policy)
{
    if (!policy || !policy->initialized || !policy->active ||
        !policy->terminal) {
        return false;
    }
    policy->active = false;
    policy->terminal = false;
    policy->retries = 0;
    policy->has_now = false;
    return true;
}

bool dm_usb_host_retry_reset(DmUsbHostRetryPolicy *policy)
{
    if (!policy || !policy->initialized) {
        return false;
    }
    policy->active = false;
    policy->terminal = false;
    policy->retries = 0;
    policy->has_now = false;
    return true;
}
