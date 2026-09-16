#ifndef DM_MC02_PROTOCOL_H
#define DM_MC02_PROTOCOL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "dm_mc02_wire.h"

#define DM_MC02_PROTOCOL_MAGIC DM_MC02_WIRE_MAGIC
#define DM_MC02_PROTOCOL_VERSION DM_MC02_WIRE_VERSION
#define DM_MC02_PROTOCOL_HEADER_SIZE DM_MC02_WIRE_HEADER_SIZE
#define DM_MC02_PROTOCOL_MAX_PAYLOAD DM_MC02_WIRE_MAX_PAYLOAD
#define DM_MC02_IMU_WIRE_PAYLOAD_SIZE 24u
#define DM_MC02_TELEMETRY_WIRE_PAYLOAD_SIZE 12u
#define DM_MC02_ADC_INPUT_WIRE_PAYLOAD_SIZE 8u
#define DM_MC02_ADC_INPUT_MAX_CHANNEL DM_MC02_WIRE_ADC_MAX_CHANNEL
#define DM_MC02_ADC_VOLTAGE_WIRE_PAYLOAD_SIZE 12u
#define DM_MC02_ADC_VOLTAGE_MAX_UV DM_MC02_WIRE_ADC_VOLTAGE_MAX_UV
#define DM_MC02_ADC_VOLTAGE_FLAG_PIN_OVERRIDE \
    DM_MC02_WIRE_ADC_VOLTAGE_FLAG_PIN_OVERRIDE
#define DM_MC02_ADC_VOLTAGE_FLAGS_MASK DM_MC02_WIRE_ADC_VOLTAGE_FLAGS_MASK

enum DmMc02FrameType {
    DM_MC02_FRAME_RESET = DM_MC02_WIRE_FRAME_RESET,
    DM_MC02_FRAME_IMU_SAMPLE = DM_MC02_WIRE_FRAME_IMU_SAMPLE,
    DM_MC02_FRAME_TELEMETRY = DM_MC02_WIRE_FRAME_TELEMETRY,
    DM_MC02_FRAME_ACK = DM_MC02_WIRE_FRAME_ACK,
    DM_MC02_FRAME_ADC_INPUT = DM_MC02_WIRE_FRAME_ADC_INPUT,
    DM_MC02_FRAME_ADC_VOLTAGE = DM_MC02_WIRE_FRAME_ADC_VOLTAGE,
};

typedef DmMc02WireFrameHeader DmMc02FrameHeader;

typedef struct DmMc02ImuSample {
    float gyro[3];
    float accel[3];
} DmMc02ImuSample;

typedef struct DmMc02Telemetry {
    uint32_t led_rgb;
    uint32_t led_brightness;
    uint8_t buzzer;
    uint8_t board_flags;
    uint16_t reserved;
} DmMc02Telemetry;

typedef struct DmMc02AdcInput {
    uint16_t channel;
    uint16_t raw;
    uint32_t reserved;
} DmMc02AdcInput;

typedef struct DmMc02AdcVoltage {
    uint16_t channel;
    uint16_t flags;
    uint32_t voltage_uv;
    uint32_t reserved;
} DmMc02AdcVoltage;

typedef DmMc02WireFrame DmMc02Frame;

typedef struct DmMc02StreamValidator {
    bool initialized;
    uint64_t last_sequence;
    uint64_t last_virtual_time_ns;
} DmMc02StreamValidator;

void dm_mc02_stream_validator_reset(DmMc02StreamValidator *validator);
size_t dm_mc02_frame_encode(uint8_t *out, size_t capacity,
                            const DmMc02Frame *frame);
bool dm_mc02_frame_decode(DmMc02Frame *frame, const uint8_t *wire,
                          size_t wire_len);
bool dm_mc02_validate_frame(DmMc02StreamValidator *validator,
                            const DmMc02Frame *frame);
bool dm_mc02_set_imu_payload(DmMc02Frame *frame, const DmMc02ImuSample *sample);
bool dm_mc02_get_imu_payload(const DmMc02Frame *frame, DmMc02ImuSample *sample);
bool dm_mc02_set_telemetry_payload(DmMc02Frame *frame,
                                   const DmMc02Telemetry *telemetry);
bool dm_mc02_get_telemetry_payload(const DmMc02Frame *frame,
                                   DmMc02Telemetry *telemetry);
bool dm_mc02_set_adc_input_payload(DmMc02Frame *frame,
                                   const DmMc02AdcInput *input);
bool dm_mc02_get_adc_input_payload(const DmMc02Frame *frame,
                                   DmMc02AdcInput *input);
bool dm_mc02_set_adc_voltage_payload(DmMc02Frame *frame,
                                     const DmMc02AdcVoltage *voltage);
bool dm_mc02_get_adc_voltage_payload(const DmMc02Frame *frame,
                                     DmMc02AdcVoltage *voltage);

#endif
