/* Component-only VMState for the board-independent BMI088 sensor model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088.h"
#include "migration/vmstate.h"
#include "migration/qemu-file.h"

#include <math.h>

/* VMState has no generic double field.  Preserve the IEEE-754 bit pattern in
 * a fixed byte order instead of serializing the host's double representation. */
static int get_bmi088_double(QEMUFile *f, void *pv, size_t size,
                             const VMStateField *field)
{
    uint64_t bits;

    if (size != sizeof(double)) {
        return -EINVAL;
    }
    bits = qemu_get_be64(f);
    memcpy(pv, &bits, sizeof(bits));
    return 0;
}

static int put_bmi088_double(QEMUFile *f, void *pv, size_t size,
                             const VMStateField *field, JSONWriter *vmdesc)
{
    uint64_t bits;

    if (size != sizeof(double)) {
        return -EINVAL;
    }
    memcpy(&bits, pv, sizeof(bits));
    qemu_put_be64(f, bits);
    return 0;
}

static const VMStateInfo vmstate_info_bmi088_double = {
    .name = "bmi088-double",
    .get = get_bmi088_double,
    .put = put_bmi088_double,
};

#define VMSTATE_BMI088_DOUBLE(_field, _state) \
    VMSTATE_SINGLE(_field, _state, 0, vmstate_info_bmi088_double, double)

#define VMSTATE_BMI088_DOUBLE_ARRAY(_field, _state, _num) \
    VMSTATE_ARRAY(_field, _state, _num, 0, vmstate_info_bmi088_double, double)

static bool bmi088_signal_kind_valid(DmMc02Bmi088SignalKind kind)
{
    return kind == DM_MC02_BMI088_SIGNAL_ACCEL ||
           kind == DM_MC02_BMI088_SIGNAL_GYRO;
}

static bool bmi088_double_valid(double value, bool nonnegative)
{
    return isfinite(value) && (nonnegative ? value >= 0.0 : value > 0.0);
}

static uint8_t bmi088_fifo_frame_length(const DmMc02Bmi088 *state)
{
    uint8_t header;

    if (!state->accel) {
        return 8;
    }
    header = state->fifo[state->fifo_head];
    if (header == 0x84) {
        return 7;
    }
    if (header == 0x40 || header == 0x48 || header == 0x50) {
        return 2;
    }
    return 1;
}

static bool bmi088_fifo_frame_length_valid(const DmMc02Bmi088 *state)
{
    if (!state->fifo_read_kind) {
        return state->fifo_read_progress == 0 &&
               state->fifo_read_frame_length == 0 &&
               state->fifo_read_sequence == 0;
    }
    if (!state->fifo_read_frame_length ||
        state->fifo_read_progress > state->fifo_read_frame_length) {
        return false;
    }
    if (state->fifo_read_kind == 1) {
        return state->fifo_read_frame_length <= state->fifo_length &&
               state->fifo_read_frame_length ==
                   bmi088_fifo_frame_length(state) &&
               state->fifo_read_sequence ==
                   state->fifo_sequence[state->fifo_head];
    }
    if (!state->accel || state->fifo_read_sequence != 0) {
        return false;
    }
    if (state->fifo_read_kind == 2) {
        return state->fifo_skipped_frames != 0 &&
               state->fifo_read_frame_length == 2;
    }
    if (state->fifo_read_kind == 3) {
        return state->fifo_sensortime_pending &&
               state->fifo_read_frame_length == 4;
    }
    return false;
}

