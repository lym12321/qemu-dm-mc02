/* VMState contract for the board-independent RNG component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_rng.h"
#include "migration/vmstate.h"

static int dm_mc02_rng_post_load(void *opaque, int version_id)
{
    DmMc02Rng *state = opaque;

    if (version_id != 1 || state->fifo_count > DM_MC02_RNG_FIFO_DEPTH ||
        state->fifo_index >= DM_MC02_RNG_FIFO_DEPTH ||
        (state->refill_armed && state->fifo_count != 0)) {
        return -EINVAL;
    }
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_rng = {
    .name = "dm-mc02-rng",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_rng_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, DmMc02Rng,
                             DM_MC02_RNG_REGION_SIZE / sizeof(uint32_t)),
        VMSTATE_UINT32(prng, DmMc02Rng),
        VMSTATE_UINT32(seed, DmMc02Rng),
        VMSTATE_UINT32_ARRAY(fifo, DmMc02Rng, DM_MC02_RNG_FIFO_DEPTH),
        VMSTATE_UINT32(status, DmMc02Rng),
        VMSTATE_UINT32(fifo_count, DmMc02Rng),
        VMSTATE_UINT32(fifo_index, DmMc02Rng),
        VMSTATE_BOOL(refill_armed, DmMc02Rng),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_rng_vmstate(void)
{
    return &vmstate_dm_mc02_rng;
}
