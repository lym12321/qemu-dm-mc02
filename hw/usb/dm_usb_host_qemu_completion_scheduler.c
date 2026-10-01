/* QEMU virtual-clock completion scheduler for STM32H7 host channels. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_qemu_completion_scheduler.h"
#include "qemu/timer.h"

static void dm_usb_host_qemu_completion_scheduler_timer(void *opaque)
{
    DmUsbHostQemuCompletionSchedulerEntry *entry = opaque;

    if (!entry->active) {
        return;
    }
    entry->active = false;
    g_assert(entry->timer);
    g_assert(entry->scheduler);
    g_assert(entry->scheduler->pending);
    --entry->scheduler->pending;
    dm_stm32h7_otg_host_complete_channel_with_token(
        entry->host, entry->channel, entry->completion_token,
        entry->completion, entry->actual_length);
}

static void dm_usb_host_qemu_completion_scheduler_clear_entry(
    DmUsbHostQemuCompletionScheduler *scheduler,
    DmUsbHostQemuCompletionSchedulerEntry *entry)
{
    timer_del(entry->timer);
    entry->active = false;
    entry->host = NULL;
    entry->completion_token = 0;
    entry->actual_length = 0;
    entry->completion = 0;
    --scheduler->pending;
}

static DmUsbHostQemuCompletionSchedulerEntry *
dm_usb_host_qemu_completion_scheduler_find(
    DmUsbHostQemuCompletionScheduler *scheduler,
    DmStm32H7OtgHost *host, unsigned channel)
{
    unsigned index;

    for (index = 0;
         index < DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_CAPACITY;
         ++index) {
        DmUsbHostQemuCompletionSchedulerEntry *entry =
            &scheduler->entry[index];

        if (entry->active && entry->host == host &&
            entry->channel == channel) {
            return entry;
        }
    }
    return NULL;
}

static DmUsbHostQemuCompletionSchedulerEntry *
dm_usb_host_qemu_completion_scheduler_free_entry(
    DmUsbHostQemuCompletionScheduler *scheduler)
{
    unsigned index;

    for (index = 0;
         index < DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_CAPACITY;
         ++index) {
        if (!scheduler->entry[index].active) {
            return &scheduler->entry[index];
        }
    }
    return NULL;
}

bool dm_usb_host_qemu_completion_scheduler_init(
    DmUsbHostQemuCompletionScheduler *scheduler, uint64_t delay_ns)
{
    unsigned index;

    if (!scheduler || scheduler->initialized) {
        return false;
    }
    *scheduler = (DmUsbHostQemuCompletionScheduler) {
        .delay_ns = delay_ns,
        .initialized = true,
    };
    for (index = 0;
         index < DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_CAPACITY;
         ++index) {
        DmUsbHostQemuCompletionSchedulerEntry *entry =
            &scheduler->entry[index];

        entry->scheduler = scheduler;
        entry->timer = timer_new_ns(
            QEMU_CLOCK_VIRTUAL,
            dm_usb_host_qemu_completion_scheduler_timer, entry);
    }
    return true;
}

void dm_usb_host_qemu_completion_scheduler_reset(
    DmUsbHostQemuCompletionScheduler *scheduler)
{
    unsigned index;

    if (!scheduler || !scheduler->initialized) {
        return;
    }
    for (index = 0;
         index < DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_CAPACITY;
         ++index) {
        DmUsbHostQemuCompletionSchedulerEntry *entry =
            &scheduler->entry[index];

        if (entry->active) {
            dm_usb_host_qemu_completion_scheduler_clear_entry(scheduler,
                                                               entry);
        }
    }
    scheduler->pending = 0;
}

void dm_usb_host_qemu_completion_scheduler_destroy(
    DmUsbHostQemuCompletionScheduler *scheduler)
{
    unsigned index;

    if (!scheduler || !scheduler->initialized) {
        return;
    }
    dm_usb_host_qemu_completion_scheduler_reset(scheduler);
    for (index = 0;
         index < DM_USB_HOST_QEMU_COMPLETION_SCHEDULER_CAPACITY;
         ++index) {
        timer_free(scheduler->entry[index].timer);
        scheduler->entry[index].timer = NULL;
        scheduler->entry[index].scheduler = NULL;
    }
    scheduler->initialized = false;
}

void dm_usb_host_qemu_completion_scheduler_schedule(
    void *opaque, DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length)
{
    DmUsbHostQemuCompletionScheduler *scheduler = opaque;

    if (!scheduler) {
        return;
    }
    dm_usb_host_qemu_completion_scheduler_schedule_after(
        scheduler, host, channel, completion_token, completion, actual_length,
        scheduler->delay_ns);
}

void dm_usb_host_qemu_completion_scheduler_schedule_after(
    DmUsbHostQemuCompletionScheduler *scheduler, DmStm32H7OtgHost *host,
    unsigned channel, uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length,
    uint64_t delay_ns)
{
    DmUsbHostQemuCompletionSchedulerEntry *entry;
    int64_t now;

    if (!scheduler || !scheduler->initialized || !host ||
        channel >= DM_STM32H7_OTG_HOST_CHANNELS || !completion_token) {
        return;
    }
    entry = dm_usb_host_qemu_completion_scheduler_find(
        scheduler, host, channel);
    if (!entry) {
        entry = dm_usb_host_qemu_completion_scheduler_free_entry(scheduler);
    }
    g_assert(entry);
    if (entry->active) {
        timer_del(entry->timer);
        --scheduler->pending;
    }
    entry->host = host;
    entry->channel = channel;
    entry->completion_token = completion_token;
    entry->completion = completion;
    entry->actual_length = actual_length;
    entry->active = true;
    ++scheduler->pending;
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    g_assert(now >= 0 && delay_ns <= (uint64_t)INT64_MAX - (uint64_t)now);
    timer_mod_ns(entry->timer, now + (int64_t)delay_ns);
}

void dm_usb_host_qemu_completion_scheduler_cancel(
    void *opaque, DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token)
{
    DmUsbHostQemuCompletionScheduler *scheduler = opaque;
    DmUsbHostQemuCompletionSchedulerEntry *entry;

    if (!scheduler || !scheduler->initialized || !completion_token) {
        return;
    }
    entry = dm_usb_host_qemu_completion_scheduler_find(
        scheduler, host, channel);
    if (!entry || entry->completion_token != completion_token) {
        return;
    }
    dm_usb_host_qemu_completion_scheduler_clear_entry(scheduler, entry);
}

unsigned dm_usb_host_qemu_completion_scheduler_pending(
    const DmUsbHostQemuCompletionScheduler *scheduler)
{
    return scheduler && scheduler->initialized ? scheduler->pending : 0;
}
