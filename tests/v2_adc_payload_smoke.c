#include "dm_mc02_v2_payload.h"
#include "dm_mc02_wire.h"

#include <stdio.h>

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

int main(void)
{
    uint8_t payload[DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE] = { 0 };
    DmMc02V2AdcInputPayload input = { .channel = 31, .raw = 0xffff };
    DmMc02V2AdcInputPayload decoded_input;
    DmMc02V2AdcVoltagePayload voltage = {
        .channel = 19,
        .flags = DM_MC02_V2_ADC_VOLTAGE_FLAG_PIN_OVERRIDE,
        .voltage_uv = DM_MC02_V2_ADC_VOLTAGE_MAX_UV,
    };
    DmMc02V2AdcVoltagePayload decoded_voltage;

    if (expect(dm_mc02_v2_payload_encode_adc_input(
                   payload, sizeof(payload), &input) ==
                   DM_MC02_V2_ADC_INPUT_PAYLOAD_SIZE,
               "ADC input encode") ||
        expect(dm_mc02_v2_payload_decode_adc_input(
                   &decoded_input, payload, DM_MC02_V2_ADC_INPUT_PAYLOAD_SIZE),
               "ADC input decode") ||
        expect(decoded_input.channel == input.channel &&
                   decoded_input.raw == input.raw && payload[4] == 0 &&
                   payload[5] == 0 && payload[6] == 0 && payload[7] == 0,
               "ADC input round trip and reserved")) {
        return 1;
    }

    if (expect(dm_mc02_v2_payload_encode_adc_voltage(
                   payload, sizeof(payload), &voltage) ==
                   DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE,
               "ADC voltage encode") ||
        expect(dm_mc02_v2_payload_decode_adc_voltage(
                   &decoded_voltage, payload,
                   DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE),
               "ADC voltage decode") ||
        expect(decoded_voltage.channel == voltage.channel &&
                   decoded_voltage.flags == voltage.flags &&
                   decoded_voltage.voltage_uv == voltage.voltage_uv,
               "ADC voltage round trip")) {
        return 1;
    }

    dm_mc02_wire_put32(payload + 8, 1);
    if (expect(!dm_mc02_v2_payload_validate_adc_voltage(
                   payload, DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE),
               "reject ADC voltage reserved")) {
        return 1;
    }
    dm_mc02_wire_put32(payload + 8, 0);
    dm_mc02_wire_put16(payload + 2, 2);
    if (expect(!dm_mc02_v2_payload_validate_adc_voltage(
                   payload, DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE),
               "reject ADC voltage flags")) {
        return 1;
    }
    dm_mc02_wire_put16(payload + 2, voltage.flags);
    dm_mc02_wire_put32(payload + 4, DM_MC02_V2_ADC_VOLTAGE_MAX_UV + 1);
    if (expect(!dm_mc02_v2_payload_validate_adc_voltage(
                   payload, DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE),
               "reject ADC voltage range")) {
        return 1;
    }
    puts("RESULT: shared v2 ADC payload codec smoke passed");
    return 0;
}
