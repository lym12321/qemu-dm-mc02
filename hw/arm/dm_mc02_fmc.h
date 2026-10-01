/*
 * Minimal STM32H723 FMC_R register model for the DM-MC02 machine.
 *
 * This models the register window only.  External memory devices and FMC
 * transaction timing are intentionally outside the scope of this device.
 */
#ifndef HW_ARM_DM_MC02_FMC_H
#define HW_ARM_DM_MC02_FMC_H

#include "exec/memory.h"
#include "qemu/typedefs.h"

#include <stdint.h>

#define DM_MC02_FMC_REG_REGION_SIZE 0x400

typedef struct DmMc02Fmc {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_FMC_REG_REGION_SIZE];
} DmMc02Fmc;

void dm_mc02_fmc_init(DmMc02Fmc *state, Object *owner);
void dm_mc02_fmc_reset(DmMc02Fmc *state);

/* Component-only state contract; MemoryRegion wiring is runtime state. */
const VMStateDescription *dm_mc02_fmc_vmstate(void);

#endif
