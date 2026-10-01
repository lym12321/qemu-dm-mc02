#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088.h"
#include "hw/arm/dm_mc02_bmi088_timing.h"

#include <math.h>
#include <string.h>

static double dm_mc02_bmi088_accel_bandwidth(uint8_t odr, uint8_t bwp)
{
    static const double normal[] = {
        0.0, 0.0, 0.0, 0.0, 0.0, 5.0, 10.0, 20.0, 40.0,
        80.0, 145.0, 230.0, 280.0,
    };
    static const double osr2[] = {
        0.0, 0.0, 0.0, 0.0, 0.0, 2.0, 5.0, 9.0, 19.0,
        38.0, 75.0, 140.0, 234.0,
    };
    static const double osr4[] = {
        0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 3.0, 5.0, 10.0,
        20.0, 40.0, 80.0, 145.0,
    };

    if (odr >= ARRAY_SIZE(normal)) {
        return 0.0;
    }
    bwp &= 0x07;
    if (bwp == 0) {
        return osr4[odr];
    }
    if (bwp == 1) {
        return osr2[odr];
    }
    return normal[odr];
}

static double dm_mc02_bmi088_gyro_bandwidth(uint8_t code)
{
    static const double bandwidth[] = {
        532.0, 230.0, 116.0, 47.0, 23.0, 12.0, 64.0, 32.0,
    };

    return code < ARRAY_SIZE(bandwidth) ? bandwidth[code] : 116.0;
}

static double dm_mc02_bmi088_accel_range(uint8_t code)
{
    static const double ranges[] = { 3.0, 6.0, 12.0, 24.0 };

    return code < ARRAY_SIZE(ranges) ? ranges[code] : 3.0;
}

static double dm_mc02_bmi088_gyro_range(uint8_t code)
{
    static const double ranges[] = { 2000.0, 1000.0, 500.0, 250.0, 125.0 };

    return code < ARRAY_SIZE(ranges) ? ranges[code] : 2000.0;
}

static uint16_t dm_mc02_bmi088_fifo_capacity(const DmMc02Bmi088 *bmi)
{
    return bmi->accel ? DM_MC02_BMI_ACCEL_FIFO_CAPACITY :
                        DM_MC02_BMI_GYRO_FIFO_CAPACITY;
}

static uint8_t dm_mc02_bmi088_fifo_frame_length(const DmMc02Bmi088 *bmi)
{
    uint8_t header;

    if (!bmi->accel) {
        return 8;
    }
    if (!bmi->fifo_length) {
        return 0;
    }
    header = bmi->fifo[bmi->fifo_head];
    if (header == 0x84) {
        return 7;
    }
    if (header == 0x40 || header == 0x48 || header == 0x50) {
        return 2;
    }
    return 1;
}

static bool dm_mc02_bmi088_fifo_enabled(const DmMc02Bmi088 *bmi)
{
    return bmi->accel ? (bmi->regs[0x49] & (1u << 6)) != 0 :
                        (bmi->regs[0x3e] == 0x40 ||
                         bmi->regs[0x3e] == 0x80);
}

static bool dm_mc02_bmi088_fifo_stop_at_full(const DmMc02Bmi088 *bmi)
{
    return bmi->accel ? (bmi->regs[0x48] & 1u) != 0 :
                        bmi->regs[0x3e] == 0x40;
}

static bool dm_mc02_bmi088_powered(const DmMc02Bmi088 *bmi)
{
    if (bmi->accel) {
        return (bmi->regs[0x7c] & 0x03) == 0 &&
               (bmi->regs[0x7d] & 0x04) != 0;
    }
    return (bmi->regs[0x11] & 0xa0) == 0;
}

static void dm_mc02_bmi088_fifo_drop_frame(DmMc02Bmi088 *bmi)
{
    uint8_t frame_length = dm_mc02_bmi088_fifo_frame_length(bmi);
    uint16_t capacity = dm_mc02_bmi088_fifo_capacity(bmi);

    if (!frame_length || frame_length > bmi->fifo_length) {
        bmi->fifo_length = 0;
        bmi->fifo_head = 0;
        return;
    }
    bmi->fifo_head = (bmi->fifo_head + frame_length) % capacity;
    bmi->fifo_length -= frame_length;
}

