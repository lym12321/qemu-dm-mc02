/* Minimal STM32H723 SYSCFG register window for DM-MC02. */
#ifndef HW_ARM_DM_MC02_SYSCFG_H
#define HW_ARM_DM_MC02_SYSCFG_H

#include "exec/memory.h"
#include "qemu/typedefs.h"

#include <stdint.h>

#define DM_MC02_SYSCFG_REGION_SIZE 0x400

typedef void DmMc02SyscfgChanged(void *opaque);

typedef struct DmMc02Syscfg {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_SYSCFG_REGION_SIZE];
    DmMc02SyscfgChanged *changed;
    void *changed_opaque;
} DmMc02Syscfg;

void dm_mc02_syscfg_init(DmMc02Syscfg *state, Object *owner);
void dm_mc02_syscfg_set_changed(DmMc02Syscfg *state,
                                DmMc02SyscfgChanged *changed,
                                void *opaque);
void dm_mc02_syscfg_reset(DmMc02Syscfg *state);
/* Return the GPIO port selected for an EXTI line (A=0, B=1, ...). */
unsigned dm_mc02_syscfg_get_exti_port(const DmMc02Syscfg *state,
                                      unsigned line);

/* Component-only state contract.  The MemoryRegion and changed callback are
 * runtime wiring; a consumer is notified after load so it can re-derive any
 * dependent routing from the restored EXTICR bytes. */
const VMStateDescription *dm_mc02_syscfg_vmstate(void);
const VMStateDescription *dm_mc02_syscfg_vmstate_raw(void);
extern const VMStateDescription vmstate_dm_mc02_syscfg_raw;

#endif
