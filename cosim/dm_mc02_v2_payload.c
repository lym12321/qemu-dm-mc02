#include "dm_mc02_v2_payload.h"

#include "dm_mc02_wire.h"

static bool valid_status(uint32_t status)
{
    return status <= DM_MC02_V2_STATUS_UNSUPPORTED;
}

static bool valid_size(const uint8_t *payload, size_t payload_len,
                       size_t expected)
{
    return payload != NULL && payload_len == expected;
}

bool dm_mc02_v2_payload_validate_reset_ack(const uint8_t *payload,
                                           size_t payload_len)
{
    return valid_size(payload, payload_len,
                      DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE) &&
           valid_status(dm_mc02_wire_get32(payload)) &&
           (dm_mc02_wire_get32(payload + 4) &
            ~DM_MC02_V2_CAPABILITY_MASK) == 0 &&
           dm_mc02_wire_get32(payload + 20) == 0;
}

bool dm_mc02_v2_payload_validate_step_ack(const uint8_t *payload,
                                          size_t payload_len)
{
    return valid_size(payload, payload_len,
                      DM_MC02_V2_STEP_ACK_PAYLOAD_SIZE) &&
           valid_status(dm_mc02_wire_get32(payload)) &&
           dm_mc02_wire_get32(payload + 12) == 0;
}

bool dm_mc02_v2_payload_validate_diagnostics(const uint8_t *payload,
                                             size_t payload_len)
{
    return valid_size(payload, payload_len,
                      DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE);
}

bool dm_mc02_v2_payload_validate_step_done(const uint8_t *payload,
                                           size_t payload_len)
{
    uint32_t status;
    uint32_t consumed;
    uint32_t missing;

    if (!valid_size(payload, payload_len,
                    DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE)) {
        return false;
    }
    status = dm_mc02_wire_get32(payload);
    consumed = dm_mc02_wire_get32(payload + 4);
    missing = dm_mc02_wire_get32(payload + 8);
    if (!valid_status(status) ||
        (consumed & ~DM_MC02_V2_CONSUMED_MASK) != 0 ||
        (missing & ~DM_MC02_V2_CONSUMED_MASK) != 0 ||
        (consumed & missing) != 0 ||
        missing != (DM_MC02_V2_CONSUMED_MASK & ~consumed)) {
        return false;
    }
    return status != DM_MC02_V2_STATUS_OK ||
           (consumed == DM_MC02_V2_CONSUMED_MASK && missing == 0);
}

bool dm_mc02_v2_payload_validate_board_telemetry(const uint8_t *payload,
                                                 size_t payload_len)
{
    return valid_size(payload, payload_len,
                      DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE);
}

bool dm_mc02_v2_payload_validate_adc_input(const uint8_t *payload,
                                           size_t payload_len)
{
    return valid_size(payload, payload_len, DM_MC02_V2_ADC_INPUT_PAYLOAD_SIZE) &&
           dm_mc02_wire_get16(payload) <= DM_MC02_V2_ADC_MAX_CHANNEL &&
           dm_mc02_wire_get32(payload + 4) == 0;
}

bool dm_mc02_v2_payload_validate_adc_voltage(const uint8_t *payload,
                                             size_t payload_len)
{
    return valid_size(payload, payload_len,
                      DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE) &&
           dm_mc02_wire_get16(payload) <= DM_MC02_V2_ADC_MAX_CHANNEL &&
           (dm_mc02_wire_get16(payload + 2) &
            (uint16_t)~DM_MC02_V2_ADC_VOLTAGE_FLAGS_MASK) == 0 &&
           dm_mc02_wire_get32(payload + 4) <=
               DM_MC02_V2_ADC_VOLTAGE_MAX_UV &&
           dm_mc02_wire_get32(payload + 8) == 0;
}

size_t dm_mc02_v2_payload_encode_reset_ack(
    uint8_t *out, size_t capacity, const DmMc02V2ResetAckPayload *value)
{
    if (!out || !value || capacity < DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE ||
        !valid_status(value->status) ||
        (value->capabilities & ~DM_MC02_V2_CAPABILITY_MASK) != 0) {
        return 0;
    }
    dm_mc02_wire_put32(out, value->status);
    dm_mc02_wire_put32(out + 4, value->capabilities);
    dm_mc02_wire_put64(out + 8, value->virtual_time_ns);
    dm_mc02_wire_put32(out + 16, value->queue_depth);
    dm_mc02_wire_put32(out + 20, 0);
    return DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE;
}

