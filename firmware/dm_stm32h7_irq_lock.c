/* Cortex-M7 PRIMASK adapter for serialized IRQ/shared-state boundaries. */
#include "dm_stm32h7_irq_lock.h"

void dm_stm32h7_irq_lock_init(DmStm32H7IrqLock *lock)
{
    if (!lock) {
        return;
    }
    lock->saved_primask = 0;
    lock->depth = 0;
}

void dm_stm32h7_irq_lock_enter(void *opaque)
{
    DmStm32H7IrqLock *lock = opaque;

    if (!lock->depth) {
        __asm__ volatile ("mrs %0, primask" : "=r"(lock->saved_primask)
                          :: "memory");
        __asm__ volatile ("cpsid i" ::: "memory");
    }
    ++lock->depth;
}

void dm_stm32h7_irq_lock_leave(void *opaque)
{
    DmStm32H7IrqLock *lock = opaque;

    if (!lock->depth) {
        return;
    }
    if (--lock->depth) {
        return;
    }
    if (lock->saved_primask & 1u) {
        __asm__ volatile ("cpsid i" ::: "memory");
    } else {
        __asm__ volatile ("cpsie i" ::: "memory");
    }
}
