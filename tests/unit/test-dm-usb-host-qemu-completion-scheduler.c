/* Tests for the QEMU virtual-clock host-channel completion scheduler. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_qemu_completion_scheduler.h"
#include "qapi/error.h"
#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "sysemu/cpu-timers.h"

static int64_t scheduler_clock_ns;

int64_t cpu_get_clock(void)
{
    return scheduler_clock_ns;
}

typedef struct SchedulerHost {
    DmStm32H7OtgHost host;
    unsigned channel;
    unsigned irq_rises;
    unsigned irq_falls;
    unsigned *order;
    unsigned *order_length;
} SchedulerHost;

static void scheduler_host_irq(void *opaque, bool level)
{
    SchedulerHost *test = opaque;

    if (level) {
        ++test->irq_rises;
        if (test->order && test->order_length) {
            test->order[(*test->order_length)++] = test->channel;
        }
    } else {
        ++test->irq_falls;
    }
}

static void scheduler_host_cancel(void *opaque, DmStm32H7OtgHost *host,
                                   unsigned channel, uint64_t completion_token)
{
    dm_usb_host_qemu_completion_scheduler_cancel(
        opaque, host, channel, completion_token);
}

static void scheduler_host_init(SchedulerHost *test, unsigned channel,
                                 unsigned *order, unsigned *order_length,
                                 uint64_t completion_token)
{
    DmStm32H7OtgHostChannel *state;

    *test = (SchedulerHost) {
        .channel = channel,
        .order = order,
        .order_length = order_length,
    };
    dm_stm32h7_otg_host_init(&test->host, NULL, NULL,
                              scheduler_host_irq, test);
    state = &test->host.channel[channel];
    state->hcchar = DM_STM32H7_OTG_HCCHAR_CHENA | 64;
    state->hcintmsk = DM_STM32H7_OTG_HCINT_XFRC |
                      DM_STM32H7_OTG_HCINT_CHHLTD;
    state->hctsiz = 4 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    state->issued_length = 4;
    state->completion_token = completion_token;
    state->active = true;
    state->waiting_completion = true;
    test->host.gahbcfg = DM_STM32H7_OTG_GAHBCFG_GINT;
    test->host.gintmsk = DM_STM32H7_OTG_GINTSTS_HCINT;
    test->host.haintmsk = 1u << channel;
}

static void run_virtual_timers(void)
{
    while (qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL)) {
    }
}

static void run_virtual_timers_until_empty(
    DmUsbHostQemuCompletionScheduler *scheduler)
{
    int64_t delta;

    while (dm_usb_host_qemu_completion_scheduler_pending(scheduler)) {
        delta = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                           QEMU_TIMER_ATTR_ALL);
        g_assert_cmpint(delta, >=, 0);
        scheduler_clock_ns += delta;
        g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    }
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(scheduler), ==, 0);
}

static void test_zero_delay_completion_and_irq(void)
{
    DmUsbHostQemuCompletionScheduler scheduler = { 0 };
    SchedulerHost host;

    scheduler_clock_ns = 0;
    scheduler_host_init(&host, 0, NULL, NULL, 11);
    g_assert_true(dm_usb_host_qemu_completion_scheduler_init(&scheduler, 0));
    dm_usb_host_qemu_completion_scheduler_schedule(
        &scheduler, &host.host, host.channel, 11,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4);
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 1);
    g_assert_cmpuint(host.irq_rises, ==, 0);
    g_assert_true(timer_pending(scheduler.entry[0].timer));

    run_virtual_timers();

    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 0);
    g_assert_cmpuint(host.irq_rises, ==, 1);
    g_assert_false(host.host.channel[0].active);
    g_assert_true(host.host.channel[0].hcint & DM_STM32H7_OTG_HCINT_XFRC);
    g_assert_true(host.host.haint & 1u);
    g_assert_true(host.host.gintsts & DM_STM32H7_OTG_GINTSTS_HCINT);
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    dm_usb_host_qemu_completion_scheduler_destroy(&scheduler);
}

static void test_positive_delay_and_reset_cancel(void)
{
    DmUsbHostQemuCompletionScheduler scheduler = { 0 };
    SchedulerHost host;

    scheduler_clock_ns = 0;
    scheduler_host_init(&host, 0, NULL, NULL, 22);
    g_assert_true(dm_usb_host_qemu_completion_scheduler_init(
        &scheduler, NANOSECONDS_PER_SECOND));
    dm_usb_host_qemu_completion_scheduler_schedule(
        &scheduler, &host.host, host.channel, 22,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4);
    g_assert_true(timer_pending(scheduler.entry[0].timer));
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 1);
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(host.irq_rises, ==, 0);

    dm_usb_host_qemu_completion_scheduler_reset(&scheduler);
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 0);
    g_assert_false(timer_pending(scheduler.entry[0].timer));
    g_assert_cmpuint(host.irq_rises, ==, 0);
    run_virtual_timers();
    g_assert_cmpuint(host.irq_rises, ==, 0);
    dm_usb_host_qemu_completion_scheduler_destroy(&scheduler);
}

static void test_channel_cancel_removes_timer(void)
{
    DmUsbHostQemuCompletionScheduler scheduler = { 0 };
    SchedulerHost host;

    scheduler_clock_ns = 0;
    scheduler_host_init(&host, 0, NULL, NULL, 23);
    g_assert_true(dm_usb_host_qemu_completion_scheduler_init(
        &scheduler, NANOSECONDS_PER_SECOND));
    dm_stm32h7_otg_host_set_channel_cancel(
        &host.host, scheduler_host_cancel, &scheduler);
    dm_usb_host_qemu_completion_scheduler_schedule(
        &scheduler, &host.host, host.channel, 23,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4);
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 1);

    dm_usb_host_qemu_completion_scheduler_cancel(
        &scheduler, &host.host, host.channel, 24);
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 1);
    dm_stm32h7_otg_host_write(&host.host,
                              DM_STM32H7_OTG_HCCHAR(host.channel),
                              DM_STM32H7_OTG_HCCHAR_CHDIS, 10);
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 0);
    g_assert_false(timer_pending(scheduler.entry[0].timer));
    g_assert_cmpuint(host.host.channel[0].hcint, ==,
                     DM_STM32H7_OTG_HCINT_CHHLTD);
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    dm_usb_host_qemu_completion_scheduler_destroy(&scheduler);
}

static void test_multiple_deadlines_and_order(void)
{
    DmUsbHostQemuCompletionScheduler scheduler = { 0 };
    SchedulerHost hosts[3];
    unsigned order[3] = { 0 };
    unsigned order_length = 0;

    scheduler_clock_ns = 0;
    g_assert_true(dm_usb_host_qemu_completion_scheduler_init(&scheduler, 0));
    scheduler_host_init(&hosts[0], 0, order, &order_length, 31);
    scheduler_host_init(&hosts[1], 1, order, &order_length, 32);
    scheduler_host_init(&hosts[2], 2, order, &order_length, 33);
    dm_usb_host_qemu_completion_scheduler_schedule_after(
        &scheduler, &hosts[0].host, 0, 31,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4, 3000000);
    dm_usb_host_qemu_completion_scheduler_schedule_after(
        &scheduler, &hosts[1].host, 1, 32,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4, 1000000);
    dm_usb_host_qemu_completion_scheduler_schedule_after(
        &scheduler, &hosts[2].host, 2, 33,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4, 2000000);
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 3);

    run_virtual_timers_until_empty(&scheduler);

    g_assert_cmpuint(order_length, ==, 3);
    g_assert_cmpuint(order[0], ==, 1);
    g_assert_cmpuint(order[1], ==, 2);
    g_assert_cmpuint(order[2], ==, 0);
    dm_usb_host_qemu_completion_scheduler_destroy(&scheduler);
}

static void test_duplicate_channel_replaces_deadline(void)
{
    DmUsbHostQemuCompletionScheduler scheduler = { 0 };
    SchedulerHost host;
    DmStm32H7OtgHostChannel *state;

    scheduler_clock_ns = 0;
    scheduler_host_init(&host, 0, NULL, NULL, 51);
    g_assert_true(dm_usb_host_qemu_completion_scheduler_init(&scheduler, 0));
    dm_usb_host_qemu_completion_scheduler_schedule_after(
        &scheduler, &host.host, host.channel, 51,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4, 1000000);
    state = &host.host.channel[host.channel];
    state->completion_token = 52;
    dm_usb_host_qemu_completion_scheduler_schedule_after(
        &scheduler, &host.host, host.channel, 52,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4, 2000000);

    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 1);
    scheduler_clock_ns = 1000000;
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(host.irq_rises, ==, 0);
    scheduler_clock_ns = 2000000;
    g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(host.irq_rises, ==, 1);
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 0);
    dm_usb_host_qemu_completion_scheduler_destroy(&scheduler);
}

static void test_destroy_cancels_all_timers(void)
{
    DmUsbHostQemuCompletionScheduler scheduler = { 0 };
    SchedulerHost host;

    scheduler_clock_ns = 0;
    scheduler_host_init(&host, 0, NULL, NULL, 44);
    g_assert_true(dm_usb_host_qemu_completion_scheduler_init(&scheduler, 1));
    dm_usb_host_qemu_completion_scheduler_schedule(
        &scheduler, &host.host, host.channel, 44,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4);
    g_assert_true(qemu_clock_has_timers(QEMU_CLOCK_VIRTUAL));
    dm_usb_host_qemu_completion_scheduler_destroy(&scheduler);
    g_assert_false(scheduler.initialized);
    g_assert_cmpuint(
        dm_usb_host_qemu_completion_scheduler_pending(&scheduler), ==, 0);
    run_virtual_timers();
    g_assert_cmpuint(host.irq_rises, ==, 0);
}

int main(int argc, char **argv)
{
    qemu_init_main_loop(&error_abort);
    qemu_clock_enable(QEMU_CLOCK_VIRTUAL, true);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/qemu-completion-scheduler/zero-delay",
                    test_zero_delay_completion_and_irq);
    g_test_add_func("/dm-usb/qemu-completion-scheduler/reset-cancel",
                    test_positive_delay_and_reset_cancel);
    g_test_add_func("/dm-usb/qemu-completion-scheduler/channel-cancel",
                    test_channel_cancel_removes_timer);
    g_test_add_func("/dm-usb/qemu-completion-scheduler/order",
                    test_multiple_deadlines_and_order);
    g_test_add_func("/dm-usb/qemu-completion-scheduler/replace",
                    test_duplicate_channel_replaces_deadline);
    g_test_add_func("/dm-usb/qemu-completion-scheduler/destroy",
                    test_destroy_cancels_all_timers);
    return g_test_run();
}
