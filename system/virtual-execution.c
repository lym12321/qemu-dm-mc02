/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Explicit CPU execution bounds; timers, clocks and pause remain native. */

#include "qemu/osdep.h"
#include "qemu/atomic.h"
#include "block/block-global-state.h"
#include "hw/core/cpu.h"
#include "migration/blocker.h"
#include "migration/misc.h"
#include "qapi/error.h"
#include "qapi/qapi-commands-misc.h"
#include "qapi/qapi-commands-run-state.h"
#include "sysemu/block-backend.h"
#include "sysemu/cpu-timers.h"
#include "sysemu/cpus.h"
#include "sysemu/qtest.h"
#include "sysemu/replay.h"
#include "sysemu/reset.h"
#include "sysemu/runstate.h"
#include "sysemu/virtual-execution.h"

static struct {
    QEMUTimer *timer;
    VirtualExecutionState state;
    bool has_target;
    bool hit;
    int64_t target_ns;
    Error *migration_blocker;
} execution;

/* Native budget reads outside BQL; retain the bound through deferred stop. */
static aligned_int64_t execution_limit_ns = -1;

int64_t virtual_execution_deadline_ns(int64_t now_ns)
{
    int64_t target = qatomic_read_i64(&execution_limit_ns);

    return target < 0 ? -1 : target <= now_ns ? 0 : target - now_ns;
}

static void execution_release(void)
{
    timer_del(execution.timer);
    qatomic_set_i64(&execution_limit_ns, -1);
    migrate_del_blocker(&execution.migration_blocker);
}

static void execution_stopped(void *opaque, bool running, RunState state)
{
    if (running || execution.state != VIRTUAL_EXECUTION_STATE_RUNNING) {
        return;
    }
    execution.state = execution.hit && state == RUN_STATE_PAUSED &&
        qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) == execution.target_ns ?
        VIRTUAL_EXECUTION_STATE_REACHED : VIRTUAL_EXECUTION_STATE_CANCELLED;
    execution_release();
}

static void execution_reset(void *opaque)
{
    if (execution.has_target) {
        execution.state = VIRTUAL_EXECUTION_STATE_CANCELLED;
        execution.hit = false;
        execution_release();
    }
}

static void execution_target(void *opaque)
{
    execution.hit = true;
    /*
     * A virtual timer cannot disable its own clock (timerlist self-deadlock).
     * Keep the native budget/warp bound at zero until deferred pause commits.
     */
    qemu_system_vmstop_request_prepare();
    qemu_system_vmstop_request(RUN_STATE_PAUSED);
    if (qemu_in_vcpu_thread()) {
        cpu_stop_current();
    }
}

VirtualExecutionInfo *qmp_x_query_virtual_execution(Error **errp)
{
    VirtualExecutionInfo *info = g_new0(VirtualExecutionInfo, 1);

    info->current_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    info->state = execution.state;
    info->has_target_ns = execution.has_target;
    info->target_ns = execution.target_ns;
    return info;
}

void qmp_x_run_to_virtual_time(int64_t target_ns, Error **errp)
{
    IOThreadInfoList *iothreads;
    bool has_iothreads;

    if (qtest_enabled() || !icount_has_exact_ns_deadlines() ||
        !first_cpu || CPU_NEXT(first_cpu) || replay_mode != REPLAY_MODE_NONE) {
        error_setg(errp, "Virtual execution requires one STTCG CPU, "
                   "icount shift=0,align=off,sleep=off and no replay");
        return;
    }
    if (execution.state == VIRTUAL_EXECUTION_STATE_RUNNING ||
        !(runstate_check(RUN_STATE_PAUSED) ||
          runstate_check(RUN_STATE_PRELAUNCH))) {
        error_setg(errp, "Virtual execution requires paused/prelaunch "
                   "with no outstanding target");
        return;
    }
    if (target_ns <= qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) {
        error_setg(errp, "Virtual execution target must be strictly after now");
        return;
    }
    if (bdrv_next_all_states(NULL) || blk_all_next(NULL) ||
        !migration_is_idle() || qemu_system_dump_in_progress()) {
        error_setg(errp, "Virtual execution does not support "
                   "block nodes/backends, migration or background dump");
        return;
    }
    iothreads = qmp_query_iothreads(NULL);
    has_iothreads = iothreads != NULL;
    qapi_free_IOThreadInfoList(iothreads);
    if (has_iothreads) {
        error_setg(errp, "Virtual execution does not support user IOThreads");
        return;
    }
    error_setg(&execution.migration_blocker,
               "Virtual execution target is outstanding");
    if (migrate_add_blocker_internal(&execution.migration_blocker, errp) < 0) {
        return;
    }
    if (!execution.timer) {
        execution.timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                       execution_target, NULL);
        qemu_add_vm_change_state_handler(execution_stopped, NULL);
        qemu_register_reset(execution_reset, NULL);
    }
    execution.target_ns = target_ns;
    execution.has_target = true;
    execution.hit = false;
    execution.state = VIRTUAL_EXECUTION_STATE_RUNNING;
    qatomic_set_i64(&execution_limit_ns, target_ns);
    timer_mod_ns(execution.timer, target_ns);
    vm_start();
}
