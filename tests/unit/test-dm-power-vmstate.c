/* Focused VMState contract test for the board-independent power model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_power.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

static int temp_fd;
static unsigned adc_source_updates;
static uint16_t adc_vin_channel;
static uint16_t adc_vin_raw;
static uint16_t adc_key_channel;
static uint16_t adc_key_raw;
static unsigned brownout_events;

static void brownout_callback(void *opaque)
{
    unsigned *events = opaque;

    (*events)++;
}

/* The isolated target does not link the ADC implementation.  This stub
 * observes the board-independent power -> ADC source boundary only. */
void dm_mc02_adc_set_board_source_raw(DmMc02Adc *state, uint16_t channel,
                                      uint16_t raw)
{
    (void)state;
    adc_source_updates++;
    if (channel == 4) {
        adc_vin_channel = channel;
        adc_vin_raw = raw;
    } else if (channel == 19) {
        adc_key_channel = channel;
        adc_key_raw = raw;
    }
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

static void save_state(const DmMc02Power *state,
                       const VMStateDescription *description)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, description, (void *)state,
                                       NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Power *state,
                      const VMStateDescription *description)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, description, state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void prepare_source(DmMc02Power *state)
{
    dm_mc02_power_init(state, NULL);
    dm_mc02_power_set_wiring(state,
                             UINT32_C(1) << 14,
                             UINT32_C(1) << 13,
                             UINT32_C(1) << 15,
                             4, 19);
    dm_mc02_power_set_electrical_policy(state, true);
    dm_mc02_power_set_vin_mv(state, 24000);
    dm_mc02_power_set_gpio_odr(state,
                               (UINT32_C(1) << 14) |
                               (UINT32_C(1) << 13) |
                               (UINT32_C(1) << 15));
}

static void prepare_destination(DmMc02Power *state, DmMc02Adc *adc)
{
    dm_mc02_power_init(state, adc);
    dm_mc02_power_set_wiring(state,
                             UINT32_C(1) << 14,
                             UINT32_C(1) << 13,
                             UINT32_C(1) << 15,
                             4, 19);
}

static void test_round_trip_reprojects_rails_and_adc(void)
{
    DmMc02Power source;
    DmMc02Power restored;
    DmMc02Adc adc;

    prepare_source(&source);
    save_state(&source, dm_mc02_power_vmstate());

    memset(&adc, 0, sizeof(adc));
    prepare_destination(&restored, &adc);
    adc_source_updates = 0;
    g_assert_cmpint(load_state(&restored, dm_mc02_power_vmstate()), ==, 0);

    g_assert_cmpuint(restored.vin_mv, ==, source.vin_mv);
    g_assert_cmpuint(restored.gpio_odr, ==, source.gpio_odr);
    g_assert_true(restored.electrical_power);
    g_assert_cmpint(dm_mc02_power_get_state(&restored), ==,
                    DM_MC02_POWER_NORMAL);
    g_assert_true(dm_mc02_power_get_out1_enabled(&restored));
    g_assert_true(dm_mc02_power_get_out2_enabled(&restored));
    g_assert_true(dm_mc02_power_get_5v_enabled(&restored));
    g_assert_true(dm_mc02_power_get_system_5v_good(&restored));
    g_assert_true(dm_mc02_power_get_system_3v3_good(&restored));
    g_assert_true(dm_mc02_power_get_out1_good(&restored));
    g_assert_true(dm_mc02_power_get_out2_good(&restored));
    g_assert_cmpuint(adc_source_updates, ==, 2);
    g_assert_cmpuint(adc_vin_channel, ==, 4);
    g_assert_cmpuint(adc_key_channel, ==, 19);
    g_assert_cmpuint(adc_vin_raw, ==,
                     dm_mc02_power_get_adc_source_raw(&restored, 4));
    g_assert_cmpuint(adc_key_raw, ==,
                     dm_mc02_power_get_adc_source_raw(&restored, 19));
}

