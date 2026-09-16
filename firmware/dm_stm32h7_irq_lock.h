/* Cortex-M7 PRIMASK adapter for serialized IRQ/shared-state boundaries. */
#ifndef DM_STM32H7_IRQ_LOCK_H
#define DM_STM32H7_IRQ_LOCK_H

#include <stdint.h>

typedef struct DmStm32H7IrqLock {
    uint32_t saved_primask;
    uint32_t depth;
} DmStm32H7IrqLock;

void dm_stm32h7_irq_lock_init(DmStm32H7IrqLock *lock);

/* These callbacks match DmStm32H7UsbHostPipeAsyncDispatchLock. The opaque
 * pointer must identify the same lock for a matched enter/leave pair. */
void dm_stm32h7_irq_lock_enter(void *opaque);
void dm_stm32h7_irq_lock_leave(void *opaque);

#endif /* DM_STM32H7_IRQ_LOCK_H */
