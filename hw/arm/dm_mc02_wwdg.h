/* Reusable STM32H723 system window watchdog register model. */
#ifndef HW_ARM_DM_MC02_WWDG_H
#define HW_ARM_DM_MC02_WWDG_H

#include "exec/memory.h"
#include "hw/irq.h"
#include "migration/vmstate.h"
#include "qemu/timer.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_WWDG_REGION_SIZE 0x1000

#define DM_MC02_WWDG_CR_OFFSET  0x00
#define DM_MC02_WWDG_CFR_OFFSET 0x04
#define DM_MC02_WWDG_SR_OFFSET  0x08

#define DM_MC02_WWDG_CR_T_MASK  UINT32_C(0x7f)
#define DM_MC02_WWDG_CR_WDGA    (UINT32_C(1) << 7)
#define DM_MC02_WWDG_CFR_W_MASK UINT32_C(0x7f)
#define DM_MC02_WWDG_CFR_EWI    (UINT32_C(1) << 9)
#define DM_MC02_WWDG_CFR_WDGTB_SHIFT 11
#define DM_MC02_WWDG_CFR_WDGTB_MASK (UINT32_C(7) << \
                                     DM_MC02_WWDG_CFR_WDGTB_SHIFT)
#define DM_MC02_WWDG_SR_EWIF    (UINT32_C(1) << 0)

#define DM_MC02_WWDG_COUNTER_EWI UINT32_C(0x40)
#define DM_MC02_WWDG_COUNTER_RESET UINT32_C(0x3f)
#define DM_MC02_WWDG_INTERNAL_DIVIDER UINT64_C(4096)

typedef void DmMc02WwdgResetRequested(void *opaque);

typedef struct DmMc02Wwdg {
    MemoryRegion iomem;
    QEMUTimer *timer;
    qemu_irq irq;
    uint32_t regs[DM_MC02_WWDG_REGION_SIZE / sizeof(uint32_t)];
    uint32_t counter;
    uint64_t counter_start_ns;
    uint64_t next_event_ns;
    uint64_t clock_hz;
    bool started;
    bool reset_stage;
    uint64_t start_count;
    uint64_t reload_count;
    uint64_t ewi_count;
    uint64_t timeout_count;
    uint64_t window_violation_count;
    DmMc02WwdgResetRequested *reset_requested;
    void *reset_requested_opaque;
} DmMc02Wwdg;

void dm_mc02_wwdg_init(DmMc02Wwdg *state, Object *owner, uint64_t clock_hz);
void dm_mc02_wwdg_reset(void *opaque);
void dm_mc02_wwdg_set_irq(DmMc02Wwdg *state, qemu_irq irq);
void dm_mc02_wwdg_set_clock_hz(DmMc02Wwdg *state, uint64_t clock_hz);
void dm_mc02_wwdg_set_reset_callback(
    DmMc02Wwdg *state, DmMc02WwdgResetRequested *callback, void *opaque);

/* Rebuild the destination timer and IRQ projection from the restored
 * absolute virtual deadline.  Clock configuration and output wiring remain
 * destination-owned. */
void dm_mc02_wwdg_sync_runtime(DmMc02Wwdg *state);

/* Component-only state contract; the DM-MC02 machine does not register it. */
bool dm_mc02_wwdg_state_valid(const DmMc02Wwdg *state);
const VMStateDescription *dm_mc02_wwdg_vmstate(void);
const VMStateDescription *dm_mc02_wwdg_vmstate_raw(void);

#endif
