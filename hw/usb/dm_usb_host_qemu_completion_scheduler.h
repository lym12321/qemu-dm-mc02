/* QEMU virtual-clock completion scheduler for STM32H7 host channels. */
#ifndef HW_USB_DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_H
#define HW_USB_DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_H

#include "hw/usb/dm_usb_host_channel_transport.h"
#include "qemu/timer.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_CAPACITY \
    DM_STM32H7_OTG_HOST_CHANNELS

typedef struct DmUsbHostQemuCompletionScheduler
    DmUsbHostQemuCompletionScheduler;

typedef struct DmUsbHostQemuCompletionSchedulerEntry {
    QEMUTimer *timer;
    DmUsbHostQemuCompletionScheduler *scheduler;
    DmStm32H7OtgHost *host;
    unsigned channel;
    uint64_t completion_token;
    DmStm32H7OtgHostChannelCompletion completion;
    uint32_t actual_length;
    bool active;
} DmUsbHostQemuCompletionSchedulerEntry;

struct DmUsbHostQemuCompletionScheduler {
    uint64_t delay_ns;
    unsigned pending;
    bool initialized;
    DmUsbHostQemuCompletionSchedulerEntry entry[
        DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_CAPACITY];
};

/* The scheduler owns its QEMU timers until destroy(). */
bool dm_usb_host_qemu_completion_scheduler_init(
    DmUsbHostQemuCompletionScheduler *scheduler, uint64_t delay_ns);
void dm_usb_host_qemu_completion_scheduler_reset(
    DmUsbHostQemuCompletionScheduler *scheduler);
void dm_usb_host_qemu_completion_scheduler_destroy(
    DmUsbHostQemuCompletionScheduler *scheduler);

/* Compatible with DmUsbHostChannelTransport's optional scheduler callback. */
void dm_usb_host_qemu_completion_scheduler_schedule(
    void *opaque, DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length);

/* Cancel the matching pending virtual timer, if it is still current. */
void dm_usb_host_qemu_completion_scheduler_cancel(
    void *opaque, DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token);

/* Schedule one completion with an explicit virtual delay for fixtures. */
void dm_usb_host_qemu_completion_scheduler_schedule_after(
    DmUsbHostQemuCompletionScheduler *scheduler, DmStm32H7OtgHost *host,
    unsigned channel, uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length,
    uint64_t delay_ns);

unsigned dm_usb_host_qemu_completion_scheduler_pending(
    const DmUsbHostQemuCompletionScheduler *scheduler);

#endif /* HW_USB_DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_H */