static void dm_mc02_bmi088_fifo_clear(DmMc02Bmi088 *bmi)
{
    bmi->fifo_head = 0;
    bmi->fifo_length = 0;
    bmi->fifo_read_progress = 0;
    bmi->fifo_read_frame_length = 0;
    bmi->fifo_read_kind = 0;
    bmi->fifo_read_sequence = 0;
    bmi->fifo_downsample_count = 0;
    bmi->fifo_skipped_frames = 0;
    bmi->fifo_sensortime_pending = false;
    bmi->fifo_overrun = false;
}

static void dm_mc02_bmi088_fifo_push(DmMc02Bmi088 *bmi, const uint8_t *data,
                                     uint8_t length, uint64_t sequence)
{
    uint16_t capacity = dm_mc02_bmi088_fifo_capacity(bmi);
    uint16_t limit = dm_mc02_bmi088_fifo_limit(
        bmi->accel, dm_mc02_bmi088_fifo_stop_at_full(bmi));
    uint16_t tail;

    if (!dm_mc02_bmi088_fifo_enabled(bmi) || !length || length > capacity) {
        return;
    }
    while (dm_mc02_bmi088_fifo_needs_room(bmi->fifo_length, limit,
                                           length)) {
        bmi->fifo_overrun = true;
        if (dm_mc02_bmi088_fifo_stop_at_full(bmi)) {
            if (bmi->accel && bmi->fifo_skipped_frames != UINT16_MAX) {
                bmi->fifo_skipped_frames++;
            }
            return;
        }
        dm_mc02_bmi088_fifo_drop_frame(bmi);
        if (bmi->accel && bmi->fifo_skipped_frames != UINT16_MAX) {
            bmi->fifo_skipped_frames++;
        }
    }
    tail = (bmi->fifo_head + bmi->fifo_length) % capacity;
    for (unsigned i = 0; i < length; ++i) {
        bmi->fifo[(tail + i) % capacity] = data[i];
        bmi->fifo_sequence[(tail + i) % capacity] = sequence;
    }
    bmi->fifo_length += length;
}

static bool dm_mc02_bmi088_fifo_should_store(DmMc02Bmi088 *bmi)
{
    uint8_t factor;

    if (!dm_mc02_bmi088_fifo_enabled(bmi)) {
        return false;
    }
    if (!bmi->accel) {
        return true;
    }
    factor = 1u << ((bmi->regs[0x45] >> 4) & 7u);
    if (!bmi->fifo_downsample_count) {
        bmi->fifo_downsample_count = factor - 1;
        return true;
    }
    bmi->fifo_downsample_count--;
    return false;
}

