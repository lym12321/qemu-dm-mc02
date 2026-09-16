#include "dm_mc02_v2_wire.h"

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
    DmMc02V2WireFrame frame = { 0 };
    DmMc02V2WireFrame decoded = { 0 };
    DmMc02V2WireSection section;
    uint8_t wire[DM_MC02_V2_WIRE_BODY_PREFIX_SIZE +
                 DM_MC02_V2_WIRE_HEADER_SIZE +
                 DM_MC02_V2_WIRE_MAX_PAYLOAD];
    uint8_t payload[DM_MC02_V2_WIRE_MAX_PAYLOAD] = { 0 };
    size_t length;
    size_t offset;
    uint16_t count;
    uint16_t marker;

    frame.header.version = DM_MC02_V2_WIRE_VERSION;
    frame.header.kind = DM_MC02_V2_WIRE_STEP;
    frame.header.payload_len = 3;
    frame.header.step_id = UINT64_C(0x0102030405060708);
    frame.header.t_sim_ns = UINT64_C(0x1112131415161718);
    frame.header.dt_ns = UINT64_C(0x2122232425262728);
    frame.header.session_id = UINT32_C(0x31415926);
    frame.payload[0] = 0xa5;
    frame.payload[1] = 0x5a;
    frame.payload[2] = 0x11;
    length = dm_mc02_v2_wire_encode(wire, sizeof(wire), &frame);
    if (expect(length == 4 + 36 + 3, "v2 body length") ||
        expect(wire[0] == 0x44 && wire[1] == 0x4d && wire[2] == 0x43 &&
                   wire[3] == 0x32,
               "v2 magic bytes") ||
        expect(dm_mc02_v2_wire_decode(&decoded, wire, length),
               "v2 frame decode") ||
        expect(decoded.header.kind == frame.header.kind &&
                   decoded.header.payload_len == frame.header.payload_len &&
                   decoded.header.step_id == frame.header.step_id &&
                   decoded.header.t_sim_ns == frame.header.t_sim_ns &&
                   decoded.header.dt_ns == frame.header.dt_ns &&
                   decoded.header.session_id == frame.header.session_id &&
                   memcmp(decoded.payload, frame.payload, 3) == 0,
               "v2 frame round-trip")) {
        return 1;
    }
    if (expect(!dm_mc02_v2_wire_decode(&decoded, wire, length - 1),
                "reject truncated v2 body") ||
        expect(!dm_mc02_v2_wire_decode(&decoded, wire, length + 1),
               "reject trailing v2 body") ||
        expect(!dm_mc02_v2_wire_known_kind(0) &&
                   !dm_mc02_v2_wire_known_kind(9),
               "reject unknown v2 kind")) {
        return 1;
    }

    memset(payload, 0, sizeof(payload));
    dm_mc02_wire_put16(payload, 2);
    dm_mc02_wire_put16(payload + 2,
                       DM_MC02_V2_WIRE_SECTION_PAYLOAD_MARKER);
    dm_mc02_wire_put16(payload + 4, 1);
    dm_mc02_wire_put16(payload + 6, 0);
    dm_mc02_wire_put32(payload + 8, 3);
    payload[12] = 1;
    payload[13] = 2;
    payload[14] = 3;
    dm_mc02_wire_put16(payload + 15, 4);
    dm_mc02_wire_put16(payload + 17, 0);
    dm_mc02_wire_put32(payload + 19, 1);
    payload[23] = 9;
    if (expect(dm_mc02_v2_wire_step_payload_begin(payload, 24, &count,
                                                  &marker),
               "section payload header") ||
        expect(count == 2 && marker == DM_MC02_V2_WIRE_SECTION_PAYLOAD_MARKER,
               "section count and marker") ||
        expect(!dm_mc02_v2_wire_is_compact_imu(payload, 24),
               "section payload is not compact IMU")) {
        return 1;
    }
    offset = DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE;
    if (expect(dm_mc02_v2_wire_next_section(payload, 24, &offset, &section),
               "first section") ||
        expect(section.type == 1 && section.flags == 0 &&
                   section.payload_len == 3 && section.payload[0] == 1 &&
                   offset == 15,
               "first section contents") ||
        expect(dm_mc02_v2_wire_next_section(payload, 24, &offset, &section),
               "second section") ||
        expect(section.type == 4 && section.payload_len == 1 &&
                   section.payload[0] == 9 && offset == 24,
               "second section contents") ||
        expect(!dm_mc02_v2_wire_next_section(payload, 24, &offset, &section),
               "section iterator end")) {
        return 1;
    }

    memset(payload, 0, sizeof(payload));
    dm_mc02_wire_put16(payload, 1);
    dm_mc02_wire_put16(payload + 2, 0);
    if (expect(dm_mc02_v2_wire_is_compact_imu(payload, 60),
               "legacy compact IMU discriminator") ||
        expect(!dm_mc02_v2_wire_step_payload_begin(payload, 60, &count,
                                                   &marker),
               "compact IMU is not a section payload")) {
        return 1;
    }
    dm_mc02_wire_put16(payload + 2,
                       DM_MC02_V2_WIRE_SECTION_PAYLOAD_MARKER);
    if (expect(!dm_mc02_v2_wire_is_compact_imu(payload, 60),
               "marked 60-byte payload is section data")) {
        return 1;
    }

    puts("RESULT: shared v2 wire codec smoke passed");
    return 0;
}
