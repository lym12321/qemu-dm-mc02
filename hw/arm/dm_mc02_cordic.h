/* Minimal STM32H723 CORDIC peripheral model. */
#ifndef HW_ARM_DM_MC02_CORDIC_H
#define HW_ARM_DM_MC02_CORDIC_H

#include "exec/memory.h"

#include "qemu/typedefs.h"

#define DM_MC02_CORDIC_REGION_SIZE 0x400

typedef struct DmMc02Cordic {
    MemoryRegion iomem;
    uint32_t csr;
    uint32_t args[2];
    uint32_t arg_count;
    uint32_t results[2];
    uint32_t result_count;
} DmMc02Cordic;

void dm_mc02_cordic_init(DmMc02Cordic *state, Object *owner);
void dm_mc02_cordic_reset(DmMc02Cordic *state);

/* Reusable component state contract.  This description is intentionally not
 * registered by the DM-MC02 machine until the complete machine state has an
 * audited migration contract. */
const VMStateDescription *dm_mc02_cordic_vmstate(void);

#endif