static uint8_t dm_mc02_bmi088_fifo_read(DmMc02Bmi088 *bmi,
                                         DmMc02Bmi088ReadEvent *event)
{
    uint16_t capacity = dm_mc02_bmi088_fifo_capacity(bmi);
    uint8_t result;

    if (!bmi->fifo_read_kind) {
        if (bmi->accel && bmi->fifo_skipped_frames) {
            bmi->fifo_read_kind = 2;
            bmi->fifo_read_frame_length = 2;
        } else if (bmi->fifo_length) {
            bmi->fifo_read_kind = 1;
            bmi->fifo_read_frame_length =
                dm_mc02_bmi088_fifo_frame_length(bmi);
            bmi->fifo_read_sequence = bmi->fifo_sequence[bmi->fifo_head];
        } else if (bmi->accel && bmi->fifo_sensortime_pending) {
            bmi->fifo_read_kind = 3;
            bmi->fifo_read_frame_length = 4;
            bmi->fifo_read_sequence = 0;
        }
    }
    if (!bmi->fifo_read_kind) {
        return 0x80;
    }
    if (bmi->fifo_read_kind == 1) {
        result = bmi->fifo[(bmi->fifo_head + bmi->fifo_read_progress) %
                           capacity];
    } else if (bmi->fifo_read_kind == 2) {
        result = bmi->fifo_read_progress ?
                 MIN(bmi->fifo_skipped_frames, 0xff) : 0x40;
    } else {
        result = bmi->fifo_read_progress == 0 ? 0x44 :
                 bmi->regs[0x18 + bmi->fifo_read_progress - 1];
    }
    bmi->fifo_read_progress++;
    if (bmi->fifo_read_progress == bmi->fifo_read_frame_length) {
        if (bmi->fifo_read_kind == 1) {
            uint64_t sequence = bmi->fifo_read_sequence;

            dm_mc02_bmi088_fifo_drop_frame(bmi);
            if (bmi->accel && !bmi->fifo_length) {
                bmi->fifo_sensortime_pending = true;
            }
            /* Accel config frames share the byte FIFO with data frames, but
             * they are not a guest consumption of a physical sample. */
            if (!bmi->accel || bmi->fifo_read_frame_length == 7) {
                event->fifo_frame_complete = true;
                event->fifo_sample_sequence = sequence;
            }
            /* The SPI adapter receives this completion through the read
             * event.  The chip model does not know the co-sim step token. */
        } else if (bmi->fifo_read_kind == 2) {
            bmi->fifo_skipped_frames = 0;
        } else {
            bmi->fifo_sensortime_pending = false;
        }
        bmi->fifo_read_progress = 0;
        bmi->fifo_read_frame_length = 0;
        bmi->fifo_read_kind = 0;
        bmi->fifo_read_sequence = 0;
    }
    return result;
}

static void dm_mc02_bmi088_set_temperature_reg(DmMc02Bmi088 *bmi,
                                                double temperature_c)
{
    double scaled;
    int raw;
    unsigned encoded;

    if (!isfinite(temperature_c)) {
        return;
    }
    dm_mc02_bmi088_signal_set_temperature(&bmi->signal, temperature_c);
    if (!bmi->accel) {
        return;
    }
    scaled = (temperature_c - 23.0) * 8.0;
    scaled = MIN(MAX(scaled, -1024.0), 1023.0);
    raw = (int)(scaled + (scaled >= 0.0 ? 0.5 : -0.5));
    encoded = (unsigned)raw & 0x7ffu;
    bmi->regs[0x22] = encoded >> 3;
    bmi->regs[0x23] = (encoded & 7u) << 5;
}

static void dm_mc02_bmi088_set_sensor_time(DmMc02Bmi088 *bmi,
                                            uint64_t sensor_time_ns)
{
    uint64_t whole_seconds;
    uint64_t remainder_ns;
    uint32_t sensor_ticks;

    if (!bmi->accel) {
        return;
    }
    whole_seconds = sensor_time_ns / UINT64_C(1000000000);
    remainder_ns = sensor_time_ns % UINT64_C(1000000000);
    sensor_ticks = (uint32_t)((whole_seconds * UINT64_C(25600) +
                               (remainder_ns * UINT64_C(25600)) /
                               UINT64_C(1000000000)) & 0xffffffu);
    bmi->regs[0x18] = sensor_ticks;
    bmi->regs[0x19] = sensor_ticks >> 8;
    bmi->regs[0x1a] = sensor_ticks >> 16;
}

void dm_mc02_bmi088_init(DmMc02Bmi088 *bmi, bool accel)
{
    memset(bmi, 0, sizeof(*bmi));
    bmi->accel = accel;
    dm_mc02_bmi088_signal_init(
        &bmi->signal,
        accel ? DM_MC02_BMI088_SIGNAL_ACCEL : DM_MC02_BMI088_SIGNAL_GYRO);
    dm_mc02_bmi088_signal_set_temperature(&bmi->signal, 25.0);
    dm_mc02_bmi088_reset(bmi);
}

