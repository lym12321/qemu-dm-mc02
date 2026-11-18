/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Scoped virtual event time shared by board-connected components. */
#ifndef HW_ARM_DM_MC02_EVENT_H
#define HW_ARM_DM_MC02_EVENT_H

#include <stdbool.h>
#include <stdint.h>

/* Runtime composition state, owned by the board and not migrated. Synchronous
 * MMIO callbacks inherit an event's scheduled time without changing QEMU's
 * virtual clock. The caller restores the returned snapshot on every exit. */
typedef struct DmMc02EventTime {
    uint64_t timestamp_ns;
    bool active;
} DmMc02EventTime;

static inline DmMc02EventTime dm_mc02_event_time_enter(
    DmMc02EventTime *context, uint64_t timestamp_ns)
{
    DmMc02EventTime saved = { 0 };

    if (context) {
        saved = *context;
        *context = (DmMc02EventTime) {
            .timestamp_ns = timestamp_ns, .active = true,
        };
    }
    return saved;
}

static inline void dm_mc02_event_time_leave(DmMc02EventTime *context,
                                            DmMc02EventTime saved)
{
    if (context) {
        *context = saved;
    }
}

static inline uint64_t dm_mc02_event_time_get(
    const DmMc02EventTime *context, uint64_t fallback_ns)
{
    return context && context->active ? context->timestamp_ns : fallback_ns;
}

#endif
