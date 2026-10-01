/* Focused VMState contract test for the board-independent BMI088 model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

static int temp_fd;

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

static void save_state(const DmMc02Bmi088 *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_bmi088_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Bmi088 *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_bmi088_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void init_destination_config(DmMc02Bmi088 *state, bool accel,
                                    DmMc02Bmi088SignalKind kind)
{
    memset(state, 0, sizeof(*state));
    state->accel = accel;
    state->signal.kind = kind;
}

static void prepare_state(DmMc02Bmi088 *state, bool accel,
                          bool sensor_time_cursor)
{
    memset(state, 0, sizeof(*state));
    state->accel = accel;
    for (unsigned i = 0; i < ARRAY_SIZE(state->regs); ++i) {
        state->regs[i] = (uint8_t)(1u + ((i * 37u) % 255u));
    }

    state->signal.kind = accel ? DM_MC02_BMI088_SIGNAL_ACCEL
                               : DM_MC02_BMI088_SIGNAL_GYRO;
    state->signal.noise_std = 0.125;
    state->signal.full_scale = accel ? 24.0 : 2000.0;
    state->signal.temperature_c = 31.25;
    state->signal.temperature_reference_c = 25.0;
    state->signal.rng = UINT32_C(0x7f31a9c5);
    state->signal.bandwidth_hz = 145.0;
    state->signal.filter_time_ns = UINT64_C(0x1020304050607080);
    state->signal.filter_valid = true;
    state->signal.odr_period_ns = UINT64_C(125000);
    state->signal.next_sample_time_ns = UINT64_C(0x2233445566778899);
    state->signal.sample_time_valid = false;
    state->signal.bias_time_ns = UINT64_C(0x8877665544332211);
    state->signal.bias_time_valid = true;
    for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
        state->signal.bias[i] = -1.25 + 0.75 * i;
        state->signal.temperature_coefficient[i] = 0.015 + 0.01 * i;
        state->signal.bias_random_walk_std[i] = 0.001 + 0.002 * i;
        state->signal.dynamic_bias[i] = -0.03125 + 0.0625 * i;
        state->signal.filter_state[i] = 10.5 + 1.25 * i;
    }

    for (unsigned i = 0; i < ARRAY_SIZE(state->fifo); ++i) {
        state->fifo[i] = (uint8_t)(1u + (i % 251u));
        state->fifo_sequence[i] = UINT64_C(0x1000000000000001) +
                                  UINT64_C(0x01010101) * i;
    }

    state->fifo_downsample_count = 5;
    state->fifo_skipped_frames = 0x1234;
    state->fifo_overrun = true;
    state->sample_sequence = UINT64_C(0xabcdef0123456789);
    state->gyro_drdy_clear_time_ns = UINT64_C(0x9988776655443322);

    if (accel && sensor_time_cursor) {
        state->fifo_head = 41;
        state->fifo_length = 0;
        state->fifo_read_progress = 2;
        state->fifo_read_frame_length = 4;
        state->fifo_read_kind = 3;
        state->fifo_read_sequence = 0;
        state->fifo_sensortime_pending = true;
    } else if (accel) {
        state->fifo_head = 37;
        state->fifo_length = 73;
        state->fifo[state->fifo_head] = 0x84;
        state->fifo_read_progress = 3;
        state->fifo_read_frame_length = 7;
        state->fifo_read_kind = 1;
        state->fifo_read_sequence = state->fifo_sequence[state->fifo_head];
        state->fifo_sensortime_pending = false;
    } else {
        state->fifo_head = 37;
        state->fifo_length = 64;
        state->fifo_read_progress = 3;
        state->fifo_read_frame_length = 8;
        state->fifo_read_kind = 1;
        state->fifo_read_sequence = state->fifo_sequence[state->fifo_head];
        state->fifo_sensortime_pending = false;
    }
}

static void assert_round_trip(const DmMc02Bmi088 *source)
{
    DmMc02Bmi088 restored;
    DmMc02Bmi088SignalKind kind = source->accel
        ? DM_MC02_BMI088_SIGNAL_ACCEL
        : DM_MC02_BMI088_SIGNAL_GYRO;

    save_state(source);
    /* Die identity and signal kind are destination-side static config. */
    init_destination_config(&restored, source->accel, kind);
    g_assert_cmpint(load_state(&restored), ==, 0);

    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source->regs,
                    sizeof(source->regs));
    g_assert_cmpmem(&restored.signal.noise_std,
                    sizeof(restored.signal.noise_std),
                    &source->signal.noise_std,
                    sizeof(source->signal.noise_std));
    g_assert_cmpmem(restored.signal.bias, sizeof(restored.signal.bias),
                    source->signal.bias, sizeof(source->signal.bias));
    g_assert_cmpmem(restored.signal.temperature_coefficient,
                    sizeof(restored.signal.temperature_coefficient),
                    source->signal.temperature_coefficient,
                    sizeof(source->signal.temperature_coefficient));
    g_assert_cmpmem(restored.signal.bias_random_walk_std,
                    sizeof(restored.signal.bias_random_walk_std),
                    source->signal.bias_random_walk_std,
                    sizeof(source->signal.bias_random_walk_std));
    g_assert_cmpmem(restored.signal.dynamic_bias,
                    sizeof(restored.signal.dynamic_bias),
                    source->signal.dynamic_bias,
                    sizeof(source->signal.dynamic_bias));
    g_assert_cmpmem(&restored.signal.full_scale,
                    sizeof(restored.signal.full_scale),
                    &source->signal.full_scale,
                    sizeof(source->signal.full_scale));
    g_assert_cmpmem(&restored.signal.temperature_c,
                    sizeof(restored.signal.temperature_c),
                    &source->signal.temperature_c,
                    sizeof(source->signal.temperature_c));
    g_assert_cmpmem(&restored.signal.temperature_reference_c,
                    sizeof(restored.signal.temperature_reference_c),
                    &source->signal.temperature_reference_c,
                    sizeof(source->signal.temperature_reference_c));
    g_assert_cmpuint(restored.signal.rng, ==, source->signal.rng);
    g_assert_cmpmem(&restored.signal.bandwidth_hz,
                    sizeof(restored.signal.bandwidth_hz),
                    &source->signal.bandwidth_hz,
                    sizeof(source->signal.bandwidth_hz));
    g_assert_cmpmem(restored.signal.filter_state,
                    sizeof(restored.signal.filter_state),
                    source->signal.filter_state,
                    sizeof(source->signal.filter_state));
    g_assert_cmpuint(restored.signal.filter_time_ns, ==,
                     source->signal.filter_time_ns);
    g_assert_cmpint(restored.signal.filter_valid, ==,
                    source->signal.filter_valid);
    g_assert_cmpuint(restored.signal.odr_period_ns, ==,
                     source->signal.odr_period_ns);
    g_assert_cmpuint(restored.signal.next_sample_time_ns, ==,
                     source->signal.next_sample_time_ns);
    g_assert_cmpint(restored.signal.sample_time_valid, ==,
                    source->signal.sample_time_valid);
    g_assert_cmpuint(restored.signal.bias_time_ns, ==,
                     source->signal.bias_time_ns);
    g_assert_cmpint(restored.signal.bias_time_valid, ==,
                    source->signal.bias_time_valid);
    g_assert_cmpmem(restored.fifo, sizeof(restored.fifo), source->fifo,
                    sizeof(source->fifo));
    g_assert_cmpmem(restored.fifo_sequence, sizeof(restored.fifo_sequence),
                    source->fifo_sequence, sizeof(source->fifo_sequence));
    g_assert_cmpuint(restored.fifo_head, ==, source->fifo_head);
    g_assert_cmpuint(restored.fifo_length, ==, source->fifo_length);
    g_assert_cmpuint(restored.fifo_read_progress, ==,
                     source->fifo_read_progress);
    g_assert_cmpuint(restored.fifo_read_frame_length, ==,
                     source->fifo_read_frame_length);
    g_assert_cmpuint(restored.fifo_read_kind, ==, source->fifo_read_kind);
    g_assert_cmpuint(restored.fifo_read_sequence, ==,
                     source->fifo_read_sequence);
    g_assert_cmpuint(restored.fifo_downsample_count, ==,
                     source->fifo_downsample_count);
    g_assert_cmpuint(restored.fifo_skipped_frames, ==,
                     source->fifo_skipped_frames);
    g_assert_cmpint(restored.fifo_sensortime_pending, ==,
                    source->fifo_sensortime_pending);
    g_assert_cmpint(restored.fifo_overrun, ==, source->fifo_overrun);
    g_assert_cmpuint(restored.sample_sequence, ==, source->sample_sequence);
    g_assert_cmpuint(restored.gyro_drdy_clear_time_ns, ==,
                     source->gyro_drdy_clear_time_ns);
}