void dm_mc02_bmi088_reset(DmMc02Bmi088 *bmi)
{
    bool accel = bmi->accel;

    memset(bmi->regs, 0, sizeof(bmi->regs));
    dm_mc02_bmi088_signal_set_kind(
        &bmi->signal,
        accel ? DM_MC02_BMI088_SIGNAL_ACCEL : DM_MC02_BMI088_SIGNAL_GYRO);
    dm_mc02_bmi088_signal_reset(&bmi->signal);
    bmi->accel = accel;
    dm_mc02_bmi088_fifo_clear(bmi);
    bmi->sample_sequence = 0;
    bmi->gyro_drdy_clear_time_ns = 0;
    bmi->regs[0x00] = accel ? 0x1e : 0x0f;
    if (accel) {
        bmi->regs[0x17] = 0x40;
        dm_mc02_bmi088_set_temperature_reg(bmi,
                                            bmi->signal.temperature_c);
    }
}

bool dm_mc02_bmi088_is_accel(const DmMc02Bmi088 *bmi)
{
    return bmi->accel;
}

bool dm_mc02_bmi088_is_powered(const DmMc02Bmi088 *bmi)
{
    return dm_mc02_bmi088_powered(bmi);
}

void dm_mc02_bmi088_set_noise(DmMc02Bmi088 *bmi, double standard_deviation)
{
    dm_mc02_bmi088_signal_set_noise(&bmi->signal, standard_deviation);
}

double dm_mc02_bmi088_get_noise(const DmMc02Bmi088 *bmi)
{
    return bmi->signal.noise_std;
}

void dm_mc02_bmi088_set_bias(DmMc02Bmi088 *bmi,
                             const double bias[DM_MC02_BMI088_SIGNAL_AXES])
{
    dm_mc02_bmi088_signal_set_bias(&bmi->signal, bias);
}

void dm_mc02_bmi088_get_bias(const DmMc02Bmi088 *bmi,
                             double bias[DM_MC02_BMI088_SIGNAL_AXES])
{
    memcpy(bias, bmi->signal.bias, sizeof(bmi->signal.bias));
}

void dm_mc02_bmi088_set_temperature_coefficient(
    DmMc02Bmi088 *bmi,
    const double coefficient[DM_MC02_BMI088_SIGNAL_AXES])
{
    dm_mc02_bmi088_signal_set_temperature_coefficient(&bmi->signal,
                                                      coefficient);
}

void dm_mc02_bmi088_get_temperature_coefficient(
    const DmMc02Bmi088 *bmi,
    double coefficient[DM_MC02_BMI088_SIGNAL_AXES])
{
    dm_mc02_bmi088_signal_get_temperature_coefficient(&bmi->signal,
                                                      coefficient);
}

void dm_mc02_bmi088_set_bias_random_walk(
    DmMc02Bmi088 *bmi,
    const double standard_deviation[DM_MC02_BMI088_SIGNAL_AXES])
{
    dm_mc02_bmi088_signal_set_bias_random_walk(&bmi->signal,
                                               standard_deviation);
}

void dm_mc02_bmi088_get_bias_random_walk(
    const DmMc02Bmi088 *bmi,
    double standard_deviation[DM_MC02_BMI088_SIGNAL_AXES])
{
    dm_mc02_bmi088_signal_get_bias_random_walk(&bmi->signal,
                                               standard_deviation);
}

void dm_mc02_bmi088_set_temperature(DmMc02Bmi088 *bmi, double temperature_c)
{
    dm_mc02_bmi088_set_temperature_reg(bmi, temperature_c);
}

double dm_mc02_bmi088_get_temperature(const DmMc02Bmi088 *bmi)
{
    return bmi->signal.temperature_c;
}

void dm_mc02_bmi088_set_rng(DmMc02Bmi088 *bmi, uint32_t seed)
{
    dm_mc02_bmi088_signal_set_rng(&bmi->signal, seed);
}

