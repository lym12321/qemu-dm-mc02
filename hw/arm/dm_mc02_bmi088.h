/*
 * BMI088 chip model used by the DM-MC02 board adapter.
 *
 * The chip owns register, sampling and FIFO state.  SPI framing, chip-select
 * routing and DMA remain outside this object so another board can reuse the
 * device model with a different bus adapter.
 */
#ifndef HW_ARM_DM_MC02_BMI088_H
#define HW_ARM_DM_MC02_BMI088_H

#include "hw/arm/dm_mc02_bmi088_signal.h"
#include "hw/arm/dm_mc02_bmi088_fifo.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct DmMc02Bmi088ReadEvent {
    bool fifo_frame_complete;
    uint64_t fifo_sample_sequence;
} DmMc02Bmi088ReadEvent;

typedef struct DmMc02Bmi088 {
    uint8_t regs[256];
    bool accel;
    DmMc02Bmi088Signal signal;
    uint8_t fifo[DM_MC02_BMI_ACCEL_FIFO_CAPACITY];
    uint64_t fifo_sequence[DM_MC02_BMI_ACCEL_FIFO_CAPACITY];
    uint16_t fifo_head;
    uint16_t fifo_length;
    uint8_t fifo_read_progress;
    uint8_t fifo_read_frame_length;
    uint8_t fifo_read_kind;
    uint64_t fifo_read_sequence;
    uint8_t fifo_downsample_count;
    uint16_t fifo_skipped_frames;
    bool fifo_sensortime_pending;
    bool fifo_overrun;
    uint64_t sample_sequence;
    uint64_t gyro_drdy_clear_time_ns;
} DmMc02Bmi088;

void dm_mc02_bmi088_init(DmMc02Bmi088 *bmi, bool accel);
void dm_mc02_bmi088_reset(DmMc02Bmi088 *bmi);

bool dm_mc02_bmi088_is_accel(const DmMc02Bmi088 *bmi);
bool dm_mc02_bmi088_is_powered(const DmMc02Bmi088 *bmi);

void dm_mc02_bmi088_set_noise(DmMc02Bmi088 *bmi, double standard_deviation);
double dm_mc02_bmi088_get_noise(const DmMc02Bmi088 *bmi);
void dm_mc02_bmi088_set_bias(DmMc02Bmi088 *bmi,
                             const double bias[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_get_bias(const DmMc02Bmi088 *bmi,
                             double bias[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_set_temperature_coefficient(
    DmMc02Bmi088 *bmi,
    const double coefficient[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_get_temperature_coefficient(
    const DmMc02Bmi088 *bmi,
    double coefficient[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_set_bias_random_walk(
    DmMc02Bmi088 *bmi,
    const double standard_deviation[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_get_bias_random_walk(
    const DmMc02Bmi088 *bmi,
    double standard_deviation[DM_MC02_BMI088_SIGNAL_AXES]);
void dm_mc02_bmi088_set_temperature(DmMc02Bmi088 *bmi, double temperature_c);
double dm_mc02_bmi088_get_temperature(const DmMc02Bmi088 *bmi);
void dm_mc02_bmi088_set_rng(DmMc02Bmi088 *bmi, uint32_t seed);

/* Apply a physical sample at sensor_time_ns.  The return value reports
 * whether the configured ODR accepted it.  now_ns is the current QEMU
 * virtual clock used for the gyro DRDY lifetime. */
bool dm_mc02_bmi088_sample(DmMc02Bmi088 *bmi,
                           const float input[DM_MC02_BMI088_SIGNAL_AXES],
                           uint64_t sample_sequence,
                           uint64_t sensor_time_ns,
                           uint64_t now_ns);

uint8_t dm_mc02_bmi088_read_reg(DmMc02Bmi088 *bmi, unsigned reg,
                                uint64_t now_ns,
                                DmMc02Bmi088ReadEvent *event);
void dm_mc02_bmi088_write_reg(DmMc02Bmi088 *bmi, unsigned reg, uint8_t value);
void dm_mc02_bmi088_end_read(DmMc02Bmi088 *bmi);

/* Component-only state contract.  SPI framing and bus wiring are described
 * by dm_mc02_bmi088_spi_vmstate() and remain outside this sensor state. */
struct VMStateDescription;
const struct VMStateDescription *dm_mc02_bmi088_vmstate(void);
extern const struct VMStateDescription vmstate_dm_mc02_bmi088;

#endif
