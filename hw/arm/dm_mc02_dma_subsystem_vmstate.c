/* Composite VMState for the reusable H723 DMA/DMAMUX subsystem. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_dma.h"
#include "migration/vmstate.h"

static bool dm_mc02_dma_subsystem_identity_valid(
    const DmMc02DmaSubsystem *state)
{
    return state &&
           state->dma1_marker == DM_MC02_DMA_SUBSYSTEM_DMA1_MARKER &&
           state->dma2_marker == DM_MC02_DMA_SUBSYSTEM_DMA2_MARKER &&
           state->dmamux1_marker == DM_MC02_DMA_SUBSYSTEM_DMAMUX1_MARKER &&
           state->dmamux2_marker == DM_MC02_DMA_SUBSYSTEM_DMAMUX2_MARKER;
}

static int dm_mc02_dma_subsystem_pre_save(void *opaque)
{
    DmMc02DmaSubsystem *state = opaque;

    return dm_mc02_dma_subsystem_identity_valid(state) ? 0 : -EINVAL;
}

static int dm_mc02_dma_subsystem_post_load(void *opaque, int version_id)
{
    DmMc02DmaSubsystem *state = opaque;

    if (!state || (version_id != 1 && version_id != 2) ||
        !dm_mc02_dma_subsystem_identity_valid(state)) {
        return -EINVAL;
    }

    for (unsigned i = 0; i < DM_MC02_DMA_CONTROLLER_COUNT; ++i) {
        if (!dm_mc02_dma_state_valid(&state->dma[i])) {
            return -EINVAL;
        }
    }

    /* DMAMUX fields are already loaded when this parent callback runs.  The
     * request index must be discarded only after both mux windows and both
     * DMA stream states have passed validation. */
    for (unsigned i = 0; i < DM_MC02_DMA_CONTROLLER_COUNT; ++i) {
        dm_mc02_dma_sync_runtime(&state->dma[i]);
    }
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_dma_subsystem = {
    .name = "dm-mc02-dma-subsystem",
    .version_id = 2,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_dma_subsystem_pre_save,
    .post_load = dm_mc02_dma_subsystem_post_load,
    .fields = (VMStateField[]) {
        /* Version 1 streams did not carry identity markers.  Their
         * destination must still be initialized with the fixed profile
         * identity before load; version 2 additionally checks these wire
         * markers and rejects swapped positional children. */
        VMSTATE_UINT32_EQUAL_V(dma1_marker, DmMc02DmaSubsystem, 2, NULL),
        /* Put the mux configuration before DMA so the serialized layout
         * follows the request producer -> consumer dependency. */
        VMSTATE_UINT32_EQUAL_V(dma2_marker, DmMc02DmaSubsystem, 2, NULL),
        VMSTATE_UINT32_EQUAL_V(dmamux1_marker, DmMc02DmaSubsystem, 2, NULL),
        VMSTATE_UINT32_EQUAL_V(dmamux2_marker, DmMc02DmaSubsystem, 2, NULL),
        VMSTATE_STRUCT_ARRAY(dmamux, DmMc02DmaSubsystem,
                             DM_MC02_DMAMUX_COUNT, 0,
                             vmstate_dm_mc02_dmamux, DmMc02Dmamux),
        VMSTATE_STRUCT_ARRAY(dma, DmMc02DmaSubsystem,
                             DM_MC02_DMA_CONTROLLER_COUNT, 0,
                             vmstate_dm_mc02_dma_raw, DmMc02Dma),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_dma_subsystem_vmstate(void)
{
    return &vmstate_dm_mc02_dma_subsystem;
}