static void test_round_trip_restores_accel_and_gyro_state(void)
{
    DmMc02Bmi088 source;

    prepare_state(&source, true, false);
    assert_round_trip(&source);

    /* The sensor-time cursor is a separate legal accelerometer read mode. */
    prepare_state(&source, true, true);
    assert_round_trip(&source);

    prepare_state(&source, false, false);
    assert_round_trip(&source);
}

static void assert_rejected(const DmMc02Bmi088 *source, bool accel,
                            DmMc02Bmi088SignalKind kind)
{
    DmMc02Bmi088 restored;

    save_state(source);
    init_destination_config(&restored, accel, kind);
    g_assert_cmpint(load_state(&restored), !=, 0);
}

static void test_rejects_invalid_destination_signal_kind(void)
{
    DmMc02Bmi088 source;

    prepare_state(&source, true, false);
    assert_rejected(&source, true, (DmMc02Bmi088SignalKind)99);
}

static void test_rejects_mismatched_destination_signal_kind(void)
{
    DmMc02Bmi088 source;

    prepare_state(&source, true, false);
    assert_rejected(&source, true, DM_MC02_BMI088_SIGNAL_GYRO);
}

static void test_rejects_invalid_fifo_cursor(void)
{
    DmMc02Bmi088 source;

    prepare_state(&source, true, false);
    source.fifo_read_progress = source.fifo_read_frame_length + 1;
    assert_rejected(&source, true, DM_MC02_BMI088_SIGNAL_ACCEL);

    prepare_state(&source, true, false);
    source.fifo_head = DM_MC02_BMI_ACCEL_FIFO_CAPACITY;
    assert_rejected(&source, true, DM_MC02_BMI088_SIGNAL_ACCEL);

    prepare_state(&source, true, false);
    source.fifo_read_frame_length = 2;
    assert_rejected(&source, true, DM_MC02_BMI088_SIGNAL_ACCEL);

    prepare_state(&source, true, false);
    source.fifo_read_sequence++;
    assert_rejected(&source, true, DM_MC02_BMI088_SIGNAL_ACCEL);
}

