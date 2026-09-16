#include "dm_mc02_protocol.h"

#include <string.h>

static uint32_t float_bits(float value)
{
    uint32_t bits;
    memcpy(&bits, &value, sizeof(bits));
    return bits;
}

static float bits_float(uint32_t bits)
{
    float value;
    memcpy(&value, &bits, sizeof(value));
    return value;
}

void dm_mc02_stream_validator_reset(DmMc02StreamValidator *validator)
{
    if (validator) {
        validator->initialized = false;
        validator->last_sequence = 0;
        validator->last_virtual_time_ns = 0;
    }
}

size_t dm_mc02_frame_encode(uint8_t *out, size_t capacity,
                            const DmMc02Frame *frame)
{
    return dm_mc02_wire_encode(out, capacity, frame);
}

bool dm_mc02_frame_decode(DmMc02Frame *frame, const uint8_t *wire,
                          size_t wire_len)
{
    return dm_mc02_wire_decode(frame, wire, wire_len);
}

bool dm_mc02_validate_frame(DmMc02StreamValidator *validator,
                            const DmMc02Frame *frame)
{
    if (!validator || !frame || frame->header.version != DM_MC02_PROTOCOL_VERSION ||
        !dm_mc02_wire_known_type(frame->header.type) ||
        !dm_mc02_wire_payload_valid(frame->header.type, frame->payload,
                                    frame->header.payload_len)) {
        return false;
    }

    if (frame->header.type == DM_MC02_FRAME_RESET) {
        dm_mc02_stream_validator_reset(validator);
        validator->initialized = true;
        validator->last_sequence = frame->header.sequence;
        validator->last_virtual_time_ns = frame->header.virtual_time_ns;
        return true;
    }

    if (validator->initialized &&
        (frame->header.sequence <= validator->last_sequence ||
         frame->header.virtual_time_ns <= validator->last_virtual_time_ns)) {
        return false;
    }
    validator->initialized = true;
    validator->last_sequence = frame->header.sequence;
    validator->last_virtual_time_ns = frame->header.virtual_time_ns;
    return true;
}

bool dm_mc02_set_imu_payload(DmMc02Frame *frame, const DmMc02ImuSample *sample)
{
    if (!frame || !sample) {
        return false;
    }
    for (unsigned i = 0; i < 3; ++i) {
        dm_mc02_wire_put32(frame->payload + i * 4,
                           float_bits(sample->gyro[i]));
        dm_mc02_wire_put32(frame->payload + 12 + i * 4,
                           float_bits(sample->accel[i]));
    }
    frame->header.type = DM_MC02_FRAME_IMU_SAMPLE;
    frame->header.payload_len = DM_MC02_IMU_WIRE_PAYLOAD_SIZE;
    return true;
}

bool dm_mc02_get_imu_payload(const DmMc02Frame *frame, DmMc02ImuSample *sample)
{
    if (!frame || !sample || frame->header.type != DM_MC02_FRAME_IMU_SAMPLE ||
        frame->header.payload_len != DM_MC02_IMU_WIRE_PAYLOAD_SIZE) {
        return false;
    }
    for (unsigned i = 0; i < 3; ++i) {
        sample->gyro[i] = bits_float(dm_mc02_wire_get32(frame->payload + i * 4));
        sample->accel[i] = bits_float(
            dm_mc02_wire_get32(frame->payload + 12 + i * 4));
    }
    return true;
}

bool dm_mc02_set_telemetry_payload(DmMc02Frame *frame,
                                   const DmMc02Telemetry *telemetry)
{
    if (!frame || !telemetry) {
        return false;
    }
    dm_mc02_wire_put32(frame->payload, telemetry->led_rgb);
    dm_mc02_wire_put32(frame->payload + 4, telemetry->led_brightness);
    frame->payload[8] = telemetry->buzzer;
    frame->payload[9] = telemetry->board_flags;
    dm_mc02_wire_put16(frame->payload + 10, telemetry->reserved);
    frame->header.type = DM_MC02_FRAME_TELEMETRY;
    frame->header.payload_len = DM_MC02_TELEMETRY_WIRE_PAYLOAD_SIZE;
    return true;
}