size_t dm_mc02_v2_payload_encode_step_ack(
    uint8_t *out, size_t capacity, const DmMc02V2StepAckPayload *value)
{
    if (!out || !value || capacity < DM_MC02_V2_STEP_ACK_PAYLOAD_SIZE ||
        !valid_status(value->status)) {
        return 0;
    }
    dm_mc02_wire_put32(out, value->status);
    dm_mc02_wire_put32(out + 4, value->queue_depth);
    dm_mc02_wire_put32(out + 8, value->dropped_count);
    dm_mc02_wire_put32(out + 12, 0);
    return DM_MC02_V2_STEP_ACK_PAYLOAD_SIZE;
}

size_t dm_mc02_v2_payload_encode_diagnostics(
    uint8_t *out, size_t capacity, const DmMc02V2DiagnosticsPayload *value)
{
    if (!out || !value || capacity < DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE) {
        return 0;
    }
    dm_mc02_wire_put64(out, value->rx_frames);
    dm_mc02_wire_put64(out + 8, value->rx_bad_frames);
    dm_mc02_wire_put64(out + 16, value->rx_dropped_bytes);
    dm_mc02_wire_put64(out + 24, value->tx_frames);
    dm_mc02_wire_put64(out + 32, value->tx_dropped);
    return DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE;
}

size_t dm_mc02_v2_payload_encode_step_done(
    uint8_t *out, size_t capacity, const DmMc02V2StepDonePayload *value)
{
    if (!out || !value || capacity < DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE ||
        !valid_status(value->status) ||
        (value->consumed_mask & ~DM_MC02_V2_CONSUMED_MASK) != 0 ||
        (value->missing_mask & ~DM_MC02_V2_CONSUMED_MASK) != 0 ||
        (value->consumed_mask & value->missing_mask) != 0 ||
        value->missing_mask !=
            (DM_MC02_V2_CONSUMED_MASK & ~value->consumed_mask) ||
        (value->status == DM_MC02_V2_STATUS_OK &&
         (value->consumed_mask != DM_MC02_V2_CONSUMED_MASK ||
          value->missing_mask != 0))) {
        return 0;
    }
    dm_mc02_wire_put32(out, value->status);
    dm_mc02_wire_put32(out + 4, value->consumed_mask);
    dm_mc02_wire_put32(out + 8, value->missing_mask);
    dm_mc02_wire_put32(out + 12, value->queue_depth);
    dm_mc02_wire_put32(out + 16, value->dropped_count);
    return DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE;
}

size_t dm_mc02_v2_payload_encode_board_telemetry(
    uint8_t *out, size_t capacity,
    const DmMc02V2BoardTelemetryPayload *value)
{
    if (!out || !value ||
        capacity < DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE) {
        return 0;
    }
    dm_mc02_wire_put32(out, value->led_rgb);
    dm_mc02_wire_put32(out + 4, value->led_brightness);
    dm_mc02_wire_put32(out + 8, value->buzzer);
    dm_mc02_wire_put32(out + 12, value->board_flags);
    dm_mc02_wire_put32(out + 16, value->buzzer_frequency_hz);
    dm_mc02_wire_put32(out + 20, value->buzzer_duty_permille);
    return DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE;
}

size_t dm_mc02_v2_payload_encode_adc_input(
    uint8_t *out, size_t capacity, const DmMc02V2AdcInputPayload *value)
{
    if (!out || !value || capacity < DM_MC02_V2_ADC_INPUT_PAYLOAD_SIZE ||
        value->channel > DM_MC02_V2_ADC_MAX_CHANNEL) {
        return 0;
    }
    dm_mc02_wire_put16(out, value->channel);
    dm_mc02_wire_put16(out + 2, value->raw);
    dm_mc02_wire_put32(out + 4, 0);
    return DM_MC02_V2_ADC_INPUT_PAYLOAD_SIZE;
}

size_t dm_mc02_v2_payload_encode_adc_voltage(
    uint8_t *out, size_t capacity,
    const DmMc02V2AdcVoltagePayload *value)
{
    if (!out || !value || capacity < DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE ||
        value->channel > DM_MC02_V2_ADC_MAX_CHANNEL ||
        (value->flags & (uint16_t)~DM_MC02_V2_ADC_VOLTAGE_FLAGS_MASK) != 0 ||
        value->voltage_uv > DM_MC02_V2_ADC_VOLTAGE_MAX_UV) {
        return 0;
    }
    dm_mc02_wire_put16(out, value->channel);
    dm_mc02_wire_put16(out + 2, value->flags);
    dm_mc02_wire_put32(out + 4, value->voltage_uv);
    dm_mc02_wire_put32(out + 8, 0);
    return DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE;
}

