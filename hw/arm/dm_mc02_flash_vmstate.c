/* Component-only VMState contract for the DM-MC02 Flash register model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_flash.h"
#include "migration/vmstate.h"

#define FLASH_CR1_OFFSET  0x0c
#define FLASH_SR1_OFFSET  0x10
#define FLASH_CR_LOCK     (1u << 0)
#define FLASH_CR_PG       (1u << 1)
#define FLASH_CR_SER      (1u << 2)
#define FLASH_CR_FW       (1u << 6)
#define FLASH_SR_PGSERR   (1u << 18)

static int dm_mc02_flash_validate(void *opaque)
{
    DmMc02Flash *state = opaque;
    uint32_t cr = ldl_le_p(state->regs + FLASH_CR1_OFFSET);
    uint32_t sr = ldl_le_p(state->regs + FLASH_SR1_OFFSET);

    /* Pending bytes are producer state, not NVM bytes or runtime wiring.
     * A complete word is committed synchronously and is never pending. */
    if (state->program_count >= DM_MC02_FLASH_WORD_SIZE ||
        (state->program_count & 3)) {
        return -EINVAL;
    }
    if (!state->program_count) {
        return state->program_address ? -EINVAL : 0;
    }
    if ((cr & (FLASH_CR_LOCK | FLASH_CR_SER | FLASH_CR_FW)) ||
        !(cr & FLASH_CR_PG) || (sr & FLASH_SR_PGSERR) ||
        (state->program_address & (DM_MC02_FLASH_WORD_SIZE - 1)) ||
        state->program_address > state->storage_size ||
        DM_MC02_FLASH_WORD_SIZE > state->storage_size - state->program_address) {
        return -EINVAL;
    }
    return 0;
}

static int dm_mc02_flash_post_load(void *opaque, int version_id)
{
    DmMc02Flash *state = opaque;

    if (version_id < 1 || version_id > 2) {
        return -EINVAL;
    }
    if (version_id == 1) {
        /* v1 could not represent a pending write buffer. */
        state->program_address = 0;
        state->program_count = 0;
        memset(state->program_data, 0xff, sizeof(state->program_data));
    }
    if (dm_mc02_flash_validate(state)) {
        return -EINVAL;
    }
    dm_mc02_flash_sync_runtime(state);
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_flash = {
    .name = "dm-mc02-flash",
    .version_id = 2,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_flash_validate,
    .post_load = dm_mc02_flash_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Flash,
                            DM_MC02_FLASH_REG_REGION_SIZE),
        VMSTATE_BOOL(key1_seen, DmMc02Flash),
        VMSTATE_BOOL(optkey_seen, DmMc02Flash),
        VMSTATE_UINT32_V(program_address, DmMc02Flash, 2),
        VMSTATE_UINT8_V(program_count, DmMc02Flash, 2),
        VMSTATE_UINT8_ARRAY_V(program_data, DmMc02Flash,
                              DM_MC02_FLASH_WORD_SIZE, 2),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_flash_vmstate(void)
{
    return &vmstate_dm_mc02_flash;
}
