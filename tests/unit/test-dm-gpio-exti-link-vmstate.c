/* Focused VMState contract test for the GPIO/SYSCFG/EXTI SoC boundary. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_gpio_exti.h"
#include "hw/irq.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

#define EXTI_PR1 0x14
#define EXTI_C1IMR1 0x80

static int temp_fd;
static unsigned odr_sync_count;
static unsigned input_sync_count;
static uint32_t observed_odr[DM_MC02_GPIO_EXTI_MAX_BANKS];

static void nvic_input_changed(void *opaque, int n, int level)
{
    bool *levels = opaque;

    g_assert_cmpint(n, >=, 0);
    g_assert_cmpint(n, <, 4);
    levels[n] = level;
}

static QEMUFile *open_test_file(bool write)
{
    int fd = dup(temp_fd);
    QIOChannel *ioc;
    QEMUFile *file;

    g_assert_cmpint(fd, >=, 0);
    g_assert_cmpint(lseek(fd, 0, SEEK_SET), ==, 0);
    if (write) {
        g_assert_cmpint(ftruncate(fd, 0), ==, 0);
    }
    ioc = QIO_CHANNEL(qio_channel_file_new_fd(fd));
    file = write ? qemu_file_new_output(ioc) : qemu_file_new_input(ioc);
    object_unref(OBJECT(ioc));
    return file;
}

static void odr_changed(void *opaque, unsigned bank, uint32_t odr)
{
    unsigned *count = opaque;

    g_assert_cmpuint(bank, <, DM_MC02_GPIO_EXTI_MAX_BANKS);
    (*count)++;
    observed_odr[bank] = odr;
}

static void input_sync(void *opaque)
{
    DmMc02GpioExti *state = opaque;

    /* The callback is the board input boundary, and therefore must observe
     * the restored SYSCFG route before it updates sampled inputs. */
    g_assert_cmpuint(dm_mc02_syscfg_get_exti_port(&state->syscfg, 0), ==, 1);
    input_sync_count++;
}

static void save_state(const DmMc02GpioExti *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_gpio_exti_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02GpioExti *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_gpio_exti_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void prepare_source(DmMc02GpioExti *state)
{
    memset(state, 0, sizeof(*state));
    state->gpio_count = 2;
    for (unsigned i = 0; i < state->gpio_count; ++i) {
        state->gpio[i].bank_index = i;
        state->gpio[i].moder = UINT32_C(0x10001) * (i + 1);
        state->gpio[i].odr = UINT32_C(0x40) + i;
        state->gpio[i].idr = UINT32_C(0x80) + i;
    }

    /* EXTI0 is sourced from GPIOB. */
    state->syscfg.regs[0x08] = 1;
    state->exti.regs[EXTI_PR1] = 1;
    state->exti.regs[EXTI_C1IMR1] = 1;
    state->exti.line_level = 1;
}

static void prepare_destination(DmMc02GpioExti *state)
{
    memset(state, 0, sizeof(*state));
    state->gpio_count = 2;
    for (unsigned i = 0; i < state->gpio_count; ++i) {
        state->gpio[i].bank_index = i;
        state->gpio[i].odr_changed = odr_changed;
        state->gpio[i].odr_changed_opaque = &odr_sync_count;
    }
    dm_mc02_gpio_exti_set_input_sync(state, input_sync, state);
}

static void test_round_trip_orders_children_before_projection(void)
{
    DmMc02GpioExti source;
    DmMc02GpioExti restored;

    prepare_source(&source);
    save_state(&source);

    prepare_destination(&restored);
    odr_sync_count = 0;
    input_sync_count = 0;
    memset(observed_odr, 0, sizeof(observed_odr));
    g_assert_cmpint(load_state(&restored), ==, 0);

    for (unsigned i = 0; i < restored.gpio_count; ++i) {
        g_assert_cmpuint(restored.gpio[i].moder, ==, source.gpio[i].moder);
        g_assert_cmpuint(restored.gpio[i].odr, ==, source.gpio[i].odr);
        g_assert_cmpuint(restored.gpio[i].idr, ==, source.gpio[i].idr);
        g_assert_cmpuint(observed_odr[i], ==, source.gpio[i].odr);
    }
    g_assert_cmpuint(restored.syscfg.regs[0x08], ==, 1);
    g_assert_cmpuint(restored.exti.line_level, ==, 1);
    g_assert_true(restored.exti.irq_level[0]);
    g_assert_cmpuint(odr_sync_count, ==, 2);
    g_assert_cmpuint(input_sync_count, ==, 1);
}

static void test_rejects_invalid_destination_without_projection(void)
{
    DmMc02GpioExti source;
    DmMc02GpioExti restored;

    prepare_source(&source);
    save_state(&source);
    prepare_destination(&restored);
    restored.gpio_count = 0;
    odr_sync_count = 0;
    input_sync_count = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_cmpuint(odr_sync_count, ==, 0);
    g_assert_cmpuint(input_sync_count, ==, 0);
}

static void test_rejects_truncated_state_without_projection(void)
{
    DmMc02GpioExti state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    prepare_destination(&state);
    odr_sync_count = 0;
    input_sync_count = 0;
    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_cmpuint(odr_sync_count, ==, 0);
    g_assert_cmpuint(input_sync_count, ==, 0);
}

static void test_nvic_route_validation_is_atomic(void)
{
    DmMc02Exti exti;
    DmMc02ExtiIrqRoute valid;
    DmMc02ExtiIrqRoute invalid[2];
    bool levels[4] = { false };
    Error *error = NULL;
    qemu_irq *inputs;

    memset(&exti, 0, sizeof(exti));
    exti.regs[EXTI_PR1] = 1;
    exti.regs[EXTI_C1IMR1] = 1;
    inputs = qemu_allocate_irqs(nvic_input_changed, levels, 4);

    valid = (DmMc02ExtiIrqRoute) {
        .exti_group = 0,
        .controller_input = 40,
        .controller_irq = inputs[1],
    };
    g_assert_true(dm_mc02_exti_connect_nvic(&exti, &valid, 1, 64, &error));
    g_assert_null(error);
    g_assert_true(levels[1]);
    g_assert_true(exti.irq[0] == inputs[1]);

    invalid[0] = (DmMc02ExtiIrqRoute) {
        .exti_group = 0,
        .controller_input = 41,
        .controller_irq = inputs[2],
    };
    invalid[1] = (DmMc02ExtiIrqRoute) {
        .exti_group = DM_MC02_EXTI_IRQ_GROUP_COUNT,
        .controller_input = 42,
        .controller_irq = inputs[3],
    };
    g_assert_false(dm_mc02_exti_connect_nvic(&exti, invalid, 2, 64, &error));
    g_assert_nonnull(error);
    error_free(error);
    g_assert_true(exti.irq[0] == inputs[1]);
    g_assert_true(levels[1]);

    qemu_free_irqs(inputs, 4);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf(
        "%s/dm-gpio-exti-link-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-gpio-exti-link-vmstate/round-trip",
                    test_round_trip_orders_children_before_projection);
    g_test_add_func("/dm-gpio-exti-link-vmstate/reject-invalid",
                    test_rejects_invalid_destination_without_projection);
    g_test_add_func("/dm-gpio-exti-link-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_projection);
    g_test_add_func("/dm-gpio-exti-link-vmstate/nvic-route-atomic",
                    test_nvic_route_validation_is_atomic);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
