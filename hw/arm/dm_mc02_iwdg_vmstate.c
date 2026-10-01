/* Component-only VMState contract for the board-independent IWDG model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_iwdg.h"
#include "migration/vmstate.h"

#define IWDG_VMSTATE_PR_OFFSET  0x04
#define IWDG_VMSTATE_RLR_OFFSET 0x08
#define IWDG_VMSTATE_SR_OFFSET  0x0c
#define IWDG_VMSTATE_WINR_OFFSET 0x10
#define IWDG_VMSTATE_PRESCALER_COUNT 7

static uint32_t iwdg_vmstate_load(const DmMc02Iwdg *state, hwaddr offset)
{
    return state->regs[offset / sizeof(uint32_t)];
}

static bool iwdg_vmstate_pending_valid(const DmMc02Iwdg *state)
{
    static const uint32_t flags[DM_MC02_IWDG_UPDATE_REGISTER_COUNT] = {
        DM_MC02_IWDG_SR_PVU,
        DM_MC02_IWDG_SR_RVU,
        DM_MC02_IWDG_SR_WVU,
    };
    uint32_t sr = iwdg_vmstate_load(state, IWDG_VMSTATE_SR_OFFSET);

    if (sr & ~DM_MC02_IWDG_SR_UPDATE_MASK) {
        return false;
    }
    for (unsigned i = 0; i < DM_MC02_IWDG_UPDATE_REGISTER_COUNT; ++i) {
        uint32_t value;
        bool active = !!(sr & flags[i]);

        switch (i) {
        case 0:
            value = state->pending_pr;
            break;
        case 1:
            value = state->pending_rlr;
            break;
        case 2:
            value = state->pending_winr;
            break;
        default:
            g_assert_not_reached();
        }

        if (active != !!state->update_deadline_ns[i] ||
            state->update_deadline_ns[i] > INT64_MAX) {
            return false;
        }
        if ((!active && value) ||
            (i == 0 && value >= IWDG_VMSTATE_PRESCALER_COUNT) ||
            (i != 0 && (value & ~0xfffu))) {
            return false;
        }
    }
    return true;
}

static int dm_mc02_iwdg_post_load(void *opaque, int version_id)
{
    DmMc02Iwdg *state = opaque;

    if (version_id < 1 || version_id > 2) {
        return -EINVAL;
    }
    if (version_id == 1) {
        /* Version 1 predates deferred PR/RLR/WINR commits.  Its model never
         * exposed SR update flags, so normalize absent v2 producer fields
         * before validating the old stream. */
        state->regs[IWDG_VMSTATE_SR_OFFSET / sizeof(uint32_t)] = 0;
        state->pending_pr = 0;
        state->pending_rlr = 0;
        state->pending_winr = 0;
        memset(state->update_deadline_ns, 0,
               sizeof(state->update_deadline_ns));
    }

    if (
        (iwdg_vmstate_load(state, IWDG_VMSTATE_PR_OFFSET) >=
         IWDG_VMSTATE_PRESCALER_COUNT) ||
        (iwdg_vmstate_load(state, IWDG_VMSTATE_RLR_OFFSET) & ~0xfffu) ||
        (iwdg_vmstate_load(state, IWDG_VMSTATE_WINR_OFFSET) & ~0xfffu) ||
        !iwdg_vmstate_pending_valid(state) ||
        (!state->started && state->next_timeout_ns) ||
        (state->started && !state->next_timeout_ns) ||
        state->next_timeout_ns > INT64_MAX) {
        return -EINVAL;
    }

    /* QEMUTimer and its owner are runtime wiring.  Re-arm only after the
     * complete producer state has passed validation. */
    dm_mc02_iwdg_sync_runtime(state);
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_iwdg = {
    .name = "dm-mc02-iwdg",
    .version_id = 2,
    .minimum_version_id = 1,
    .post_load = dm_mc02_iwdg_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, DmMc02Iwdg,
                             DM_MC02_IWDG_REGION_SIZE / sizeof(uint32_t)),
        VMSTATE_BOOL(boot_grace_pending, DmMc02Iwdg),
        VMSTATE_BOOL(write_unlocked, DmMc02Iwdg),
        VMSTATE_BOOL(started, DmMc02Iwdg),
        VMSTATE_UINT64(next_timeout_ns, DmMc02Iwdg),
        VMSTATE_UINT32_V(pending_pr, DmMc02Iwdg, 2),
        VMSTATE_UINT32_V(pending_rlr, DmMc02Iwdg, 2),
        VMSTATE_UINT32_V(pending_winr, DmMc02Iwdg, 2),
        VMSTATE_UINT64_ARRAY_V(update_deadline_ns, DmMc02Iwdg,
                               DM_MC02_IWDG_UPDATE_REGISTER_COUNT, 2),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_iwdg_vmstate(void)
{
    return &vmstate_dm_mc02_iwdg;
}
