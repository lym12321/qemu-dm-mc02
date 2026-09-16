#include "dm_mc02_wire.h"

#include <stdio.h>
#include <string.h>

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
    DmMc02WireFrame frame = { 0 };
    DmMc02WireFrame decoded = { 0 };
    uint8_t wire[DM_MC02_WIRE_HEADER_SIZE + DM_MC02_WIRE_MAX_PAYLOAD];
    uint8_t payload[DM_MC02_WIRE_MAX_PAYLOAD] = { 0 };
    size_t length;

    if (expect(dm_mc02_wire_get16((const uint8_t[]) { 0x34, 0x12 }) ==
                   UINT16_C(0x1234), "little-endian u16 decode") ||
        expect(dm_mc02_wire_get32((const uint8_t[]) {
                       0x78, 0x56, 0x34, 0x12 }) == UINT32_C(0x12345678),
               "little-endian u32 decode") ||
        expect(dm_mc02_wire_get64((const uint8_t[]) {
                       0xef, 0xcd, 0xab, 0x89, 0x67, 0x45, 0x23, 0x01 }) ==
                   UINT64_C(0x0123456789abcdef),
               "little-endian u64 decode")) {
        return 1;
    }

    frame.header.version = DM_MC02_WIRE_VERSION;
    frame.header.type = DM_MC02_WIRE_FRAME_RESET;
    frame.header.sequence = UINT64_C(0x0102030405060708);
    frame.header.virtual_time_ns = UINT64_C(0x1112131415161718);
    length = dm_mc02_wire_encode(wire, sizeof(wire), &frame);
    if (expect(length == DM_MC02_WIRE_HEADER_SIZE, "RESET wire size") ||
        expect(wire[0] == 0x44 && wire[1] == 0x4d && wire[2] == 0x43 &&
                   wire[3] == 0x32,
               "wire magic bytes") ||
        expect(dm_mc02_wire_decode(&decoded, wire, length),
               "RESET decode") ||
        expect(frame.header.version == decoded.header.version &&
                   frame.header.type == decoded.header.type &&
                   frame.header.payload_len == decoded.header.payload_len &&
                   frame.header.sequence == decoded.header.sequence &&
                   frame.header.virtual_time_ns ==
                       decoded.header.virtual_time_ns,
               "RESET header round-trip") ||
        expect(dm_mc02_wire_payload_valid(decoded.header.type,
                                          decoded.payload,
                                          decoded.header.payload_len),
               "RESET payload validation")) {
        return 1;
    }
    if (expect(!dm_mc02_wire_decode(&decoded, wire, length - 1),
                "reject truncated frame") ||
        expect(!dm_mc02_wire_decode(&decoded, wire, length + 1),
               "reject trailing frame bytes")) {
        return 1;
    }

    frame = (DmMc02WireFrame) { 0 };
    frame.header.version = DM_MC02_WIRE_VERSION;
    frame.header.type = DM_MC02_WIRE_FRAME_IMU_SAMPLE;
    frame.header.payload_len = 24;
    for (unsigned i = 0; i < 6; ++i) {
        dm_mc02_wire_put32(frame.payload + i * 4,
                           UINT32_C(0x3f800000) + i);
    }
    length = dm_mc02_wire_encode(wire, sizeof(wire), &frame);
    if (expect(length == DM_MC02_WIRE_HEADER_SIZE + 24,
               "IMU wire size") ||
        expect(dm_mc02_wire_decode(&decoded, wire, length), "IMU decode") ||
        expect(dm_mc02_wire_payload_valid(decoded.header.type,
                                          decoded.payload,
                                          decoded.header.payload_len),
               "finite IMU payload validation")) {
        return 1;
    }

    dm_mc02_wire_put32(frame.payload, UINT32_C(0x7fc00000));
    if (expect(!dm_mc02_wire_payload_valid(DM_MC02_WIRE_FRAME_IMU_SAMPLE,
                                           frame.payload, 24),
                "reject non-finite IMU payload") ||
        expect(dm_mc02_wire_encode(wire, sizeof(wire), &frame) != 0,
               "structure codec accepts semantic-invalid frame")) {
        return 1;
    }

    memset(payload, 0, sizeof(payload));
    dm_mc02_wire_put16(payload, 4);
    dm_mc02_wire_put16(payload + 2, UINT16_C(0x1234));
    if (expect(dm_mc02_wire_payload_valid(DM_MC02_WIRE_FRAME_ADC_INPUT,
                                          payload, 8),
               "valid ADC input payload") ||
        expect(!dm_mc02_wire_payload_valid(DM_MC02_WIRE_FRAME_ADC_INPUT,
                                           payload, 7),
               "reject short ADC input payload")) {
        return 1;
    }
    dm_mc02_wire_put32(payload + 4, 1);
    if (expect(!dm_mc02_wire_payload_valid(DM_MC02_WIRE_FRAME_ADC_INPUT,
                                           payload, 8),
               "reject ADC input reserved field")) {
        return 1;
    }

    memset(payload, 0, sizeof(payload));
    dm_mc02_wire_put16(payload, DM_MC02_WIRE_ADC_MAX_CHANNEL);
    dm_mc02_wire_put16(payload + 2,
                       DM_MC02_WIRE_ADC_VOLTAGE_FLAG_PIN_OVERRIDE);
    dm_mc02_wire_put32(payload + 4, DM_MC02_WIRE_ADC_VOLTAGE_MAX_UV);
    if (expect(dm_mc02_wire_payload_valid(DM_MC02_WIRE_FRAME_ADC_VOLTAGE,
                                          payload, 12),
               "valid ADC voltage payload")) {
        return 1;
    }
    dm_mc02_wire_put32(payload + 4, DM_MC02_WIRE_ADC_VOLTAGE_MAX_UV + 1u);
    if (expect(!dm_mc02_wire_payload_valid(DM_MC02_WIRE_FRAME_ADC_VOLTAGE,
                                           payload, 12),
               "reject ADC voltage upper bound")) {
        return 1;
    }

    frame.header.type = 99;
    frame.header.payload_len = 0;
    if (expect(dm_mc02_wire_encode(wire, sizeof(wire), &frame) == 0,
               "reject unknown frame type") ||
        expect(dm_mc02_wire_expected_payload_len(99) == UINT32_MAX,
               "unknown type has no payload size")) {
        return 1;
    }

    puts("RESULT: shared v1 wire codec smoke passed");
    return 0;
}
