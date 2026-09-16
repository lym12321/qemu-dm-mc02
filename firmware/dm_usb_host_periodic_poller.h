/* Controller-independent periodic USB endpoint poll composition. */
#ifndef DM_USB_HOST_PERIODIC_POLLER_H
#define DM_USB_HOST_PERIODIC_POLLER_H

#include "dm_usb_host_endpoint_state.h"
#include "dm_usb_host_periodic_schedule.h"
#include "dm_usb_host_retry.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum DmUsbHostPeriodicSubmitResult {
    DM_USB_HOST_PERIODIC_SUBMIT_OK,
    /* No packet was submitted; retry at a later scheduler event. */
    DM_USB_HOST_PERIODIC_SUBMIT_DEFERRED,
    DM_USB_HOST_PERIODIC_SUBMIT_INVALID,
} DmUsbHostPeriodicSubmitResult;

typedef DmUsbHostPeriodicSubmitResult DmUsbHostPeriodicSubmit(
    void *opaque, const DmUsbHostPipe *pipe, DmUsbHostEndpointDataPid pid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion,
    size_t *actual_length);

typedef enum DmUsbHostPeriodicPollResult {
    DM_USB_HOST_PERIODIC_POLL_NOT_DUE,
    DM_USB_HOST_PERIODIC_POLL_DEFERRED,
    DM_USB_HOST_PERIODIC_POLL_SUBMITTED,
    DM_USB_HOST_PERIODIC_POLL_RETRY_EXHAUSTED,
    DM_USB_HOST_PERIODIC_POLL_RETRY_TIMEOUT,
    DM_USB_HOST_PERIODIC_POLL_HALTED,
    DM_USB_HOST_PERIODIC_POLL_INVALID,
} DmUsbHostPeriodicPollResult;

typedef struct DmUsbHostPeriodicPoller {
    DmUsbHostPeriodicSchedule schedule;
    DmUsbHostEndpointState *endpoint_state;
    DmUsbHostPeriodicSubmit *submit;
    void *submit_opaque;
    DmUsbHostRetryPolicy retry;
    bool retry_enabled;
    bool retry_pending;
    const uint8_t *retry_out_data;
    size_t retry_out_length;
    uint8_t *retry_in_data;
    size_t retry_in_capacity;
} DmUsbHostPeriodicPoller;

/* Bind an endpoint and optionally retain a finite virtual-time NAK policy. */
DmUsbHostPeriodicScheduleResult dm_usb_host_periodic_poller_init_with_retry(
    DmUsbHostPeriodicPoller *poller, DmUsbHostEndpointState *endpoint_state,
    DmUsbHostSpeed speed, uint64_t origin_ns,
    const DmUsbHostRetryConfig *retry_config,
    DmUsbHostPeriodicSubmit *submit, void *submit_opaque);

/* Bind one endpoint state and submit callback to a virtual periodic schedule. */
DmUsbHostPeriodicScheduleResult dm_usb_host_periodic_poller_init(
    DmUsbHostPeriodicPoller *poller, DmUsbHostEndpointState *endpoint_state,
    DmUsbHostSpeed speed, uint64_t origin_ns,
    DmUsbHostPeriodicSubmit *submit, void *submit_opaque);

/* Submit at most one packet when due and report its protocol completion. */
DmUsbHostPeriodicPollResult dm_usb_host_periodic_poller_poll(
    DmUsbHostPeriodicPoller *poller, uint64_t timestamp_ns,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *completion);

#endif /* DM_USB_HOST_PERIODIC_POLLER_H */