bool dm_mc02_get_telemetry_payload(const DmMc02Frame *frame,
                                   DmMc02Telemetry *telemetry)
{
    if (!frame || !telemetry || frame->header.type != DM_MC02_FRAME_TELEMETRY ||
        frame->header.payload_len != DM_MC02_TELEMETRY_WIRE_PAYLOAD_SIZE) {
        return false;
    }
    telemetry->led_rgb = dm_mc02_wire_get32(frame->payload);
    telemetry->led_brightness = dm_mc02_wire_get32(frame->payload + 4);
    telemetry->buzzer = frame->payload[8];
    telemetry->board_flags = frame->payload[9];
    telemetry->reserved = dm_mc02_wire_get16(frame->payload + 10);
    return true;
}

bool dm_mc02_set_adc_input_payload(DmMc02Frame *frame,
                                   const DmMc02AdcInput *input)
{
    if (!frame || !input || input->channel > DM_MC02_ADC_INPUT_MAX_CHANNEL ||
        input->reserved != 0) {
        return false;
    }
    dm_mc02_wire_put16(frame->payload, input->channel);
    dm_mc02_wire_put16(frame->payload + 2, input->raw);
    dm_mc02_wire_put32(frame->payload + 4, input->reserved);
    frame->header.type = DM_MC02_FRAME_ADC_INPUT;
    frame->header.payload_len = DM_MC02_ADC_INPUT_WIRE_PAYLOAD_SIZE;
    return true;
}

bool dm_mc02_get_adc_input_payload(const DmMc02Frame *frame,
                                   DmMc02AdcInput *input)
{
    if (!frame || !input || frame->header.type != DM_MC02_FRAME_ADC_INPUT ||
        frame->header.payload_len != DM_MC02_ADC_INPUT_WIRE_PAYLOAD_SIZE) {
        return false;
    }
    input->channel = dm_mc02_wire_get16(frame->payload);
    input->raw = dm_mc02_wire_get16(frame->payload + 2);
    input->reserved = dm_mc02_wire_get32(frame->payload + 4);
    return input->channel <= DM_MC02_ADC_INPUT_MAX_CHANNEL &&
           input->reserved == 0;
}

bool dm_mc02_set_adc_voltage_payload(DmMc02Frame *frame,
                                     const DmMc02AdcVoltage *voltage)
{
    if (!frame || !voltage ||
        voltage->channel > DM_MC02_ADC_INPUT_MAX_CHANNEL ||
        (voltage->flags & ~DM_MC02_ADC_VOLTAGE_FLAGS_MASK) != 0 ||
        voltage->voltage_uv > DM_MC02_ADC_VOLTAGE_MAX_UV ||
        voltage->reserved != 0) {
        return false;
    }
    dm_mc02_wire_put16(frame->payload, voltage->channel);
    dm_mc02_wire_put16(frame->payload + 2, voltage->flags);
    dm_mc02_wire_put32(frame->payload + 4, voltage->voltage_uv);
    dm_mc02_wire_put32(frame->payload + 8, voltage->reserved);
    frame->header.type = DM_MC02_FRAME_ADC_VOLTAGE;
    frame->header.payload_len = DM_MC02_ADC_VOLTAGE_WIRE_PAYLOAD_SIZE;
    return true;
}

bool dm_mc02_get_adc_voltage_payload(const DmMc02Frame *frame,
                                     DmMc02AdcVoltage *voltage)
{
    if (!frame || !voltage || frame->header.type != DM_MC02_FRAME_ADC_VOLTAGE ||
        frame->header.payload_len != DM_MC02_ADC_VOLTAGE_WIRE_PAYLOAD_SIZE) {
        return false;
    }
    voltage->channel = dm_mc02_wire_get16(frame->payload);
    voltage->flags = dm_mc02_wire_get16(frame->payload + 2);
    voltage->voltage_uv = dm_mc02_wire_get32(frame->payload + 4);
    voltage->reserved = dm_mc02_wire_get32(frame->payload + 8);
    return voltage->channel <= DM_MC02_ADC_INPUT_MAX_CHANNEL &&
           (voltage->flags & ~DM_MC02_ADC_VOLTAGE_FLAGS_MASK) == 0 &&
           voltage->voltage_uv <= DM_MC02_ADC_VOLTAGE_MAX_UV &&
           voltage->reserved == 0;
}
