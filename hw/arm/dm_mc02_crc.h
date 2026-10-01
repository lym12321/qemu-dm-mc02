/* Minimal STM32H723 CRC register window for DM-MC02. */
#ifndef HW_ARM_DM_MC02_CRC_H
#define HW_ARM_DM_MC02_CRC_H

#include "exec/memory.h"
#include "qemu/typedefs.h"

#include <stdint.h>

#define DM_MC02_CRC_REGION_SIZE 0x100

typedef struct DmMc02Crc {
    MemoryRegion iomem;
    uint32_t regs[DM_MC02_CRC_REGION_SIZE / sizeof(uint32_t)];
    uint32_t value;
} DmMc02Crc;

void dm_mc02_crc_init(DmMc02Crc *state, Object *owner);
void dm_mc02_crc_reset(DmMc02Crc *state);

/* Component-only state contract; machine-level migration is intentionally
 * deferred until all local devices and runtime connections are covered. */
const VMStateDescription *dm_mc02_crc_vmstate(void);

#endif
