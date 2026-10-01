/* Focused VMState contract test for the composite SPI/BMI088 link. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088_spi_link.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_spi_sync_calls;
extern unsigned dm_mc02_spi_restore_selected_mask_calls;

static int temp_fd;
static unsigned target_select_calls;

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

static int save_link(DmMc02Bmi088SpiLink *state)
{
    QEMUFile *file = open_test_file(true);
    int ret = vmstate_save_state(file, dm_mc02_bmi088_spi_link_vmstate(),
                                 state, NULL);

    if (!ret) {
        qemu_put_byte(file, QEMU_VM_EOF);
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static int load_link(DmMc02Bmi088SpiLink *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_bmi088_spi_link_vmstate(),
                                 state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void target_select_sentinel(void *opaque, bool selected,
                                    uint32_t selected_mask)
{
    (void)opaque;
    (void)selected;
    (void)selected_mask;
    target_select_calls++;
}

static void prepare_sensor(DmMc02Bmi088 *state, bool accel)
{
    memset(state, 0, sizeof(*state));
    state->accel = accel;
    state->signal.kind = accel ? DM_MC02_BMI088_SIGNAL_ACCEL :
                                 DM_MC02_BMI088_SIGNAL_GYRO;
    state->signal.full_scale = accel ? 16.0 : 2000.0;
    state->signal.bandwidth_hz = 100.0;
    state->signal.temperature_c = 25.0;
    state->signal.temperature_reference_c = 25.0;
    state->regs[0x02] = accel ? 0x11 : 0x22;
    state->sample_sequence = accel ? 101 : 202;
    state->signal.rng = accel ? 0x12345678 : 0x87654321;
}

static void prepare_source(DmMc02Bmi088SpiLink *state)
{
    memset(state, 0, sizeof(*state));
    state->spi.cr1 = 1;
    state->spi.cr2 = 0x1234;
    state->spi.cfg1 = 0x11223344;
    state->spi.cfg2 = 0x55667788;
    state->spi.transfer_remaining = 0x234;
    state->spi.rx = 0xa5;
    state->spi.rx_valid = true;
    state->spi.eot = true;
    state->spi.dma_tx_next_ns = 5000;
    state->spi.selected_mask = 2;

    prepare_sensor(&state->accel, true);
    prepare_sensor(&state->gyro, false);
    state->accel.fifo_head = 2;
    state->accel.fifo_length = 16;
    state->accel.fifo[2] = 0x84;
    state->gyro.fifo_head = 1;
    state->gyro.fifo_length = 8;
    state->accel_spi.command_seen = true;
    state->accel_spi.read_transfer = true;
    state->accel_spi.dummy_pending = true;
    state->accel_spi.reg = 0x19;
    state->accel_spi.read_start_reg = 0x12;
    state->gyro_spi.command_seen = true;
    state->gyro_spi.read_transfer = false;
    state->gyro_spi.reg = 0x40;
    state->gyro_spi.read_start_reg = 0x40;
}

static void seed_destination_runtime(DmMc02Bmi088SpiLink *state)
{
    memset(state, 0, sizeof(*state));
    prepare_sensor(&state->accel, true);
    prepare_sensor(&state->gyro, false);
    state->accel_spi.bmi = &state->accel;
    state->gyro_spi.bmi = &state->gyro;
    state->spi.selected_mask = 1;
    state->spi.dma_request_active = true;
    state->spi.targets[0].select = target_select_sentinel;
    state->spi.targets[0].opaque = state;
    state->spi.targets[1].select = target_select_sentinel;
    state->spi.targets[1].opaque = state;
}

static void test_round_trip_restores_link_without_cs_callback(void)
{
    DmMc02Bmi088SpiLink source;
    DmMc02Bmi088SpiLink restored;

    prepare_source(&source);
    g_assert_cmpint(save_link(&source), ==, 0);

    seed_destination_runtime(&restored);
    target_select_calls = 0;
    dm_mc02_spi_sync_calls = 0;
    dm_mc02_spi_restore_selected_mask_calls = 0;
    g_assert_cmpint(load_link(&restored), ==, 0);

    g_assert_cmpuint(restored.spi.cr1, ==, source.spi.cr1);
    g_assert_cmpuint(restored.spi.transfer_remaining, ==,
                     source.spi.transfer_remaining);
    g_assert_cmpuint(restored.spi.rx, ==, source.spi.rx);
    g_assert_true(restored.spi.rx_valid);
    g_assert_true(restored.spi.eot);
    g_assert_cmpuint(restored.spi.dma_tx_next_ns, ==,
                     source.spi.dma_tx_next_ns);
    g_assert_cmpuint(restored.spi.selected_mask, ==, 2);
    g_assert_cmpuint(restored.selected_mask_snapshot, ==, 2);
    g_assert_cmpuint(restored.accel.regs[0x02], ==, 0x11);
    g_assert_cmpuint(restored.gyro.regs[0x02], ==, 0x22);
    g_assert_cmpuint(restored.accel.sample_sequence, ==, 101);
    g_assert_cmpuint(restored.gyro.sample_sequence, ==, 202);
    g_assert_true(restored.accel_spi.bmi == &restored.accel);
    g_assert_true(restored.gyro_spi.bmi == &restored.gyro);
    g_assert_true(restored.spi.targets[0].select == target_select_sentinel);
    g_assert_true(restored.spi.targets[1].select == target_select_sentinel);
    g_assert_false(restored.spi.dma_request_active);
    g_assert_cmpuint(target_select_calls, ==, 0);
    /* The raw SPI child must not synchronize independently; the composite
     * activates it exactly once after all sensor/framer state is valid. */
    g_assert_cmpuint(dm_mc02_spi_restore_selected_mask_calls, ==, 1);
    g_assert_cmpuint(dm_mc02_spi_sync_calls, ==, 1);
}