bool dm_mc02_bmi088_sample(DmMc02Bmi088 *bmi,
                           const float input[DM_MC02_BMI088_SIGNAL_AXES],
                           uint64_t sample_sequence,
                           uint64_t sensor_time_ns,
                           uint64_t now_ns)
{
    float filtered[DM_MC02_BMI088_SIGNAL_AXES];

    if (!dm_mc02_bmi088_powered(bmi)) {
        return false;
    }
    if (!dm_mc02_bmi088_signal_sample(&bmi->signal, input, sensor_time_ns,
                                      filtered)) {
        return false;
    }
    for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
        uint16_t raw = (uint16_t)dm_mc02_bmi088_signal_raw(
            &bmi->signal, filtered[i]);
        unsigned reg = bmi->accel ? 0x12 + i * 2 : 0x02 + i * 2;

        bmi->regs[reg] = raw;
        bmi->regs[reg + 1] = raw >> 8;
    }
    bmi->sample_sequence = sample_sequence;
    dm_mc02_bmi088_set_sensor_time(bmi, sensor_time_ns);
    if (bmi->accel) {
        uint8_t fifo_frame[7] = { 0x84 };

        for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
            fifo_frame[1 + i * 2] = bmi->regs[0x12 + i * 2];
            fifo_frame[1 + i * 2 + 1] = bmi->regs[0x13 + i * 2];
        }
        if (dm_mc02_bmi088_fifo_should_store(bmi)) {
            dm_mc02_bmi088_fifo_push(bmi, fifo_frame,
                                     sizeof(fifo_frame), sample_sequence);
        }
        bmi->regs[0x03] |= 1u << 7;
    } else {
        uint8_t fifo_frame[8] = { 0 };

        for (unsigned i = 0; i < DM_MC02_BMI088_SIGNAL_AXES; ++i) {
            fifo_frame[i * 2] = bmi->regs[0x02 + i * 2];
            fifo_frame[i * 2 + 1] = bmi->regs[0x03 + i * 2];
        }
        if (dm_mc02_bmi088_fifo_should_store(bmi)) {
            dm_mc02_bmi088_fifo_push(bmi, fifo_frame,
                                     sizeof(fifo_frame), sample_sequence);
        }
        bmi->regs[0x0a] |= 1u << 7;
        bmi->gyro_drdy_clear_time_ns = now_ns + 300 * 1000;
    }
    return true;
}

static uint8_t dm_mc02_bmi088_read_reg_internal(DmMc02Bmi088 *bmi,
                                                unsigned reg,
                                                uint64_t now_ns,
                                                DmMc02Bmi088ReadEvent *event)
{
    if (bmi->accel) {
        if (reg == 0x24) {
            return bmi->fifo_length;
        }
        if (reg == 0x25) {
            return (bmi->fifo_length >> 8) & 0x3f;
        }
        if (reg == 0x26) {
            return dm_mc02_bmi088_fifo_read(bmi, event);
        }
    } else {
        if (reg == 0x0e) {
            return (bmi->fifo_overrun ? 0x80 : 0) |
                   MIN(bmi->fifo_length / 8, 0x7f);
        }
        if (reg == 0x0a) {
            uint8_t value = bmi->regs[reg];
            uint8_t watermark = bmi->regs[0x3d] & 0x7f;
            uint8_t frames = bmi->fifo_length / 8;

            if (bmi->gyro_drdy_clear_time_ns &&
                now_ns >= bmi->gyro_drdy_clear_time_ns) {
                bmi->regs[reg] &= ~(1u << 7);
                value &= ~(1u << 7);
            }
            if (bmi->fifo_overrun ||
                (watermark && frames > watermark)) {
                value |= 1u << 4;
            }
            return value;
        }
        if (reg == 0x3f) {
            return dm_mc02_bmi088_fifo_read(bmi, event);
        }
    }
    if (bmi->accel && reg >= 0x12 && reg <= 0x17) {
        bmi->regs[0x03] &= ~(1u << 7);
    }
    return bmi->regs[reg];
}