bool dm_mc02_v2_payload_decode_reset_ack(
    DmMc02V2ResetAckPayload *value, const uint8_t *payload,
    size_t payload_len)
{
    if (!value || !dm_mc02_v2_payload_validate_reset_ack(payload,
                                                          payload_len)) {
        return false;
    }
    value->status = dm_mc02_wire_get32(payload);
    value->capabilities = dm_mc02_wire_get32(payload + 4);
    value->virtual_time_ns = dm_mc02_wire_get64(payload + 8);
    value->queue_depth = dm_mc02_wire_get32(payload + 16);
    return true;
}

bool dm_mc02_v2_payload_decode_step_ack(
    DmMc02V2StepAckPayload *value, const uint8_t *payload,
    size_t payload_len)
{
    if (!value || !dm_mc02_v2_payload_validate_step_ack(payload, payload_len)) {
        return false;
    }
    value->status = dm_mc02_wire_get32(payload);
    value->queue_depth = dm_mc02_wire_get32(payload + 4);
    value->dropped_count = dm_mc02_wire_get32(payload + 8);
    return true;
}

bool dm_mc02_v2_payload_decode_diagnostics(
    DmMc02V2DiagnosticsPayload *value, const uint8_t *payload,
    size_t payload_len)
{
    if (!value || !dm_mc02_v2_payload_validate_diagnostics(payload,
                                                            payload_len)) {
        return false;
    }
    value->rx_frames = dm_mc02_wire_get64(payload);
    value->rx_bad_frames = dm_mc02_wire_get64(payload + 8);
    value->rx_dropped_bytes = dm_mc02_wire_get64(payload + 16);
    value->tx_frames = dm_mc02_wire_get64(payload + 24);
    value->tx_dropped = dm_mc02_wire_get64(payload + 32);
    return true;
}

bool dm_mc02_v2_payload_decode_step_done(
    DmMc02V2StepDonePayload *value, const uint8_t *payload,
    size_t payload_len)
{
    if (!value || !dm_mc02_v2_payload_validate_step_done(payload,
                                                          payload_len)) {
        return false;
    }
    value->status = dm_mc02_wire_get32(payload);
    value->consumed_mask = dm_mc02_wire_get32(payload + 4);
    value->missing_mask = dm_mc02_wire_get32(payload + 8);
    value->queue_depth = dm_mc02_wire_get32(payload + 12);
    value->dropped_count = dm_mc02_wire_get32(payload + 16);
    return true;
}

bool dm_mc02_v2_payload_decode_board_telemetry(
    DmMc02V2BoardTelemetryPayload *value, const uint8_t *payload,
    size_t payload_len)
{
    if (!value || !dm_mc02_v2_payload_validate_board_telemetry(payload,
                                                                payload_len)) {
        return false;
    }
    value->led_rgb = dm_mc02_wire_get32(payload);
    value->led_brightness = dm_mc02_wire_get32(payload + 4);
    value->buzzer = dm_mc02_wire_get32(payload + 8);
    value->board_flags = dm_mc02_wire_get32(payload + 12);
    value->buzzer_frequency_hz = dm_mc02_wire_get32(payload + 16);
    value->buzzer_duty_permille = dm_mc02_wire_get32(payload + 20);
    return true;
}

bool dm_mc02_v2_payload_decode_adc_input(
    DmMc02V2AdcInputPayload *value, const uint8_t *payload,
    size_t payload_len)
{
    if (!value || !dm_mc02_v2_payload_validate_adc_input(payload,
                                                          payload_len)) {
        return false;
    }
    value->channel = dm_mc02_wire_get16(payload);
    value->raw = dm_mc02_wire_get16(payload + 2);
    return true;
}

bool dm_mc02_v2_payload_decode_adc_voltage(
    DmMc02V2AdcVoltagePayload *value, const uint8_t *payload,
    size_t payload_len)
{
    if (!value || !dm_mc02_v2_payload_validate_adc_voltage(payload,
                                                            payload_len)) {
        return false;
    }
    value->channel = dm_mc02_wire_get16(payload);
    value->flags = dm_mc02_wire_get16(payload + 2);
    value->voltage_uv = dm_mc02_wire_get32(payload + 4);
    return true;
}
