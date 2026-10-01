/* Board-independent adapter from a controller-owned SSI bus to dm-w25q64. */
#ifndef HW_ARM_DM_MC02_SSI_NOR_H
#define HW_ARM_DM_MC02_SSI_NOR_H

#include "hw/irq.h"
#include "hw/qdev-core.h"
#include "hw/ssi/ssi.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct DmMc02SsiNor {
    SSIBus *bus;
    DeviceState *device;
    qemu_irq cs;
    uint8_t *storage;
    bool selected;
} DmMc02SsiNor;

bool dm_mc02_ssi_nor_init(DmMc02SsiNor *adapter, DeviceState *parent,
                          const char *bus_name,
                          uint32_t expected_size,
                          const uint8_t expected_id[3], Error **errp);
void dm_mc02_ssi_nor_select(DmMc02SsiNor *adapter, bool selected);
uint8_t dm_mc02_ssi_nor_transfer(DmMc02SsiNor *adapter, uint8_t value);
void dm_mc02_ssi_nor_reset(DmMc02SsiNor *adapter);

#endif
