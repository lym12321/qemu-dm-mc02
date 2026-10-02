#ifndef DM_MC02_V2_WIRE_H
#define DM_MC02_V2_WIRE_H

#include "dm_mc02_wire.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_MC02_V2_WIRE_VERSION 2u
#define DM_MC02_V2_WIRE_HEADER_SIZE 36u
#define DM_MC02_V2_WIRE_MAX_PAYLOAD 512u
#define DM_MC02_V2_WIRE_BODY_PREFIX_SIZE 4u
#define DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE 4u
#define DM_MC02_V2_WIRE_SECTION_HEADER_SIZE 8u
#define DM_MC02_V2_WIRE_SECTION_PAYLOAD_MARKER UINT16_C(0x5343)
#define DM_MC02_V2_WIRE_IMU_PAYLOAD_SIZE 60u

enum DmMc02V2WireKind {
    DM_MC02_V2_WIRE_RESET = 1,
    DM_MC02_V2_WIRE_STEP = 2,
    DM_MC02_V2_WIRE_STEP_ACK = 3,
    DM_MC02_V2_WIRE_DIAGNOSTICS = 4,
    DM_MC02_V2_WIRE_RESET_ACK = 5,
    DM_MC02_V2_WIRE_STEP_DONE = 6,
    DM_MC02_V2_WIRE_TELEMETRY = 7,
    DM_MC02_V2_WIRE_MOTOR_STATE = 8,
};

enum DmMc02V2WireSectionType {
    DM_MC02_V2_WIRE_SECTION_IMU_SAMPLE = 1,
    DM_MC02_V2_WIRE_SECTION_MOTOR_COMMAND = 2,
    DM_MC02_V2_WIRE_SECTION_ADC_INPUT = 3,
    DM_MC02_V2_WIRE_SECTION_ADC_VOLTAGE = 4,
    DM_MC02_V2_WIRE_SECTION_MOTOR_STATE = 5,
};

typedef struct DmMc02V2WireFrameHeader {
    uint16_t version;
    uint16_t kind;
    uint32_t payload_len;
    uint64_t step_id;
    uint64_t t_sim_ns;
    uint64_t dt_ns;
    uint32_t session_id;
} DmMc02V2WireFrameHeader;

typedef struct DmMc02V2WireFrame {
    DmMc02V2WireFrameHeader header;
    uint8_t payload[DM_MC02_V2_WIRE_MAX_PAYLOAD];
} DmMc02V2WireFrame;

typedef struct DmMc02V2WireSection {
    uint16_t type;
    uint16_t flags;
    uint32_t payload_len;
    const uint8_t *payload;
} DmMc02V2WireSection;

bool dm_mc02_v2_wire_known_kind(uint16_t kind);

/* Encode/decode the v2 body: magic + 36-byte header + payload. The outer
 * stream length prefix remains owned by the transport, matching v1. */
size_t dm_mc02_v2_wire_encode(uint8_t *out, size_t capacity,
                              const DmMc02V2WireFrame *frame);
bool dm_mc02_v2_wire_decode(DmMc02V2WireFrame *frame, const uint8_t *wire,
                            size_t wire_len);

/* Validate the structural section header and initialize an iteration cursor.
 * Marker zero is retained for old section encoders; the explicit marker
 * disambiguates a section payload whose total size is 60 bytes from compact
 * ImuSampleV2. */
bool dm_mc02_v2_wire_step_payload_begin(const uint8_t *payload,
                                        size_t payload_len,
                                        uint16_t *section_count,
                                        uint16_t *marker);

/* Return one borrowed section and advance *offset. Start with offset equal to
 * DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE. */
bool dm_mc02_v2_wire_next_section(const uint8_t *payload, size_t payload_len,
                                  size_t *offset,
                                  DmMc02V2WireSection *section);

bool dm_mc02_v2_wire_is_compact_imu(const uint8_t *payload,
                                    size_t payload_len);

#endif
