/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "qemu/main-loop.h"
#include "qemu/thread.h"
#include "qemu/timer.h"
#include "sysemu/cpu-timers.h"

static int64_t test_clock;
static unsigned main_notifications;
static unsigned callback_notifications;
static bool test_cpu_idle;

int use_icount;

int64_t icount_get_raw(void)
{
    /* Clock replay can sample this; the test executes no guest instructions. */
    return 0;
}

void icount_notify_exit(void)
{
}

/* Model sleep=off warp at the CPU boundary. The real timer queue selects the
 * deadline, and asserting the synthetic IRQ makes the CPU runnable. */
void icount_start_warp_timer(void)
{
    int64_t deadline;

    if (!test_cpu_idle) {
        return;
    }
    deadline = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                         ~QEMU_TIMER_ATTR_EXTERNAL);
    if (deadline > 0) {
        test_clock += deadline;
    }
}

int64_t cpus_get_virtual_clock(void)
{
    return test_clock;
}

static void notify_main(void *opaque, QEMUClockType type)
{
    g_assert_cmpint(type, ==, QEMU_CLOCK_VIRTUAL);
    main_notifications++;
    if (qemu_clock_in_main_loop_timer_callback(type)) {
        callback_notifications++;
    }
}

static void notify_other(void *opaque, QEMUClockType type)
{
    unsigned *notifications = opaque;

    (*notifications)++;
}

static void count_callback(void *opaque)
{
    unsigned *callbacks = opaque;

    (*callbacks)++;
}

static void test_notify_expired(void)
{
    QEMUTimerListGroup due_group, future_group, empty_group;
    unsigned due = 0, future = 0, empty = 0, callbacks = 0;
    QEMUTimer *main_timer, *due_timer, *future_timer;

    test_clock = 100;
    timerlistgroup_init(&due_group, notify_other, &due);
    timerlistgroup_init(&future_group, notify_other, &future);
    timerlistgroup_init(&empty_group, notify_other, &empty);
    main_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, count_callback, &callbacks);
    due_timer = timer_new_full(&due_group, QEMU_CLOCK_VIRTUAL, SCALE_NS, 0,
                              count_callback, &callbacks);
    future_timer = timer_new_full(&future_group, QEMU_CLOCK_VIRTUAL, SCALE_NS, 0,
                                 count_callback, &callbacks);
    timer_mod(main_timer, 100);
    timer_mod(due_timer, 100);
    timer_mod(future_timer, 101);
    main_notifications = due = future = empty = 0;

    qemu_clock_notify_expired(QEMU_CLOCK_VIRTUAL, main_timer->timer_list);
    g_assert_cmpuint(main_notifications, ==, 0);
    g_assert_cmpuint(due, ==, 1);
    g_assert_cmpuint(future, ==, 0);
    g_assert_cmpuint(empty, ==, 0);
    g_assert_cmpuint(callbacks, ==, 0);

    test_clock = 101;
    qemu_clock_notify_expired(QEMU_CLOCK_VIRTUAL, NULL);
    g_assert_cmpuint(main_notifications, ==, 1);
    g_assert_cmpuint(due, ==, 2);
    g_assert_cmpuint(future, ==, 1);
    g_assert_cmpuint(empty, ==, 0);

    timer_del(main_timer);
    timer_del(due_timer);
    timer_del(future_timer);
    timer_free(main_timer);
    timer_free(due_timer);
    timer_free(future_timer);
    timerlistgroup_deinit(&due_group);
    timerlistgroup_deinit(&future_group);
    timerlistgroup_deinit(&empty_group);
}

typedef struct CallbackTest {
    QEMUTimer *earlier;
    QEMUTimer *nested;
    unsigned callbacks;
    bool async;
} CallbackTest;

static void *rearm_from_other_thread(void *opaque)
{
    CallbackTest *test = opaque;

    g_assert_false(qemu_clock_in_main_loop_timer_callback(QEMU_CLOCK_VIRTUAL));
    timer_mod(test->earlier, 150);
    return NULL;
}

static void nested_callback(void *opaque)
{
    CallbackTest *test = opaque;

    g_assert_false(qemu_clock_in_main_loop_timer_callback(QEMU_CLOCK_VIRTUAL));
    test->callbacks++;
}

