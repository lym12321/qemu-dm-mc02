/* External DM-MC02 reset-pin input. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_reset_input.h"

static void dm_mc02_reset_input_set(void *opaque, int line, int level)
{
    DmMc02ResetInput *state = opaque;
    bool was_high;

    (void)line;
    if (!state) {
        return;
    }

    was_high = state->nrst_high;
    state->nrst_high = level != 0;
    /* NRST is active low.  Only the asserted edge is a new reset event;
     * keeping the line low must not queue an unbounded stream of resets. */
    if (was_high && !state->nrst_high && state->asserted) {
        state->asserted(state->asserted_opaque);
    }
}

void dm_mc02_reset_input_set_callback(DmMc02ResetInput *state,
                                       DmMc02ResetInputAsserted *callback,
                                       void *opaque)
{
    if (!state) {
        return;
    }
    state->asserted = callback;
    state->asserted_opaque = opaque;
}

void dm_mc02_reset_input_reset(DmMc02ResetInput *state)
{
    if (!state) {
        return;
    }
    /* The pin is external runtime wiring.  A board reset must not release an
     * externally held-low NRST; the producer owns the later high transition. */
}

static void dm_mc02_reset_input_init(Object *obj)
{
    DmMc02ResetInput *state = DM_MC02_RESET_INPUT(obj);

    state->nrst_high = true;
    qdev_init_gpio_in_named(DEVICE(obj), dm_mc02_reset_input_set,
                            "NRST", 1);
}

static const TypeInfo dm_mc02_reset_input_type = {
    .name = TYPE_DM_MC02_RESET_INPUT,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(DmMc02ResetInput),
    .instance_init = dm_mc02_reset_input_init,
};

static void dm_mc02_reset_input_register_types(void)
{
    type_register_static(&dm_mc02_reset_input_type);
}

type_init(dm_mc02_reset_input_register_types)
