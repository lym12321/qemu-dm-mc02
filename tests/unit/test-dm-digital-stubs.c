/* SPDX-License-Identifier: GPL-2.0-or-later */
/* External clock/MMIO boundary for physical output unit consumers. */
#include "qemu/osdep.h"
#include "exec/memory.h"
#include "hw/irq.h"
#include "qemu/timer.h"

static QEMUTimer *timers[16];
static int64_t now_ns;

int64_t qemu_clock_get_ns(QEMUClockType type)
{
    return now_ns;
}

void timer_init_full(QEMUTimer *t, QEMUTimerListGroup *group,
                     QEMUClockType type, int scale, int attributes,
                     QEMUTimerCB *cb, void *opaque)
{
    t->cb = cb; t->opaque = opaque; t->scale = scale; t->expire_time = -1;
    for (unsigned i = 0; i < G_N_ELEMENTS(timers); ++i) {
        if (!timers[i]) { timers[i] = t; return; }
    }
    g_assert_not_reached();
}

void timer_mod(QEMUTimer *t, int64_t deadline) { t->expire_time = deadline; }
void timer_del(QEMUTimer *t) { t->expire_time = -1; }
void dm_test_clock(int64_t target, bool late);
void dm_test_clock_reset(void);
void dm_test_clock_reset(void)
{
    memset(timers, 0, sizeof(timers));
    now_ns = 0;
}

/* Late dispatch leaves the clock at target while delivering sorted deadlines. */
void dm_test_clock(int64_t target, bool late)
{
    if (late) { now_ns = target; }
    for (unsigned guard = 0; guard < 100000; ++guard) {
        QEMUTimer *first = NULL;
        for (unsigned i = 0; i < G_N_ELEMENTS(timers); ++i) {
            QEMUTimer *t = timers[i];
            if (t && t->expire_time >= 0 && t->expire_time <= target &&
                (!first || t->expire_time < first->expire_time)) { first = t; }
        }
        if (!first) { now_ns = target; return; }
        if (!late) { now_ns = first->expire_time; }
        timer_del(first);
        first->cb(first->opaque);
    }
    g_error("digital producer failed to advance its timer");
}

void memory_region_init_io(MemoryRegion *mr, Object *owner,
                           const MemoryRegionOps *ops, void *opaque,
                           const char *name, uint64_t size)
{
    mr->ops = ops; mr->opaque = opaque;
}

void qemu_set_irq(qemu_irq irq, int level)
{
    g_assert_null(irq);
}
