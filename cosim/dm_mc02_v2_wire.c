#include "dm_mc02_v2_wire.h"

#include <string.h>

bool dm_mc02_v2_wire_known_kind(uint16_t kind)
{
    return kind >= DM_MC02_V2_WIRE_RESET &&
           kind <= DM_MC02_V2_WIRE_MOTOR_STATE;
}

size_t dm_mc02_v2_wire_encode(uint8_t *out, size_t capacity,
                              const DmMc02V2WireFrame *frame)
{
    size_t total;

    if (!out || !frame || frame->header.version != DM_MC02_V2_WIRE_VERSION ||
        !dm_mc02_v2_wire_known_kind(frame->header.kind) ||
        frame->header.payload_len > DM_MC02_V2_WIRE_MAX_PAYLOAD) {
        return 0;
    }
    total = DM_MC02_V2_WIRE_BODY_PREFIX_SIZE +
            DM_MC02_V2_WIRE_HEADER_SIZE + frame->header.payload_len;
    if (capacity < total) {
        return 0;
    }
    dm_mc02_wire_put32(out, DM_MC02_WIRE_MAGIC);
    dm_mc02_wire_put16(out + 4, frame->header.version);
    dm_mc02_wire_put16(out + 6, frame->header.kind);
    dm_mc02_wire_put32(out + 8, frame->header.payload_len);
    dm_mc02_wire_put64(out + 12, frame->header.step_id);
    dm_mc02_wire_put64(out + 20, frame->header.t_sim_ns);
    dm_mc02_wire_put64(out + 28, frame->header.dt_ns);
    dm_mc02_wire_put32(out + 36, frame->header.session_id);
    if (frame->header.payload_len) {
        memcpy(out + DM_MC02_V2_WIRE_BODY_PREFIX_SIZE +
                   DM_MC02_V2_WIRE_HEADER_SIZE,
               frame->payload, frame->header.payload_len);
    }
    return total;
}

bool dm_mc02_v2_wire_decode(DmMc02V2WireFrame *frame, const uint8_t *wire,
                            size_t wire_len)
{
    uint16_t kind;
    uint32_t payload_len;

    if (!frame || !wire ||
        wire_len < DM_MC02_V2_WIRE_BODY_PREFIX_SIZE +
                       DM_MC02_V2_WIRE_HEADER_SIZE ||
        wire_len > DM_MC02_V2_WIRE_BODY_PREFIX_SIZE +
                       DM_MC02_V2_WIRE_HEADER_SIZE +
                       DM_MC02_V2_WIRE_MAX_PAYLOAD ||
        dm_mc02_wire_get32(wire) != DM_MC02_WIRE_MAGIC ||
        dm_mc02_wire_get16(wire + 4) != DM_MC02_V2_WIRE_VERSION) {
        return false;
    }
    kind = dm_mc02_wire_get16(wire + 6);
    payload_len = dm_mc02_wire_get32(wire + 8);
    if (!dm_mc02_v2_wire_known_kind(kind) ||
        payload_len > DM_MC02_V2_WIRE_MAX_PAYLOAD ||
        wire_len != DM_MC02_V2_WIRE_BODY_PREFIX_SIZE +
                        DM_MC02_V2_WIRE_HEADER_SIZE + payload_len) {
        return false;
    }
    frame->header.version = dm_mc02_wire_get16(wire + 4);
    frame->header.kind = kind;
    frame->header.payload_len = payload_len;
    frame->header.step_id = dm_mc02_wire_get64(wire + 12);
    frame->header.t_sim_ns = dm_mc02_wire_get64(wire + 20);
    frame->header.dt_ns = dm_mc02_wire_get64(wire + 28);
    frame->header.session_id = dm_mc02_wire_get32(wire + 36);
    if (payload_len) {
        memcpy(frame->payload,
               wire + DM_MC02_V2_WIRE_BODY_PREFIX_SIZE +
                   DM_MC02_V2_WIRE_HEADER_SIZE,
               payload_len);
    }
    return true;
}

bool dm_mc02_v2_wire_step_payload_begin(const uint8_t *payload,
                                        size_t payload_len,
                                        uint16_t *section_count,
                                        uint16_t *marker)
{
    uint16_t count;
    uint16_t value;

    if (!payload || !section_count || !marker ||
        payload_len < DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE ||
        payload_len > DM_MC02_V2_WIRE_MAX_PAYLOAD ||
        dm_mc02_v2_wire_is_compact_imu(payload, payload_len)) {
        return false;
    }
    count = dm_mc02_wire_get16(payload);
    value = dm_mc02_wire_get16(payload + 2);
    if (count == 0 || (value != 0 &&
                       value != DM_MC02_V2_WIRE_SECTION_PAYLOAD_MARKER)) {
        return false;
    }
    *section_count = count;
    *marker = value;
    return true;
}

bool dm_mc02_v2_wire_next_section(const uint8_t *payload, size_t payload_len,
                                  size_t *offset,
                                  DmMc02V2WireSection *section)
{
    size_t cursor;
    uint32_t section_len;

    if (!payload || !offset || !section ||
        *offset < DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE ||
        *offset > payload_len ||
        payload_len - *offset < DM_MC02_V2_WIRE_SECTION_HEADER_SIZE) {
        return false;
    }
    cursor = *offset;
    section->type = dm_mc02_wire_get16(payload + cursor);
    section->flags = dm_mc02_wire_get16(payload + cursor + 2);
    section_len = dm_mc02_wire_get32(payload + cursor + 4);
    cursor += DM_MC02_V2_WIRE_SECTION_HEADER_SIZE;
    if (section->type == 0 || section->flags != 0 ||
        section_len > payload_len - cursor) {
        return false;
    }
    section->payload_len = section_len;
    section->payload = payload + cursor;
    *offset = cursor + section_len;
    return true;
}

bool dm_mc02_v2_wire_is_compact_imu(const uint8_t *payload,
                                    size_t payload_len)
{
    return payload && payload_len == DM_MC02_V2_WIRE_IMU_PAYLOAD_SIZE &&
           dm_mc02_wire_get16(payload + 2) !=
               DM_MC02_V2_WIRE_SECTION_PAYLOAD_MARKER;
}
