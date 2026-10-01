/* Minimal STM32H723 DBGMCU register window for DM-MC02. */
#ifndef HW_ARM_DM_MC02_DBGMCU_H
#define HW_ARM_DM_MC02_DBGMCU_H

#include "exec/memory.h"

#include <stdint.h>

#define DM_MC02_DBGMCU_REGION_SIZE 0x400
#define DM_MC02_DBGMCU_IDCODE      0x00
#define DM_MC02_DBGMCU_IDCODE_VALUE UINT32_C(0x20030483)

typedef struct DmMc02Dbgmcu {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_DBGMCU_REGION_SIZE];
} DmMc02Dbgmcu;

void dm_mc02_dbgmcu_init(DmMc02Dbgmcu *state, Object *owner);
void dm_mc02_dbgmcu_reset(DmMc02Dbgmcu *state);

/* Component-only state contract; machine-level migration is intentionally
 * deferred until all local devices and runtime connections are covered. */
const VMStateDescription *dm_mc02_dbgmcu_vmstate(void);

#endif
