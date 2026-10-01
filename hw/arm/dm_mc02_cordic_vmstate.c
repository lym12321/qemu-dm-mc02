/* VMState contract for the board-independent CORDIC component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_cordic.h"
#include "migration/vmstate.h"

static int dm_mc02_cordic_post_load(void *opaque, int version_id)
{
    DmMc02Cordic *state = opaque;

    if (version_id != 1 || state->arg_count > ARRAY_SIZE(state->args) ||
        state->result_count > ARRAY_SIZE(state->results)) {
        return -EINVAL;
    }
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_cordic = {
    .name = "dm-mc02-cordic",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_cordic_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32(csr, DmMc02Cordic),
        VMSTATE_UINT32_ARRAY(args, DmMc02Cordic, 2),
        VMSTATE_UINT32(arg_count, DmMc02Cordic),
        VMSTATE_UINT32_ARRAY(results, DmMc02Cordic, 2),
        VMSTATE_UINT32(result_count, DmMc02Cordic),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_cordic_vmstate(void)
{
    return &vmstate_dm_mc02_cordic;
}
