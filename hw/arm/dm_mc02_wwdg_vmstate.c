/* Component-only VMState contract for the reusable WWDG data path. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_wwdg.h"
#include "migration/vmstate.h"

static uint32_t wwdg_vmstate_reg(const DmMc02Wwdg *state, hwaddr offset)
{
    return state->regs[offset / sizeof(uint32_t)];
}

bool dm_mc02_wwdg_state_valid(const DmMc02Wwdg *state)
{
    uint32_t cr;
    uint32_t cfr;
    uint32_t sr;

    if (!state || !state->clock_hz ||
        state->counter > DM_MC02_WWDG_CR_T_MASK ||
        state->counter_start_ns > INT64_MAX ||
        state->next_event_ns > INT64_MAX) {
        return false;
    }

    cr = wwdg_vmstate_reg(state, DM_MC02_WWDG_CR_OFFSET);
    cfr = wwdg_vmstate_reg(state, DM_MC02_WWDG_CFR_OFFSET);
    sr = wwdg_vmstate_reg(state, DM_MC02_WWDG_SR_OFFSET);
    if ((cr & ~(DM_MC02_WWDG_CR_T_MASK | DM_MC02_WWDG_CR_WDGA)) ||
        (cfr & ~(DM_MC02_WWDG_CFR_W_MASK | DM_MC02_WWDG_CFR_EWI |
                 DM_MC02_WWDG_CFR_WDGTB_MASK)) ||
        (sr & ~DM_MC02_WWDG_SR_EWIF)) {
        return false;
    }

    /* The register window has no implemented state beyond CR/CFR/SR. */
    for (unsigned i = 3; i < ARRAY_SIZE(state->regs); ++i) {
        if (state->regs[i]) {
            return false;
        }
    }

    if (!state->started) {
        return !state->reset_stage && !state->next_event_ns;
    }
    if (!state->next_event_ns || state->counter_start_ns > state->next_event_ns) {
        return false;
    }
    if (state->reset_stage && state->counter != DM_MC02_WWDG_COUNTER_EWI) {
        return false;
    }
    return true;
}

static int dm_mc02_wwdg_prepare_post_load(void *opaque, int version_id)
{
    DmMc02Wwdg *state = opaque;

    if (version_id != 1 || !dm_mc02_wwdg_state_valid(state)) {
        return -EINVAL;
    }
    return 0;
}

static int dm_mc02_wwdg_post_load(void *opaque, int version_id)
{
    DmMc02Wwdg *state = opaque;

    if (dm_mc02_wwdg_prepare_post_load(state, version_id)) {
        return -EINVAL;
    }

    /* QEMUTimer, IRQ and reset callback are destination-owned runtime
     * wiring.  Project them only after all producer fields are valid. */
    dm_mc02_wwdg_sync_runtime(state);
    return 0;
}

static const VMStateField vmstate_dm_mc02_wwdg_fields[] = {
    VMSTATE_UINT32_ARRAY(regs, DmMc02Wwdg,
                         DM_MC02_WWDG_REGION_SIZE / sizeof(uint32_t)),
    VMSTATE_UINT32(counter, DmMc02Wwdg),
    VMSTATE_UINT64(counter_start_ns, DmMc02Wwdg),
    VMSTATE_UINT64(next_event_ns, DmMc02Wwdg),
    VMSTATE_BOOL(started, DmMc02Wwdg),
    VMSTATE_BOOL(reset_stage, DmMc02Wwdg),
    VMSTATE_UINT64(start_count, DmMc02Wwdg),
    VMSTATE_UINT64(reload_count, DmMc02Wwdg),
    VMSTATE_UINT64(ewi_count, DmMc02Wwdg),
    VMSTATE_UINT64(timeout_count, DmMc02Wwdg),
    VMSTATE_UINT64(window_violation_count, DmMc02Wwdg),
    VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_mc02_wwdg_raw = {
    .name = "dm-mc02-wwdg-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_wwdg_prepare_post_load,
    .fields = vmstate_dm_mc02_wwdg_fields,
};

static const VMStateDescription vmstate_dm_mc02_wwdg = {
    .name = "dm-mc02-wwdg",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_wwdg_post_load,
    .fields = vmstate_dm_mc02_wwdg_fields,
};

const VMStateDescription *dm_mc02_wwdg_vmstate(void)
{
    return &vmstate_dm_mc02_wwdg;
}

const VMStateDescription *dm_mc02_wwdg_vmstate_raw(void)
{
    return &vmstate_dm_mc02_wwdg_raw;
}
