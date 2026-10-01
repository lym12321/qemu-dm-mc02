/* Component-only VMState for the board-independent BMI088 SPI framer. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088_spi.h"
#include "migration/vmstate.h"

static int dm_mc02_bmi088_spi_post_load(void *opaque, int version_id)
{
    DmMc02Bmi088Spi *state = opaque;

    /* A framer with no command has no meaningful transaction cursor.  Keep
     * this check independent of the pointed-to die so the component remains
     * usable with a destination-owned runtime device pointer. */
    if (version_id != 1 ||
        state->read_start_reg > 0x7f ||
        (!state->command_seen &&
         (state->read_transfer || state->dummy_pending || state->reg ||
          state->read_start_reg)) ||
        (!state->read_transfer && state->dummy_pending)) {
        return -EINVAL;
    }
    return 0;
}

const VMStateDescription vmstate_dm_mc02_bmi088_spi = {
    .name = "dm-mc02-bmi088-spi",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_bmi088_spi_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_BOOL(command_seen, DmMc02Bmi088Spi),
        VMSTATE_BOOL(read_transfer, DmMc02Bmi088Spi),
        VMSTATE_BOOL(dummy_pending, DmMc02Bmi088Spi),
        VMSTATE_UINT8(reg, DmMc02Bmi088Spi),
        VMSTATE_UINT8(read_start_reg, DmMc02Bmi088Spi),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_bmi088_spi_vmstate(void)
{
    return &vmstate_dm_mc02_bmi088_spi;
}