static void test_raw_load_does_not_reproject_runtime(void)
{
    DmMc02Power source;
    DmMc02Power restored;
    DmMc02Adc adc;

    prepare_source(&source);
    save_state(&source, dm_mc02_power_vmstate_raw());

    memset(&adc, 0, sizeof(adc));
    prepare_destination(&restored, &adc);
    adc_source_updates = 0;
    g_assert_cmpint(load_state(&restored, dm_mc02_power_vmstate_raw()), ==, 0);

    g_assert_cmpuint(restored.vin_mv, ==, source.vin_mv);
    g_assert_cmpuint(restored.gpio_odr, ==, source.gpio_odr);
    g_assert_true(restored.electrical_power);
    g_assert_cmpuint(adc_source_updates, ==, 0);
    /* The raw child does not overwrite derived destination state. */
    g_assert_false(restored.out1_enabled);
    g_assert_false(restored.out2_enabled);
    g_assert_false(restored.switched_5v_enabled);
    g_assert_false(restored.out1_good);
    g_assert_false(restored.out2_good);
}

static void test_rejects_unmasked_gpio_odr_without_projection(void)
{
    DmMc02Power source;
    DmMc02Power restored;
    DmMc02Adc adc;

    prepare_source(&source);
    source.gpio_odr = UINT32_C(0x10000);
    save_state(&source, dm_mc02_power_vmstate());

    memset(&adc, 0, sizeof(adc));
    prepare_destination(&restored, &adc);
    adc_source_updates = 0;
    g_assert_cmpint(load_state(&restored, dm_mc02_power_vmstate()), !=, 0);
    g_assert_cmpuint(adc_source_updates, ==, 0);
}

static void test_truncated_state_is_rejected_without_projection(void)
{
    DmMc02Power state;
    DmMc02Adc adc;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&adc, 0, sizeof(adc));
    prepare_destination(&state, &adc);
    adc_source_updates = 0;
    g_assert_cmpint(load_state(&state, dm_mc02_power_vmstate()), !=, 0);
    g_assert_cmpuint(adc_source_updates, ==, 0);
}

static void test_brownout_callback_only_fires_on_downward_crossing(void)
{
    DmMc02Power power;

    dm_mc02_power_init(&power, NULL);
    /* Board construction may establish a low input before it connects the
     * runtime brownout consumer; that initial condition is not an edge. */
    dm_mc02_power_set_vin_mv(&power, 11999);
    brownout_events = 0;
    dm_mc02_power_set_brownout_callback(&power, brownout_callback,
                                        &brownout_events);

    dm_mc02_power_set_vin_mv(&power, 24000);
    g_assert_cmpuint(brownout_events, ==, 0);

    dm_mc02_power_set_vin_mv(&power, 11999);
    g_assert_cmpuint(brownout_events, ==, 1);
    /* Holding undervoltage and recovering do not repeat the event. */
    dm_mc02_power_set_vin_mv(&power, 10000);
    dm_mc02_power_set_vin_mv(&power, 24000);
    g_assert_cmpuint(brownout_events, ==, 1);

    /* Power removal has a distinct OFF boundary. */
    dm_mc02_power_set_vin_mv(&power, 0);
    g_assert_cmpuint(brownout_events, ==, 1);

    /* Runtime callback wiring survives board reset and can observe the next
     * normal-to-undervoltage crossing. */
    dm_mc02_power_reset(&power);
    dm_mc02_power_set_vin_mv(&power, 24000);
    dm_mc02_power_set_vin_mv(&power, 11999);
    g_assert_cmpuint(brownout_events, ==, 2);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-power-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-power-vmstate/round-trip-reproject",
                    test_round_trip_reprojects_rails_and_adc);
    g_test_add_func("/dm-power-vmstate/raw-no-reproject",
                    test_raw_load_does_not_reproject_runtime);
    g_test_add_func("/dm-power-vmstate/reject-unmasked-gpio",
                    test_rejects_unmasked_gpio_odr_without_projection);
    g_test_add_func("/dm-power-vmstate/reject-truncated",
                    test_truncated_state_is_rejected_without_projection);
    g_test_add_func("/dm-power/brownout-crossing",
                    test_brownout_callback_only_fires_on_downward_crossing);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
