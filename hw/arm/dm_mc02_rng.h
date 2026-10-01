/*
 * Minimal STM32H723 random-number generator model for DM-MC02.
 *
 * The model provides the register behavior needed by the firmware during
 * startup.  It is deterministic by design, which makes firmware tests
 * reproducible; it is not intended to model entropy quality or analog noise.
 */
#ifndef HW_ARM_DM_MC02_RNG_H
#define HW_ARM_DM_MC02_RNG_H

#include "exec/memory.h"
#include "hw/irq.h"

#include <stdint.h>

#define DM_MC02_RNG_REGION_SIZE 0x100
#define DM_MC02_RNG_FIFO_DEPTH  4

#define DM_MC02_RNG_CR             0x00
#define DM_MC02_RNG_SR             0x04
#define DM_MC02_RNG_DR             0x08
#define DM_MC02_RNG_HTCR           0x10

#define DM_MC02_RNG_CR_RNGEN       UINT32_C(0x00000004)
#define DM_MC02_RNG_CR_IE          UINT32_C(0x00000008)
#define DM_MC02_RNG_CR_CED         UINT32_C(0x00000020)
#define DM_MC02_RNG_CR_CONFIG3     UINT32_C(0x00000F00)
#define DM_MC02_RNG_CR_NISTC       UINT32_C(0x00001000)
#define DM_MC02_RNG_CR_CONFIG2     UINT32_C(0x0000E000)
#define DM_MC02_RNG_CR_CLKDIV      UINT32_C(0x000F0000)
#define DM_MC02_RNG_CR_CONFIG1     UINT32_C(0x03F00000)
#define DM_MC02_RNG_CR_CONDRST     UINT32_C(0x40000000)
#define DM_MC02_RNG_CR_CONFIGLOCK  UINT32_C(0x80000000)
#define DM_MC02_RNG_CR_CONFIG_MASK \
    (DM_MC02_RNG_CR_CED | DM_MC02_RNG_CR_CONFIG3 | \
     DM_MC02_RNG_CR_NISTC | DM_MC02_RNG_CR_CONFIG2 | \
     DM_MC02_RNG_CR_CLKDIV | DM_MC02_RNG_CR_CONFIG1)

#define DM_MC02_RNG_SR_DRDY        UINT32_C(0x00000001)
#define DM_MC02_RNG_SR_CECS        UINT32_C(0x00000002)
#define DM_MC02_RNG_SR_SECS        UINT32_C(0x00000004)
#define DM_MC02_RNG_SR_CEIS        UINT32_C(0x00000020)
#define DM_MC02_RNG_SR_SEIS        UINT32_C(0x00000040)

typedef struct DmMc02Rng {
    MemoryRegion iomem;
    uint32_t regs[DM_MC02_RNG_REGION_SIZE / sizeof(uint32_t)];
    uint32_t prng;
    uint32_t seed;
    uint32_t fifo[4];
    uint32_t status;
    uint32_t fifo_count;
    uint32_t fifo_index;
    /* The real output buffer can refill after DRDY has gone low.  Keep one
     * observable empty poll before refilling so polling HAL code sees the
     * documented boundary without adding a virtual timer per random word. */
    bool refill_armed;
    qemu_irq irq;
} DmMc02Rng;

void dm_mc02_rng_init(DmMc02Rng *state, Object *owner);
void dm_mc02_rng_reset(DmMc02Rng *state);
uint64_t dm_mc02_rng_read_reg(DmMc02Rng *state, hwaddr offset,
                               unsigned size);
void dm_mc02_rng_write_reg(DmMc02Rng *state, hwaddr offset, uint64_t value,
                           unsigned size);
void dm_mc02_rng_set_irq(DmMc02Rng *state, qemu_irq irq);

/* Component-only state contract; machine-level migration is intentionally
 * deferred until all local devices and runtime connections are covered. */
const VMStateDescription *dm_mc02_rng_vmstate(void);

#endif