uint8_t dm_mc02_bmi088_read_reg(DmMc02Bmi088 *bmi, unsigned reg,
                                uint64_t now_ns,
                                DmMc02Bmi088ReadEvent *event)
{
    memset(event, 0, sizeof(*event));
    if (reg >= ARRAY_SIZE(bmi->regs)) {
        return 0;
    }
    return dm_mc02_bmi088_read_reg_internal(bmi, reg, now_ns, event);
}

void dm_mc02_bmi088_write_reg(DmMc02Bmi088 *bmi, unsigned reg, uint8_t value)
{
    if (reg >= ARRAY_SIZE(bmi->regs)) {
        return;
    }
    if (bmi->accel && dm_mc02_bmi088_fifo_enabled(bmi)) {
        uint8_t config_frame[2] = { 0x48, 0 };

        if (reg == 0x40 || reg == 0x45) {
            config_frame[1] = 1;
            dm_mc02_bmi088_fifo_push(bmi, config_frame,
                                     sizeof(config_frame), 0);
        } else if (reg == 0x41) {
            config_frame[1] = 2;
            dm_mc02_bmi088_fifo_push(bmi, config_frame,
                                     sizeof(config_frame), 0);
        }
    }
    bmi->regs[reg] = value;
    if ((bmi->accel && (reg == 0x7c || reg == 0x7d)) ||
        (!bmi->accel && reg == 0x11)) {
        if (!dm_mc02_bmi088_powered(bmi)) {
            if (bmi->accel) {
                bmi->regs[0x03] &= ~(1u << 7);
            } else {
                bmi->regs[0x0a] &= ~(1u << 7);
            }
        }
    }
    if (bmi->accel && reg == 0x41) {
        dm_mc02_bmi088_signal_set_full_scale(
            &bmi->signal, dm_mc02_bmi088_accel_range(value & 0x03));
    } else if (bmi->accel && reg == 0x40) {
        dm_mc02_bmi088_signal_set_odr_period(
            &bmi->signal, dm_mc02_bmi088_accel_period_ns(value & 0x0f));
        dm_mc02_bmi088_signal_set_bandwidth(
            &bmi->signal,
            dm_mc02_bmi088_accel_bandwidth(value & 0x0f,
                                           (value >> 4) & 0x0f));
    } else if (bmi->accel && reg == 0x45) {
        bmi->regs[reg] = (value & 0x70) | 0x80;
        bmi->fifo_downsample_count = 0;
    } else if (!bmi->accel && reg == 0x0f) {
        dm_mc02_bmi088_signal_set_full_scale(
            &bmi->signal, dm_mc02_bmi088_gyro_range(value & 0x07));
    } else if (!bmi->accel && reg == 0x10) {
        dm_mc02_bmi088_signal_set_odr_period(
            &bmi->signal, dm_mc02_bmi088_gyro_period_ns(value & 0x0f));
        dm_mc02_bmi088_signal_set_bandwidth(
            &bmi->signal, dm_mc02_bmi088_gyro_bandwidth(value & 0x07));
    }
    if ((bmi->accel && reg == 0x7e && value == 0xb0) ||
        (!bmi->accel && (reg == 0x3d || reg == 0x3e))) {
        dm_mc02_bmi088_fifo_clear(bmi);
    }
    if (((bmi->accel && reg == 0x7e) || (!bmi->accel && reg == 0x14)) &&
        value == 0xb6) {
        dm_mc02_bmi088_reset(bmi);
    }
}

void dm_mc02_bmi088_end_read(DmMc02Bmi088 *bmi)
{
    if (!bmi->fifo_read_progress && !bmi->fifo_sensortime_pending) {
        return;
    }
    if (bmi->fifo_sensortime_pending) {
        bmi->fifo_sensortime_pending = false;
    }
    if (!bmi->accel) {
        dm_mc02_bmi088_fifo_drop_frame(bmi);
    }
    bmi->fifo_read_progress = 0;
    bmi->fifo_read_frame_length = 0;
    bmi->fifo_read_kind = 0;
    bmi->fifo_read_sequence = 0;
}
