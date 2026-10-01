/* Focused active-low external NRST input contract test. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_reset_input.h"
#include "hw/irq.h"
#include "hw/qdev-core.h"
#include "hw/sysbus.h"
#include "qemu/module.h"

static const TypeInfo test_sysbus_device_type = {
    .name = TYPE_SYS_BUS_DEVICE,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(SysBusDevice),
};

static void reset_asserted(void *opaque)
{
    unsigned *count = opaque;

    (*count)++;
}

static void set_nrst(DmMc02ResetInput *state, int level)
{
    qemu_set_irq(qdev_get_gpio_in_named(DEVICE(state), "NRST", 0), level);
}

static void test_active_low_assertion_is_edge_triggered(void)
{
    DmMc02ResetInput *state =
        DM_MC02_RESET_INPUT(object_new(TYPE_DM_MC02_RESET_INPUT));
    unsigned assertions = 0;

    dm_mc02_reset_input_set_callback(state, reset_asserted, &assertions);
    g_assert_true(state->nrst_high);

    set_nrst(state, 1);
    set_nrst(state, 0);
    g_assert_cmpuint(assertions, ==, 1);
    g_assert_false(state->nrst_high);

    /* A held-low external input is not a stream of reset requests. */
    set_nrst(state, 0);
    dm_mc02_reset_input_reset(state);
    set_nrst(state, 0);
    g_assert_cmpuint(assertions, ==, 1);
    g_assert_false(state->nrst_high);

    set_nrst(state, 1);
    set_nrst(state, 0);
    g_assert_cmpuint(assertions, ==, 2);

    object_unref(OBJECT(state));
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    /* The isolated target deliberately does not link QEMU's system-bus
     * implementation.  This minimal parent supplies only the QOM ancestry
     * needed by the input component; GPIO behavior comes from the real
     * qdev helper linked below. */
    type_register_static(&test_sysbus_device_type);
    module_call_init(MODULE_INIT_QOM);
    g_test_add_func("/dm-mc02/reset-input/active-low-edge",
                    test_active_low_assertion_is_edge_triggered);
    return g_test_run();
}
