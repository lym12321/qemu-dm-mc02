/* Controller-independent NAK retry policy for one USB packet attempt. */
#ifndef DM_USB_HOST_RETRY_H
#define DM_USB_HOST_RETRY_H

#include <stdbool.h>
#include <stdint.h>

typedef struct DmUsbHostRetryConfig {
    uint32_t max_retries;
    uint64_t timeout_ns;
} DmUsbHostRetryConfig;

typedef enum DmUsbHostRetryResult {
    DM_USB_HOST_RETRY_RETRY,
    DM_USB_HOST_RETRY_EXHAUSTED,
    DM_USB_HOST_RETRY_TIMEOUT,
    DM_USB_HOST_RETRY_INVALID,
} DmUsbHostRetryResult;

typedef struct DmUsbHostRetryPolicy {
    DmUsbHostRetryConfig config;
    uint64_t started_at_ns;
    uint64_t last_now_ns;
    uint32_t retries;
    bool initialized;
    bool active;
    bool terminal;
    bool has_now;
} DmUsbHostRetryPolicy;

/* Initialize a policy. Both zero configuration values are valid. */
bool dm_usb_host_retry_init(DmUsbHostRetryPolicy *policy,
                            DmUsbHostRetryConfig config);

/* Start the first submission of a packet; it does not consume retry budget. */
bool dm_usb_host_retry_begin(DmUsbHostRetryPolicy *policy,
                             uint64_t started_at_ns);

/* Check virtual time; now_ns must advance monotonically modulo uint64_t. */
DmUsbHostRetryResult dm_usb_host_retry_check(DmUsbHostRetryPolicy *policy,
                                               uint64_t now_ns);

/* Classify a NAK at now_ns and consume one retry when returning RETRY. */
DmUsbHostRetryResult dm_usb_host_retry_on_nak(DmUsbHostRetryPolicy *policy,
                                               uint64_t now_ns);

/* Complete a successful packet attempt. */
bool dm_usb_host_retry_complete(DmUsbHostRetryPolicy *policy);

/* Finish an attempt after the policy has reached a terminal result. */
bool dm_usb_host_retry_finish(DmUsbHostRetryPolicy *policy);

/* Abort the current attempt and retain the configured policy limits. */
bool dm_usb_host_retry_reset(DmUsbHostRetryPolicy *policy);

#endif /* DM_USB_HOST_RETRY_H */
