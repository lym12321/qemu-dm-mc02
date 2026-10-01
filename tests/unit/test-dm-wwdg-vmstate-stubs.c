#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_wwdg.h"

unsigned dm_mc02_wwdg_sync_calls;

void dm_mc02_wwdg_sync_runtime(DmMc02Wwdg *state)
{
    dm_mc02_wwdg_sync_calls++;
    /* The isolated target has no QEMUTimer.  Mark the runtime projection
     * that a real implementation would rebuild. */
    state->timer = (QEMUTimer *)(uintptr_t)1;
}
