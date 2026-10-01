/*
 * Minimal STM32H723 CORDIC model.
 *
 * This intentionally implements only the common 32-bit Q1.31 cosine/sine
 * modes: FUNC=0 is cosine and FUNC=1 is sine (STM32H723 HAL encoding),
 * ARGSIZE=RESSIZE=0, NARGS=0, and NRES=0 or 1.  A Q1.31 angle is
 * normalized in turns: -1..+1 represents -pi..+pi, as specified by the
 * STM32H7 CORDIC HAL.  NRES requests the paired result: sine/cosine for
 * cosine/sine respectively.  Precision,
 * scale, interrupt, and DMA controls are accepted as configuration fields
 * but do not change this synchronous functional slice. Unsupported input
 * configurations set the model diagnostic bit below and produce no result.
 */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_cordic.h"

#include <math.h>

#define CORDIC_CSR             0x00
#define CORDIC_WDATA           0x04
#define CORDIC_RDATA           0x08
#define CSR_FUNC_MASK          0x0000000f
#define CSR_NRES               (1u << 19)
#define CSR_NARGS              (1u << 20)
#define CSR_RESSIZE            (1u << 21)
#define CSR_ARGSIZE            (1u << 22)
#define CSR_RRDY               (1u << 31)
#define CSR_ERROR               (1u << 30) /* model-only unsupported flag */
#define CSR_WRITABLE_MASK      0x007f07ff

static uint32_t cordic_q31(double value)
{
    double scaled = value * 2147483648.0;

    if (scaled >= 2147483647.0) {
        return 0x7fffffffu;
    }
    if (scaled <= -2147483648.0) {
        return 0x80000000u;
    }
    return (uint32_t)(int32_t)(scaled >= 0.0 ? scaled + 0.5 : scaled - 0.5);
}

static void cordic_compute(DmMc02Cordic *s)
{
    int32_t raw = (int32_t)s->args[0];
    double angle = (double)raw * (M_PI / 2147483648.0);
    uint32_t function = s->csr & CSR_FUNC_MASK;

    s->arg_count = 0;
    s->result_count = 0;
    if (function > 1 || (s->csr & (CSR_NARGS | CSR_RESSIZE | CSR_ARGSIZE))) {
        s->csr |= CSR_ERROR;
        return;
    }

    s->results[0] = cordic_q31(function == 0 ? cos(angle) : sin(angle));
    s->result_count = 1;
    if (s->csr & CSR_NRES) {
        s->results[1] = cordic_q31(function == 0 ? sin(angle) : cos(angle));
        s->result_count = 2;
    }
}

static uint64_t dm_mc02_cordic_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Cordic *s = opaque;

    if (size != 4 || offset >= DM_MC02_CORDIC_REGION_SIZE) {
        return 0;
    }
    switch (offset) {
    case CORDIC_CSR:
        return (s->csr & CSR_WRITABLE_MASK) |
               (s->result_count ? CSR_RRDY : 0) |
               (s->csr & CSR_ERROR);
    case CORDIC_RDATA:
        if (s->result_count == 0) {
            return 0;
        }
        {
            uint32_t result = s->results[0];
            s->results[0] = s->results[1];
            s->result_count--;
            return result;
        }
    case CORDIC_WDATA:
    default:
        return 0;
    }
}

static void dm_mc02_cordic_write(void *opaque, hwaddr offset, uint64_t value,
                                 unsigned size)
{
    DmMc02Cordic *s = opaque;

    if (size != 4 || offset >= DM_MC02_CORDIC_REGION_SIZE) {
        return;
    }
    switch (offset) {
    case CORDIC_CSR:
        s->csr = (uint32_t)value & CSR_WRITABLE_MASK;
        s->csr &= ~CSR_ERROR;
        s->arg_count = 0;
        break;
    case CORDIC_WDATA:
        if (s->arg_count < ARRAY_SIZE(s->args)) {
            s->args[s->arg_count++] = (uint32_t)value;
        }
        if (s->arg_count == 1 && !(s->csr & CSR_NARGS)) {
            cordic_compute(s);
        } else if (s->arg_count == 2 && (s->csr & CSR_NARGS)) {
            cordic_compute(s);
        }
        break;
    case CORDIC_RDATA:
    default:
        break;
    }
}

static const MemoryRegionOps dm_mc02_cordic_ops = {
    .read = dm_mc02_cordic_read,
    .write = dm_mc02_cordic_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

void dm_mc02_cordic_init(DmMc02Cordic *state, Object *owner)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_cordic_ops, state,
                          "dm-mc02.cordic", DM_MC02_CORDIC_REGION_SIZE);
}

void dm_mc02_cordic_reset(DmMc02Cordic *state)
{
    if (state) {
        state->csr = 0;
        memset(state->args, 0, sizeof(state->args));
        state->arg_count = 0;
        memset(state->results, 0, sizeof(state->results));
        state->result_count = 0;
    }
}
