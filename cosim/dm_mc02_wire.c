#include "dm_mc02_wire.h"

#include <math.h>
#include <string.h>

uint16_t dm_mc02_wire_get16(const uint8_t *p)
{
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

uint32_t dm_mc02_wire_get32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

uint64_t dm_mc02_wire_get64(const uint8_t *p)
{
    return (uint64_t)dm_mc02_wire_get32(p) |
           ((uint64_t)dm_mc02_wire_get32(p + 4) << 32);
}

void dm_mc02_wire_put16(uint8_t *p, uint16_t value)
{
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
}

void dm_mc02_wire_put32(uint8_t *p, uint32_t value)
{
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
    p[2] = (uint8_t)(value >> 16);
    p[3] = (uint8_t)(value >> 24);
}

void dm_mc02_wire_put64(uint8_t *p, uint64_t value)
{
    dm_mc02_wire_put32(p, (uint32_t)value);
    dm_mc02_wire_put32(p + 4, (uint32_t)(value >> 32));
}

uint32_t dm_mc02_wire_expected_payload_len(uint16_t type)
{
    switch (type) {
    case DM_MC02_WIRE_FRAME_RESET:
    case DM_MC02_WIRE_FRAME_ACK:
        return 0;
    case DM_MC02_WIRE_FRAME_IMU_SAMPLE:
        return 24;
    case DM_MC02_WIRE_FRAME_TELEMETRY:
        return 12;
    case DM_MC02_WIRE_FRAME_ADC_INPUT:
        return 8;
    case DM_MC02_WIRE_FRAME_ADC_VOLTAGE:
        return 12;
    default:
        return UINT32_MAX;
    }
}

bool dm_mc02_wire_known_type(uint16_t type)
{
    return dm_mc02_wire_expected_payload_len(type) != UINT32_MAX;
}

static bool finite_wire_float(const uint8_t *p)
{
    uint32_t bits = dm_mc02_wire_get32(p);

    return (bits & UINT32_C(0x7f800000)) != UINT32_C(0x7f800000);
}

bool dm_mc02_wire_payload_valid(uint16_t type, const uint8_t *payload,
                                size_t payload_len)
{
    uint32_t expected = dm_mc02_wire_expected_payload_len(type);

    if (expected == UINT32_MAX || payload_len != expected ||
        (payload_len && !payload)) {
        return false;
    }
    switch (type) {
    case DM_MC02_WIRE_FRAME_IMU_SAMPLE:
        for (unsigned i = 0; i < 6; ++i) {
            if (!finite_wire_float(payload + i * sizeof(uint32_t))) {
                return false;
            }
        }
        return true;
    case DM_MC02_WIRE_FRAME_TELEMETRY:
        return dm_mc02_wire_get16(payload + 10) == 0;
    case DM_MC02_WIRE_FRAME_ADC_INPUT:
        return dm_mc02_wire_get16(payload) <= DM_MC02_WIRE_ADC_MAX_CHANNEL &&
               dm_mc02_wire_get32(payload + 4) == 0;
    case DM_MC02_WIRE_FRAME_ADC_VOLTAGE:
        return dm_mc02_wire_get16(payload) <= DM_MC02_WIRE_ADC_MAX_CHANNEL &&
               (dm_mc02_wire_get16(payload + 2) &
                ~DM_MC02_WIRE_ADC_VOLTAGE_FLAGS_MASK) == 0 &&
               dm_mc02_wire_get32(payload + 4) <=
                   DM_MC02_WIRE_ADC_VOLTAGE_MAX_UV &&
               dm_mc02_wire_get32(payload + 8) == 0;
    case DM_MC02_WIRE_FRAME_RESET:
    case DM_MC02_WIRE_FRAME_ACK:
        return true;
    default:
        return false;
    }
}

size_t dm_mc02_wire_encode(uint8_t *out, size_t capacity,
                           const DmMc02WireFrame *frame)
{
    size_t total;

    if (!out || !frame || frame->header.version != DM_MC02_WIRE_VERSION ||
        !dm_mc02_wire_known_type(frame->header.type) ||
        frame->header.payload_len !=
            dm_mc02_wire_expected_payload_len(frame->header.type)) {
        return 0;
    }
    total = DM_MC02_WIRE_HEADER_SIZE + frame->header.payload_len;
    if (capacity < total) {
        return 0;
    }
    dm_mc02_wire_put32(out, DM_MC02_WIRE_MAGIC);
    dm_mc02_wire_put16(out + 4, frame->header.version);
    dm_mc02_wire_put16(out + 6, frame->header.type);
    dm_mc02_wire_put32(out + 8, frame->header.payload_len);
    dm_mc02_wire_put64(out + 12, frame->header.sequence);
    dm_mc02_wire_put64(out + 20, frame->header.virtual_time_ns);
    if (frame->header.payload_len) {
        memcpy(out + DM_MC02_WIRE_HEADER_SIZE, frame->payload,
               frame->header.payload_len);
    }
    return total;
}

bool dm_mc02_wire_decode(DmMc02WireFrame *frame, const uint8_t *wire,
                         size_t wire_len)
{
    uint16_t type;
    uint32_t payload_len;

    if (!frame || !wire || wire_len < DM_MC02_WIRE_HEADER_SIZE ||
        dm_mc02_wire_get32(wire) != DM_MC02_WIRE_MAGIC ||
        dm_mc02_wire_get16(wire + 4) != DM_MC02_WIRE_VERSION) {
        return false;
    }
    type = dm_mc02_wire_get16(wire + 6);
    payload_len = dm_mc02_wire_get32(wire + 8);
    if (payload_len > DM_MC02_WIRE_MAX_PAYLOAD ||
        payload_len != dm_mc02_wire_expected_payload_len(type) ||
        wire_len != DM_MC02_WIRE_HEADER_SIZE + payload_len) {
        return false;
    }
    frame->header.version = dm_mc02_wire_get16(wire + 4);
    frame->header.type = type;
    frame->header.payload_len = payload_len;
    frame->header.sequence = dm_mc02_wire_get64(wire + 12);
    frame->header.virtual_time_ns = dm_mc02_wire_get64(wire + 20);
    if (payload_len) {
        memcpy(frame->payload, wire + DM_MC02_WIRE_HEADER_SIZE,
               payload_len);
    }
    return true;
}
