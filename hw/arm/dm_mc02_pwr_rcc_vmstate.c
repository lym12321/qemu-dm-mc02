/* Component-only VMState contract for the STM32H723 PWR/RCC model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_pwr_rcc.h"
#include "migration/vmstate.h"

bool dm_mc02_pwr_rcc_state_valid(const DmMc02PwrRcc *state)
{
    return state && state->system_clock_source <= 3;
}

static int dm_mc02_pwr_rcc_post_load(void *opaque, int version_id)
{
    DmMc02PwrRcc *state = opaque;

    if (version_id != 1 || !dm_mc02_pwr_rcc_state_valid(state)) {
        return -EINVAL;
    }

    /* MemoryRegions and the callback are destination-owned.  Reproject the
     * restored effective clock only after every serialized field is valid. */
    dm_mc02_pwr_rcc_sync_runtime(state);
    return 0;
}

static const VMStateField vmstate_dm_mc02_pwr_rcc_fields[] = {
    VMSTATE_UINT8_ARRAY(pwr_regs, DmMc02PwrRcc,
                        DM_MC02_PWR_RCC_REGION_SIZE),
    VMSTATE_UINT8_ARRAY(rcc_regs, DmMc02PwrRcc,
                        DM_MC02_PWR_RCC_REGION_SIZE),
    VMSTATE_UINT8(system_clock_source, DmMc02PwrRcc),
    VMSTATE_BOOL(adc_clock_configured, DmMc02PwrRcc),
    VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_mc02_pwr_rcc_raw = {
    .name = "dm-mc02-pwr-rcc-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_mc02_pwr_rcc_fields,
};

static const VMStateDescription vmstate_dm_mc02_pwr_rcc = {
    .name = "dm-mc02-pwr-rcc",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_pwr_rcc_post_load,
    .fields = vmstate_dm_mc02_pwr_rcc_fields,
};

const VMStateDescription *dm_mc02_pwr_rcc_vmstate(void)
{
    return &vmstate_dm_mc02_pwr_rcc;
}

const VMStateDescription *dm_mc02_pwr_rcc_vmstate_raw(void)
{
    return &vmstate_dm_mc02_pwr_rcc_raw;
}