static void main_callback(void *opaque)
{
    CallbackTest *test = opaque;

    g_assert_true(qemu_clock_in_main_loop_timer_callback(QEMU_CLOCK_VIRTUAL));
    g_assert_false(qemu_clock_in_main_loop_timer_callback(QEMU_CLOCK_REALTIME));
    if (test->async) {
        QemuThread thread;

        qemu_thread_create(&thread, "timer-rearm", rearm_from_other_thread, test,
                           QEMU_THREAD_JOINABLE);
        qemu_thread_join(&thread);
        g_assert_cmpuint(callback_notifications, ==, 0);
    } else {
        timer_mod(test->earlier, 150);
        g_assert_cmpuint(callback_notifications, ==, 1);
    }
    g_assert_cmpuint(main_notifications, ==, 1);
    g_assert_cmpint(qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                              QEMU_TIMER_ATTR_ALL), ==, 0);
    /* A nested AioContext callback must not inherit the main-list scope. */
    g_assert_true(timerlist_run_timers(test->nested->timer_list));
    g_assert_true(qemu_clock_in_main_loop_timer_callback(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpint(qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                              QEMU_TIMER_ATTR_ALL), ==, 50);
    test->callbacks++;
}

static void test_callback_rearm(gconstpointer opaque)
{
    CallbackTest test = { .async = GPOINTER_TO_INT(opaque) };
    QEMUTimerListGroup nested_group;
    unsigned nested_notifications = 0;
    QEMUTimer *main_timer;

    test_clock = 100;
    timerlistgroup_init(&nested_group, notify_other, &nested_notifications);
    test.earlier = timer_new_ns(QEMU_CLOCK_VIRTUAL, count_callback,
                                &test.callbacks);
    test.nested = timer_new_full(&nested_group, QEMU_CLOCK_VIRTUAL, SCALE_NS, 0,
                                 nested_callback, &test);
    main_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, main_callback, &test);
    timer_mod(test.earlier, 500);
    timer_mod(test.nested, 100);
    timer_mod(main_timer, 100);
    main_notifications = callback_notifications = 0;

    g_assert_false(qemu_clock_in_main_loop_timer_callback(QEMU_CLOCK_VIRTUAL));
    g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_false(qemu_clock_in_main_loop_timer_callback(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(test.callbacks, ==, 2);
    test_clock = 150;
    g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(test.callbacks, ==, 3);

    /* Rearming outside callback scope still requests a new deadline. */
    main_notifications = callback_notifications = 0;
    timer_mod(test.earlier, 151);
    g_assert_cmpuint(main_notifications, ==, 1);
    g_assert_cmpuint(callback_notifications, ==, 0);
    timer_del(test.earlier);
    timer_free(main_timer);
    timer_free(test.earlier);
    timer_free(test.nested);
    timerlistgroup_deinit(&nested_group);
}

typedef struct RearmIrqTest {
    QEMUTimer *timer;
    int64_t irq_ns;
    bool irq_asserted;
} RearmIrqTest;

static void rearm_then_irq(void *opaque)
{
    RearmIrqTest *test = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    /* Devices commonly schedule their next expiration before raising IRQ.
     * The pending IRQ is not visible to the idle check until this completes. */
    timer_mod(test->timer, now + 100);
    test->irq_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    test->irq_asserted = true;
    test_cpu_idle = false;
}

static void test_rearm_before_irq(void)
{
    RearmIrqTest test = { 0 };

    test_clock = 100;
    use_icount = 1;
    test_cpu_idle = false;
    test.timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, rearm_then_irq, &test);
    timer_mod(test.timer, test_clock);

    test_cpu_idle = true;
    g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_true(test.irq_asserted);
    g_assert_cmpint(test.irq_ns, ==, 100);
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 100);
    g_assert_cmpint(qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                              QEMU_TIMER_ATTR_ALL), ==, 100);

    /* The main loop checks only after callbacks finish: a runnable CPU must
     * not warp, but returning to idle still permits the next timer wakeup. */
    icount_start_warp_timer();
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 100);
    test_cpu_idle = true;
    icount_start_warp_timer();
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 200);

    timer_del(test.timer);
    timer_free(test.timer);
    test_cpu_idle = false;
    use_icount = 0;
}

int main(int argc, char **argv)
{
    int result;

    g_test_init(&argc, &argv, NULL);
    init_clocks(notify_main);
    qemu_clock_enable(QEMU_CLOCK_VIRTUAL, true);
    g_test_add_func("/timer/notify/expired-lists", test_notify_expired);
    g_test_add_data_func("/timer/notify/callback-earlier-deadline",
                         GINT_TO_POINTER(false), test_callback_rearm);
    g_test_add_data_func("/timer/notify/async-earlier-deadline",
                         GINT_TO_POINTER(true), test_callback_rearm);
    g_test_add_func("/timer/notify/rearm-before-irq", test_rearm_before_irq);
    result = g_test_run();
    timerlistgroup_deinit(&main_loop_tlg);
    return result;
}
