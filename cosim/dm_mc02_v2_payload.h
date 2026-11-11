#ifndef DM_MC02_V2_PAYLOAD_H
#define DM_MC02_V2_PAYLOAD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_MC02_V2_STATUS_OK 0u
#define DM_MC02_V2_STATUS_QUEUE_FULL 1u
#define DM_MC02_V2_STATUS_PROTOCOL 2u
#define DM_MC02_V2_STATUS_UNSUPPORTED 3u

#define DM_MC02_V2_CAP_STEP (UINT32_C(1) << 0)
#define DM_MC02_V2_CAP_IMU (UINT32_C(1) << 1)
#define DM_MC02_V2_CAP_DIAGNOSTICS (UINT32_C(1) << 2)
#define DM_MC02_V2_CAP_ADC (UINT32_C(1) << 3)
#define DM_MC02_V2_CAP_TELEMETRY (UINT32_C(1) << 4)
#define DM_MC02_V2_CAP_MOTOR (UINT32_C(1) << 5)
#define DM_MC02_V2_CAPABILITY_MASK \
    (DM_MC02_V2_CAP_STEP | DM_MC02_V2_CAP_IMU | \
     DM_MC02_V2_CAP_DIAGNOSTICS | DM_MC02_V2_CAP_ADC | \
     DM_MC02_V2_CAP_TELEMETRY | DM_MC02_V2_CAP_MOTOR)

#define DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE 24u
#define DM_MC02_V2_STEP_ACK_PAYLOAD_SIZE 16u
#define DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE 40u
#define DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE 20u
#define DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE 24u
#define DM_MC02_V2_ADC_INPUT_PAYLOAD_SIZE 8u
#define DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE 12u
#define DM_MC02_V2_ADC_MAX_CHANNEL 31u
#define DM_MC02_V2_ADC_VOLTAGE_FLAG_PIN_OVERRIDE UINT16_C(1)
#define DM_MC02_V2_ADC_VOLTAGE_FLAGS_MASK \
    DM_MC02_V2_ADC_VOLTAGE_FLAG_PIN_OVERRIDE
#define DM_MC02_V2_ADC_VOLTAGE_MAX_UV UINT32_C(3300000)

#define DM_MC02_V2_CONSUMED_ACCEL (UINT32_C(1) << 0)
#define DM_MC02_V2_CONSUMED_GYRO (UINT32_C(1) << 1)
#define DM_MC02_V2_CONSUMED_MASK \
    (DM_MC02_V2_CONSUMED_ACCEL | DM_MC02_V2_CONSUMED_GYRO)

typedef struct DmMc02V2ResetAckPayload {
    uint32_t status;
    uint32_t capabilities;
    uint64_t virtual_time_ns;
    uint32_t queue_depth;
} DmMc02V2ResetAckPayload;

typedef struct DmMc02V2StepAckPayload {
    uint32_t status;
    uint32_t queue_depth;
    uint32_t dropped_count;
} DmMc02V2StepAckPayload;

typedef struct DmMc02V2DiagnosticsPayload {
    uint64_t rx_frames;
    uint64_t rx_bad_frames;
    uint64_t rx_dropped_bytes;
    uint64_t tx_frames;
    uint64_t tx_dropped;
} DmMc02V2DiagnosticsPayload;

typedef struct DmMc02V2StepDonePayload {
    uint32_t status;
    uint32_t consumed_mask;
    uint32_t missing_mask;
    uint32_t queue_depth;
    uint32_t dropped_count;
} DmMc02V2StepDonePayload;

typedef struct DmMc02V2BoardTelemetryPayload {
    uint32_t led_rgb;
    uint32_t led_brightness;
    uint32_t buzzer;
    uint32_t board_flags;
    uint32_t buzzer_frequency_hz;
    uint32_t buzzer_duty_permille;
} DmMc02V2BoardTelemetryPayload;

typedef struct DmMc02V2AdcInputPayload {
    uint16_t channel;
    uint16_t raw;
} DmMc02V2AdcInputPayload;

typedef struct DmMc02V2AdcVoltagePayload {
    uint16_t channel;
    uint16_t flags;
    uint32_t voltage_uv;
} DmMc02V2AdcVoltagePayload;

bool dm_mc02_v2_payload_validate_reset_ack(const uint8_t *payload,
                                           size_t payload_len);
bool dm_mc02_v2_payload_validate_step_ack(const uint8_t *payload,
                                          size_t payload_len);
bool dm_mc02_v2_payload_validate_diagnostics(const uint8_t *payload,
                                             size_t payload_len);
bool dm_mc02_v2_payload_validate_step_done(const uint8_t *payload,
                                           size_t payload_len);
bool dm_mc02_v2_payload_validate_board_telemetry(const uint8_t *payload,
                                                 size_t payload_len);
bool dm_mc02_v2_payload_validate_adc_input(const uint8_t *payload,
                                           size_t payload_len);
bool dm_mc02_v2_payload_validate_adc_voltage(const uint8_t *payload,
                                             size_t payload_len);

size_t dm_mc02_v2_payload_encode_reset_ack(
    uint8_t *out, size_t capacity, const DmMc02V2ResetAckPayload *value);
size_t dm_mc02_v2_payload_encode_step_ack(
    uint8_t *out, size_t capacity, const DmMc02V2StepAckPayload *value);
size_t dm_mc02_v2_payload_encode_diagnostics(
    uint8_t *out, size_t capacity, const DmMc02V2DiagnosticsPayload *value);
size_t dm_mc02_v2_payload_encode_step_done(
    uint8_t *out, size_t capacity, const DmMc02V2StepDonePayload *value);
size_t dm_mc02_v2_payload_encode_board_telemetry(
    uint8_t *out, size_t capacity,
    const DmMc02V2BoardTelemetryPayload *value);
size_t dm_mc02_v2_payload_encode_adc_input(
    uint8_t *out, size_t capacity, const DmMc02V2AdcInputPayload *value);
size_t dm_mc02_v2_payload_encode_adc_voltage(
    uint8_t *out, size_t capacity, const DmMc02V2AdcVoltagePayload *value);

bool dm_mc02_v2_payload_decode_reset_ack(
    DmMc02V2ResetAckPayload *value, const uint8_t *payload,
    size_t payload_len);
bool dm_mc02_v2_payload_decode_step_ack(
    DmMc02V2StepAckPayload *value, const uint8_t *payload,
    size_t payload_len);
bool dm_mc02_v2_payload_decode_diagnostics(
    DmMc02V2DiagnosticsPayload *value, const uint8_t *payload,
    size_t payload_len);
bool dm_mc02_v2_payload_decode_step_done(
    DmMc02V2StepDonePayload *value, const uint8_t *payload,
    size_t payload_len);
bool dm_mc02_v2_payload_decode_board_telemetry(
    DmMc02V2BoardTelemetryPayload *value, const uint8_t *payload,
    size_t payload_len);
bool dm_mc02_v2_payload_decode_adc_input(
    DmMc02V2AdcInputPayload *value, const uint8_t *payload,
    size_t payload_len);
bool dm_mc02_v2_payload_decode_adc_voltage(
    DmMc02V2AdcVoltagePayload *value, const uint8_t *payload,
    size_t payload_len);

#endif
