#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_iwdg.h"

unsigned dm_mc02_iwdg_sync_calls;

void dm_mc02_iwdg_sync_runtime(DmMc02Iwdg *state)
{
    dm_mc02_iwdg_sync_calls++;
    /* The isolated test has no QEMUTimer.  Mark the derived runtime state
     * that a real implementation would rebuild, without changing the
     * serialized producer fields. */
    state->timeout_timer = (QEMUTimer *)(uintptr_t)1;
}
