#include "dm_mc02_protocol.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

static uint64_t fnv1a(const uint8_t *data, size_t size, uint64_t hash)
{
    for (size_t i = 0; i < size; ++i) {
        hash ^= data[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

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
    DmMc02StreamValidator validator;
    uint8_t wire[DM_MC02_PROTOCOL_HEADER_SIZE + DM_MC02_PROTOCOL_MAX_PAYLOAD];
    uint64_t hash = UINT64_C(1469598103934665603);

    if (expect(DM_MC02_IMU_WIRE_PAYLOAD_SIZE == 24,
               "IMU wire payload size") ||
        expect(DM_MC02_TELEMETRY_WIRE_PAYLOAD_SIZE == 12,
               "telemetry wire payload size") ||
        expect(DM_MC02_ADC_INPUT_WIRE_PAYLOAD_SIZE == 8,
               "ADC input wire payload size") ||
        expect(DM_MC02_ADC_VOLTAGE_WIRE_PAYLOAD_SIZE == 12,
               "ADC voltage wire payload size")) {
        return 1;
    }
    {
        DmMc02Frame tx = {0};
        DmMc02Frame rx = {0};
        DmMc02AdcVoltage voltage = {19, DM_MC02_ADC_VOLTAGE_FLAG_PIN_OVERRIDE,
                                    3300000, 0};
        DmMc02AdcVoltage decoded = {0};
        DmMc02StreamValidator voltage_validator;

        dm_mc02_stream_validator_reset(&voltage_validator);
        tx.header.version = DM_MC02_PROTOCOL_VERSION;
        tx.header.sequence = 10;
        tx.header.virtual_time_ns = 1000;
        if (expect(dm_mc02_set_adc_voltage_payload(&tx, &voltage),
                   "set ADC voltage payload") ||
            expect(dm_mc02_frame_encode(wire, sizeof(wire), &tx) != 0,
                   "encode ADC voltage frame") ||
            expect(dm_mc02_frame_decode(&rx, wire,
                                        DM_MC02_PROTOCOL_HEADER_SIZE + 12),
                   "decode ADC voltage frame") ||
            expect(dm_mc02_get_adc_voltage_payload(&rx, &decoded),
                   "get ADC voltage payload") ||
            expect(memcmp(&voltage, &decoded, sizeof(voltage)) == 0,
                   "ADC voltage round-trip") ||
            expect(dm_mc02_validate_frame(&voltage_validator, &rx),
                   "validate ADC voltage frame")) {
            return 1;
        }

        voltage.channel = 32;
        if (expect(!dm_mc02_set_adc_voltage_payload(&tx, &voltage),
                   "reject ADC voltage channel")) {
            return 1;
        }
        voltage.channel = 19;
        voltage.flags = 2;
        if (expect(!dm_mc02_set_adc_voltage_payload(&tx, &voltage),
                   "reject ADC voltage flags")) {
            return 1;
        }
        voltage.flags = DM_MC02_ADC_VOLTAGE_FLAG_PIN_OVERRIDE;
        voltage.voltage_uv = DM_MC02_ADC_VOLTAGE_MAX_UV + 1u;
        if (expect(!dm_mc02_set_adc_voltage_payload(&tx, &voltage),
                   "reject ADC voltage upper bound")) {
            return 1;
        }
        voltage.voltage_uv = 3300000;
        voltage.reserved = 1;
        if (expect(!dm_mc02_set_adc_voltage_payload(&tx, &voltage),
                   "reject ADC voltage reserved")) {
            return 1;
        }
        voltage.reserved = 0;
        dm_mc02_set_adc_voltage_payload(&tx, &voltage);
        tx.header.sequence = 11;
        tx.header.virtual_time_ns = 1100;
        dm_mc02_frame_encode(wire, sizeof(wire), &tx);
        dm_mc02_frame_decode(&rx, wire, DM_MC02_PROTOCOL_HEADER_SIZE + 12);
        rx.payload[8] = 1;
        if (expect(!dm_mc02_get_adc_voltage_payload(&rx, &decoded),
                   "decode rejects ADC voltage reserved")) {
            return 1;
        }
        dm_mc02_set_adc_voltage_payload(&tx, &voltage);
        tx.header.sequence = 11;
        tx.header.virtual_time_ns = 1100;
        dm_mc02_frame_encode(wire, sizeof(wire), &tx);
        dm_mc02_frame_decode(&rx, wire, DM_MC02_PROTOCOL_HEADER_SIZE + 12);
        rx.header.sequence = 10;
        if (expect(!dm_mc02_validate_frame(&voltage_validator, &rx),
                   "reject ADC voltage sequence rollback")) {
            return 1;
        }
        rx.header.sequence = 12;
        rx.header.virtual_time_ns = 999;
        if (expect(!dm_mc02_validate_frame(&voltage_validator, &rx),
                   "reject ADC voltage timestamp rollback")) {
            return 1;
        }
    }
    dm_mc02_stream_validator_reset(&validator);

    for (uint64_t i = 0; i < 1000; ++i) {
        DmMc02Frame tx = {0};
        DmMc02Frame rx = {0};
        DmMc02ImuSample sample = {
            { (float)i * 0.001f, -0.25f, 1.5f },
            { 0.0f, 0.0f, 9.80665f },
        };
        tx.header.version = DM_MC02_PROTOCOL_VERSION;
        tx.header.sequence = i + 1;
        tx.header.virtual_time_ns = (i + 1) * UINT64_C(1000000);
        if (expect(dm_mc02_set_imu_payload(&tx, &sample), "set IMU payload") ||
            expect(dm_mc02_frame_encode(wire, sizeof(wire), &tx) != 0,
                   "encode IMU frame") ||
            expect(dm_mc02_frame_decode(&rx, wire,
                                        DM_MC02_PROTOCOL_HEADER_SIZE +
                                            DM_MC02_IMU_WIRE_PAYLOAD_SIZE),
                   "decode IMU frame") ||
            expect(dm_mc02_validate_frame(&validator, &rx),
                   "validate monotonic IMU frame")) {
            return 1;
        }
        hash = fnv1a(wire, DM_MC02_PROTOCOL_HEADER_SIZE +
                              DM_MC02_IMU_WIRE_PAYLOAD_SIZE, hash);
    }

    {
        DmMc02Frame tx = {0};
        DmMc02Frame rx = {0};
        DmMc02AdcInput input = {4, 0x1234, 0};
        DmMc02AdcInput decoded_input = {0};
        DmMc02StreamValidator adc_validator;

        dm_mc02_stream_validator_reset(&adc_validator);
        tx.header.version = DM_MC02_PROTOCOL_VERSION;
        tx.header.sequence = 1;
        tx.header.virtual_time_ns = 100;
        if (expect(dm_mc02_set_adc_input_payload(&tx, &input),
                   "set ADC input payload") ||
            expect(dm_mc02_frame_encode(wire, sizeof(wire), &tx) != 0,
                   "encode ADC input frame") ||
            expect(dm_mc02_frame_decode(&rx, wire,
                                        DM_MC02_PROTOCOL_HEADER_SIZE + 8),
                   "decode ADC input frame") ||
            expect(dm_mc02_get_adc_input_payload(&rx, &decoded_input),
                   "get ADC input payload") ||
            expect(memcmp(&input, &decoded_input, sizeof(input)) == 0,
                   "ADC input round-trip") ||
            expect(dm_mc02_validate_frame(&adc_validator, &rx),
                   "validate ADC input frame")) {
            return 1;
        }

        rx.payload[4] = 1;
        if (expect(!dm_mc02_get_adc_input_payload(&rx, &decoded_input),
                   "reject ADC reserved") ||
            expect(!dm_mc02_validate_frame(&adc_validator, &rx),
                   "validate rejects ADC reserved")) {
            return 1;
        }
        dm_mc02_set_adc_input_payload(&tx, &input);
        tx.header.sequence = 2;
        tx.header.virtual_time_ns = 200;
        dm_mc02_frame_decode(&rx, wire, DM_MC02_PROTOCOL_HEADER_SIZE + 8);
        rx.payload[0] = 32;
        rx.payload[1] = 0;
        if (expect(!dm_mc02_get_adc_input_payload(&rx, &decoded_input),
                   "reject ADC channel") ||
            expect(!dm_mc02_validate_frame(&adc_validator, &rx),
                   "validate rejects ADC channel")) {
            return 1;
        }
        dm_mc02_set_adc_input_payload(&tx, &input);
        tx.header.sequence = 2;
        tx.header.virtual_time_ns = 200;
        if (expect(dm_mc02_frame_encode(wire, sizeof(wire), &tx) != 0,
                   "encode second ADC frame") ||
            expect(dm_mc02_frame_decode(&rx, wire,
                                        DM_MC02_PROTOCOL_HEADER_SIZE + 8),
                   "decode second ADC frame") ||
            expect(dm_mc02_validate_frame(&adc_validator, &rx),
                   "validate increasing ADC frame")) {
            return 1;
        }
        dm_mc02_stream_validator_reset(&adc_validator);
        if (expect(dm_mc02_validate_frame(&adc_validator, &rx),
                   "revalidate ADC baseline")) {
            return 1;
        }
        rx.header.sequence = 1;
        rx.header.virtual_time_ns = 300;
        if (expect(!dm_mc02_validate_frame(&adc_validator, &rx),
                   "reject ADC sequence rollback")) {
            return 1;
        }
        dm_mc02_stream_validator_reset(&adc_validator);
        rx.header.sequence = 1;
        rx.header.virtual_time_ns = 100;
        if (expect(dm_mc02_validate_frame(&adc_validator, &rx),
                   "validate ADC time baseline")) {
            return 1;
        }
        rx.header.sequence = 3;
        rx.header.virtual_time_ns = 99;
        if (expect(!dm_mc02_validate_frame(&adc_validator, &rx),
                   "reject ADC timestamp rollback")) {
            return 1;
        }
    }
    {
        DmMc02Frame bad = {0};
        DmMc02Frame decoded = {0};
        bad.header.version = DM_MC02_PROTOCOL_VERSION;
        bad.header.sequence = 1001;
        bad.header.virtual_time_ns = UINT64_C(1001000000);
        bad.header.type = DM_MC02_FRAME_IMU_SAMPLE;
        bad.header.payload_len = DM_MC02_IMU_WIRE_PAYLOAD_SIZE;
        DmMc02ImuSample invalid = {0};
        invalid.gyro[0] = NAN;
        dm_mc02_set_imu_payload(&bad, &invalid);
        if (expect(dm_mc02_frame_encode(wire, sizeof(wire), &bad) != 0,
                   "encode NaN frame") ||
            expect(dm_mc02_frame_decode(&decoded, wire,
                                        DM_MC02_PROTOCOL_HEADER_SIZE + bad.header.payload_len),
                   "decode NaN frame") ||
            expect(!dm_mc02_validate_frame(&validator, &decoded),
                   "reject NaN frame")) {
            return 1;
        }
        wire[0] ^= 1;
        if (expect(!dm_mc02_frame_decode(&decoded, wire,
                                         DM_MC02_PROTOCOL_HEADER_SIZE + bad.header.payload_len),
                    "reject bad magic")) {
            return 1;
        }
    }
    {
        DmMc02Frame telemetry = {0};
        DmMc02Frame reset = {0};
        DmMc02Frame restarted = {0};
        DmMc02Telemetry payload = {0};

        telemetry.header.version = DM_MC02_PROTOCOL_VERSION;
        telemetry.header.sequence = 1002;
        telemetry.header.virtual_time_ns = UINT64_C(1002000000);
        payload.reserved = 1;
        if (expect(dm_mc02_set_telemetry_payload(&telemetry, &payload),
                   "set telemetry payload") ||
            expect(dm_mc02_frame_encode(wire, sizeof(wire), &telemetry) != 0,
                   "encode reserved telemetry") ||
            expect(dm_mc02_frame_decode(&restarted, wire,
                                        DM_MC02_PROTOCOL_HEADER_SIZE +
                                            DM_MC02_TELEMETRY_WIRE_PAYLOAD_SIZE),
                   "decode reserved telemetry") ||
            expect(!dm_mc02_validate_frame(&validator, &restarted),
                   "reject non-zero telemetry reserved")) {
            return 1;
        }

        reset.header.version = DM_MC02_PROTOCOL_VERSION;
        reset.header.type = DM_MC02_FRAME_RESET;
        reset.header.sequence = 0;
        reset.header.virtual_time_ns = 0;
        if (expect(dm_mc02_frame_encode(wire, sizeof(wire), &reset) != 0,
                   "encode RESET frame") ||
            expect(dm_mc02_frame_decode(&restarted, wire,
                                        DM_MC02_PROTOCOL_HEADER_SIZE),
                   "decode RESET frame") ||
            expect(dm_mc02_validate_frame(&validator, &restarted),
                   "accept RESET as session start")) {
            return 1;
        }

        restarted.header.type = DM_MC02_FRAME_ACK;
        restarted.header.payload_len = 0;
        restarted.header.sequence = 1;
        restarted.header.virtual_time_ns = 1;
        if (expect(dm_mc02_validate_frame(&validator, &restarted),
                   "accept sequence restart after RESET")) {
            return 1;
        }
    }
    printf("RESULT: cosim codec smoke passed\nframes=1000 hash=0x%016llx\n",
           (unsigned long long)hash);
    return 0;
}