static int dm_mc02_bmi088_post_load(void *opaque, int version_id)
{
    DmMc02Bmi088 *state = opaque;
    uint16_t capacity = state->accel ? DM_MC02_BMI_ACCEL_FIFO_CAPACITY :
                                      DM_MC02_BMI_GYRO_FIFO_CAPACITY;
    DmMc02Bmi088SignalKind expected_kind =
        state->accel ? DM_MC02_BMI088_SIGNAL_ACCEL :
                       DM_MC02_BMI088_SIGNAL_GYRO;

    if (version_id != 1 || !bmi088_signal_kind_valid(state->signal.kind) ||
        state->signal.kind != expected_kind ||
        !bmi088_double_valid(state->signal.noise_std, true) ||
        !bmi088_double_valid(state->signal.full_scale, false) ||
        !bmi088_double_valid(state->signal.bandwidth_hz, true) ||
        !isfinite(state->signal.temperature_c) ||
        !isfinite(state->signal.temperature_reference_c) ||
        state->fifo_head >= capacity || state->fifo_length > capacity ||
        (!state->accel && (state->fifo_length % 8)) ||
        state->fifo_read_kind > (state->accel ? 3 : 1) ||
        (!state->accel && state->fifo_sensortime_pending) ||
        !bmi088_fifo_frame_length_valid(state)) {
        return -EINVAL;
    }

    for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
        if (!isfinite(state->signal.bias[i]) ||
            !isfinite(state->signal.temperature_coefficient[i]) ||
            !bmi088_double_valid(state->signal.bias_random_walk_std[i], true) ||
            !isfinite(state->signal.dynamic_bias[i]) ||
            !isfinite(state->signal.filter_state[i])) {
            return -EINVAL;
        }
    }
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_bmi088_signal = {
    .name = "dm-mc02-bmi088-signal",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_BMI088_DOUBLE(noise_std, DmMc02Bmi088Signal),
        VMSTATE_BMI088_DOUBLE_ARRAY(bias, DmMc02Bmi088Signal,
                                     DM_MC02_BMI088_SIGNAL_AXES),
        VMSTATE_BMI088_DOUBLE_ARRAY(temperature_coefficient,
                                     DmMc02Bmi088Signal,
                                     DM_MC02_BMI088_SIGNAL_AXES),
        VMSTATE_BMI088_DOUBLE_ARRAY(bias_random_walk_std,
                                     DmMc02Bmi088Signal,
                                     DM_MC02_BMI088_SIGNAL_AXES),
        VMSTATE_BMI088_DOUBLE_ARRAY(dynamic_bias, DmMc02Bmi088Signal,
                                     DM_MC02_BMI088_SIGNAL_AXES),
        VMSTATE_BMI088_DOUBLE(full_scale, DmMc02Bmi088Signal),
        VMSTATE_BMI088_DOUBLE(temperature_c, DmMc02Bmi088Signal),
        VMSTATE_BMI088_DOUBLE(temperature_reference_c,
                              DmMc02Bmi088Signal),
        VMSTATE_UINT32(rng, DmMc02Bmi088Signal),
        VMSTATE_BMI088_DOUBLE(bandwidth_hz, DmMc02Bmi088Signal),
        VMSTATE_BMI088_DOUBLE_ARRAY(filter_state, DmMc02Bmi088Signal,
                                     DM_MC02_BMI088_SIGNAL_AXES),
        VMSTATE_UINT64(filter_time_ns, DmMc02Bmi088Signal),
        VMSTATE_BOOL(filter_valid, DmMc02Bmi088Signal),
        VMSTATE_UINT64(odr_period_ns, DmMc02Bmi088Signal),
        VMSTATE_UINT64(next_sample_time_ns, DmMc02Bmi088Signal),
        VMSTATE_BOOL(sample_time_valid, DmMc02Bmi088Signal),
        VMSTATE_UINT64(bias_time_ns, DmMc02Bmi088Signal),
        VMSTATE_BOOL(bias_time_valid, DmMc02Bmi088Signal),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription vmstate_dm_mc02_bmi088 = {
    .name = "dm-mc02-bmi088",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_bmi088_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Bmi088, sizeof(((DmMc02Bmi088 *)0)->regs)),
        VMSTATE_STRUCT(signal, DmMc02Bmi088, 0,
                       vmstate_dm_mc02_bmi088_signal,
                       DmMc02Bmi088Signal),
        VMSTATE_UINT8_ARRAY(fifo, DmMc02Bmi088,
                            DM_MC02_BMI_ACCEL_FIFO_CAPACITY),
        VMSTATE_UINT64_ARRAY(fifo_sequence, DmMc02Bmi088,
                             DM_MC02_BMI_ACCEL_FIFO_CAPACITY),
        VMSTATE_UINT16(fifo_head, DmMc02Bmi088),
        VMSTATE_UINT16(fifo_length, DmMc02Bmi088),
        VMSTATE_UINT8(fifo_read_progress, DmMc02Bmi088),
        VMSTATE_UINT8(fifo_read_frame_length, DmMc02Bmi088),
        VMSTATE_UINT8(fifo_read_kind, DmMc02Bmi088),
        VMSTATE_UINT64(fifo_read_sequence, DmMc02Bmi088),
        VMSTATE_UINT8(fifo_downsample_count, DmMc02Bmi088),
        VMSTATE_UINT16(fifo_skipped_frames, DmMc02Bmi088),
        VMSTATE_BOOL(fifo_sensortime_pending, DmMc02Bmi088),
        VMSTATE_BOOL(fifo_overrun, DmMc02Bmi088),
        VMSTATE_UINT64(sample_sequence, DmMc02Bmi088),
        VMSTATE_UINT64(gyro_drdy_clear_time_ns, DmMc02Bmi088),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_bmi088_vmstate(void)
{
    return &vmstate_dm_mc02_bmi088;
}