static void test_rejects_truncated_stream(void)
{
    DmMc02Bmi088 source;
    DmMc02Bmi088 restored;
    struct stat statbuf;

    prepare_state(&source, true, false);
    save_state(&source);
    g_assert_cmpint(fstat(temp_fd, &statbuf), ==, 0);
    g_assert_cmpint(statbuf.st_size, >, 2);
    g_assert_cmpint(ftruncate(temp_fd, statbuf.st_size - 2), ==, 0);

    init_destination_config(&restored, true, DM_MC02_BMI088_SIGNAL_ACCEL);
    g_assert_cmpint(load_state(&restored), !=, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file =
        g_strdup_printf("%s/dm-bmi088-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-bmi088-vmstate/round-trip",
                    test_round_trip_restores_accel_and_gyro_state);
    g_test_add_func("/dm-bmi088-vmstate/reject-invalid-destination-kind",
                    test_rejects_invalid_destination_signal_kind);
    g_test_add_func("/dm-bmi088-vmstate/reject-mismatched-destination-kind",
                    test_rejects_mismatched_destination_signal_kind);
    g_test_add_func("/dm-bmi088-vmstate/reject-invalid-cursor",
                    test_rejects_invalid_fifo_cursor);
    g_test_add_func("/dm-bmi088-vmstate/reject-truncated",
                    test_rejects_truncated_stream);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