static void test_rejects_mismatched_static_die_before_runtime_sync(void)
{
    DmMc02Bmi088SpiLink source;
    DmMc02Bmi088SpiLink restored;
    DmMc02Bmi088 *accel;
    DmMc02Bmi088Spi *framer;

    prepare_source(&source);
    g_assert_cmpint(save_link(&source), ==, 0);
    seed_destination_runtime(&restored);
    accel = &restored.accel;
    framer = &restored.accel_spi;
    accel->accel = false;
    accel->signal.kind = DM_MC02_BMI088_SIGNAL_GYRO;
    dm_mc02_spi_sync_calls = 0;
    dm_mc02_spi_restore_selected_mask_calls = 0;
    target_select_calls = 0;

    g_assert_cmpint(load_link(&restored), !=, 0);
    g_assert_true(restored.accel_spi.bmi == &restored.accel);
    g_assert_true(restored.accel_spi.bmi == accel);
    g_assert_true(framer == &restored.accel_spi);
    g_assert_cmpuint(dm_mc02_spi_sync_calls, ==, 0);
    g_assert_cmpuint(dm_mc02_spi_restore_selected_mask_calls, ==, 0);
    g_assert_cmpuint(target_select_calls, ==, 0);
}

static void test_rejects_invalid_selection_on_save(void)
{
    DmMc02Bmi088SpiLink state;

    prepare_source(&state);
    state.spi.selected_mask = UINT32_C(1) << DM_MC02_SPI_MAX_TARGETS;
    g_assert_cmpint(save_link(&state), !=, 0);
}

static void test_rejects_truncated_link_without_runtime_sync(void)
{
    DmMc02Bmi088SpiLink state;
    QEMUFile *file = open_test_file(true);
    static const uint8_t truncated[] = { 0x01, 0x02, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    seed_destination_runtime(&state);
    dm_mc02_spi_sync_calls = 0;
    dm_mc02_spi_restore_selected_mask_calls = 0;
    target_select_calls = 0;

    g_assert_cmpint(load_link(&state), !=, 0);
    g_assert_cmpuint(dm_mc02_spi_sync_calls, ==, 0);
    g_assert_cmpuint(dm_mc02_spi_restore_selected_mask_calls, ==, 0);
    g_assert_cmpuint(target_select_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf(
        "%s/dm-bmi088-spi-link-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-bmi088-spi-link-vmstate/round-trip-no-cs-callback",
                    test_round_trip_restores_link_without_cs_callback);
    g_test_add_func("/dm-bmi088-spi-link-vmstate/reject-static-kind",
                    test_rejects_mismatched_static_die_before_runtime_sync);
    g_test_add_func("/dm-bmi088-spi-link-vmstate/reject-invalid-selection",
                    test_rejects_invalid_selection_on_save);
    g_test_add_func("/dm-bmi088-spi-link-vmstate/reject-truncated",
                    test_rejects_truncated_link_without_runtime_sync);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
