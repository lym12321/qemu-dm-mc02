/* Composite VMState for GPIO banks, SYSCFG routing and EXTI state. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_gpio_exti.h"
#include "migration/vmstate.h"

bool dm_mc02_gpio_exti_state_valid(const DmMc02GpioExti *state)
{
    if (!state || !state->gpio_count ||
        state->gpio_count > DM_MC02_GPIO_EXTI_MAX_BANKS) {
        return false;
    }
    for (unsigned i = 0; i < state->gpio_count; ++i) {
        if (state->gpio[i].bank_index != i) {
            return false;
        }
    }
    return true;
}

void dm_mc02_gpio_exti_set_input_sync(
    DmMc02GpioExti *state, DmMc02GpioExtiInputSync *callback, void *opaque)
{
    if (!state) {
        return;
    }
    state->input_sync = callback;
    state->input_sync_opaque = opaque;
}

void dm_mc02_gpio_exti_sync_runtime(DmMc02GpioExti *state)
{
    if (!state || !dm_mc02_gpio_exti_state_valid(state)) {
        return;
    }

    /* ODR consumers observe only after all GPIO and SYSCFG fields are loaded. */
    for (unsigned i = 0; i < state->gpio_count; ++i) {
        DmMc02GpioBank *gpio = &state->gpio[i];

        if (gpio->odr_changed) {
            gpio->odr_changed(gpio->odr_changed_opaque, gpio->bank_index,
                              gpio->odr);
        }
    }

    /* Board input injection owns electrical source state.  It can use the
     * restored SYSCFG mapping without this generic layer guessing pull-up or
     * alternate-function behavior. */
    if (state->input_sync) {
        state->input_sync(state->input_sync_opaque);
    }
    dm_mc02_exti_sync_runtime(&state->exti);
}

static int dm_mc02_gpio_exti_post_load(void *opaque, int version_id)
{
    DmMc02GpioExti *state = opaque;

    if (!state || version_id != 1 ||
        !dm_mc02_gpio_exti_state_valid(state)) {
        return -EINVAL;
    }
    dm_mc02_gpio_exti_sync_runtime(state);
    return 0;
}

static int dm_mc02_gpio_exti_pre_save(void *opaque)
{
    return dm_mc02_gpio_exti_state_valid(opaque) ? 0 : -EINVAL;
}

static const VMStateDescription vmstate_dm_mc02_gpio_exti = {
    .name = "dm-mc02-gpio-exti",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_gpio_exti_pre_save,
    .post_load = dm_mc02_gpio_exti_post_load,
    .fields = (VMStateField[]) {
        /* Producer-to-consumer order: GPIO state, route selection, pending
         * edge state.  Raw children intentionally have no post-load hooks. */
        VMSTATE_STRUCT_ARRAY(gpio, DmMc02GpioExti,
                             DM_MC02_GPIO_EXTI_MAX_BANKS, 0,
                             vmstate_dm_mc02_gpio_raw, DmMc02GpioBank),
        VMSTATE_STRUCT(syscfg, DmMc02GpioExti, 0,
                       vmstate_dm_mc02_syscfg_raw, DmMc02Syscfg),
        VMSTATE_STRUCT(exti, DmMc02GpioExti, 0,
                       vmstate_dm_mc02_exti_raw, DmMc02Exti),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_gpio_exti_vmstate(void)
{
    return &vmstate_dm_mc02_gpio_exti;
}
