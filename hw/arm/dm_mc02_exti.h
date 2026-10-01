/* Minimal STM32H723 EXTI register window for DM-MC02. */
#ifndef HW_ARM_DM_MC02_EXTI_H
#define HW_ARM_DM_MC02_EXTI_H

#include "exec/memory.h"
#include "hw/irq.h"
#include "qapi/error.h"

#include <stdint.h>
#include <stddef.h>

#define DM_MC02_EXTI_REGION_SIZE 0x400
#define DM_MC02_EXTI_IRQ_GROUP_COUNT 7

/* A borrowed destination IRQ target.  The EXTI model does not inspect or
 * serialize the interrupt controller behind this line. */
typedef struct DmMc02ExtiIrqRoute {
    unsigned exti_group;
    unsigned controller_input;
    qemu_irq controller_irq;
} DmMc02ExtiIrqRoute;

typedef struct DmMc02Exti {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_EXTI_REGION_SIZE];
    uint16_t line_level;
    qemu_irq irq[DM_MC02_EXTI_IRQ_GROUP_COUNT];
    bool irq_level[DM_MC02_EXTI_IRQ_GROUP_COUNT];
} DmMc02Exti;

void dm_mc02_exti_init(DmMc02Exti *state, Object *owner);
void dm_mc02_exti_reset(DmMc02Exti *state);
/* Connect EXTI0..4, EXTI9_5 and EXTI15_10 respectively. */
void dm_mc02_exti_set_irq(DmMc02Exti *state, unsigned group, qemu_irq irq);
/* Bind EXTI groups to destination interrupt-controller inputs.  The complete
 * route table is validated before any existing binding is changed. */
bool dm_mc02_exti_connect_nvic(DmMc02Exti *state,
                               const DmMc02ExtiIrqRoute *routes,
                               size_t route_count,
                               unsigned controller_input_count,
                               Error **errp);
/* Drive an external GPIO line.  Rising/falling trigger selection creates a
 * pending bit; the IRQ output remains level-sensitive until PR is cleared. */
void dm_mc02_exti_set_line(DmMc02Exti *state, unsigned line, bool level);

/* Rebuild all IRQ projections from the restored register and line state. */
void dm_mc02_exti_sync_runtime(DmMc02Exti *state);

/* Component-only state contract; IRQ handles and derived levels are runtime
 * wiring, while line_level is the sampled input needed for future edges. */
const VMStateDescription *dm_mc02_exti_vmstate(void);
const VMStateDescription *dm_mc02_exti_vmstate_raw(void);
extern const VMStateDescription vmstate_dm_mc02_exti_raw;

#endif
