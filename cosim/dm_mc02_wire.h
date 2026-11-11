#ifndef DM_MC02_WIRE_H
#define DM_MC02_WIRE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_MC02_WIRE_MAGIC UINT32_C(0x32434d44)
#define DM_MC02_WIRE_VERSION 1u
#define DM_MC02_WIRE_HEADER_SIZE 28u
#define DM_MC02_WIRE_MAX_PAYLOAD 128u
#define DM_MC02_WIRE_ADC_MAX_CHANNEL 31u
#define DM_MC02_WIRE_ADC_VOLTAGE_MAX_UV 3300000u
#define DM_MC02_WIRE_ADC_VOLTAGE_FLAG_PIN_OVERRIDE 1u
#define DM_MC02_WIRE_ADC_VOLTAGE_FLAGS_MASK \
    DM_MC02_WIRE_ADC_VOLTAGE_FLAG_PIN_OVERRIDE

enum DmMc02WireFrameType {
    DM_MC02_WIRE_FRAME_RESET = 1,
    DM_MC02_WIRE_FRAME_IMU_SAMPLE = 2,
    DM_MC02_WIRE_FRAME_TELEMETRY = 3,
    DM_MC02_WIRE_FRAME_ACK = 4,
    DM_MC02_WIRE_FRAME_ADC_INPUT = 5,
    DM_MC02_WIRE_FRAME_ADC_VOLTAGE = 6,
};

typedef struct DmMc02WireFrameHeader {
    uint16_t version;
    uint16_t type;
    uint32_t payload_len;
    uint64_t sequence;
    uint64_t virtual_time_ns;
} DmMc02WireFrameHeader;

typedef struct DmMc02WireFrame {
    DmMc02WireFrameHeader header;
    uint8_t payload[DM_MC02_WIRE_MAX_PAYLOAD];
} DmMc02WireFrame;

/* Return the exact payload size for a known v1 type, or UINT32_MAX. */
uint32_t dm_mc02_wire_expected_payload_len(uint16_t type);
bool dm_mc02_wire_known_type(uint16_t type);

/* Validate a complete typed payload, including reserved fields and finite
 * IMU values.  The payload pointer is only borrowed for this call. */
bool dm_mc02_wire_payload_valid(uint16_t type, const uint8_t *payload,
                                size_t payload_len);

/* Encode/decode the frame body.  The 4-byte outer stream length is owned by
 * the transport and is intentionally outside this codec. */
size_t dm_mc02_wire_encode(uint8_t *out, size_t capacity,
                           const DmMc02WireFrame *frame);
bool dm_mc02_wire_decode(DmMc02WireFrame *frame, const uint8_t *wire,
                         size_t wire_len);

uint16_t dm_mc02_wire_get16(const uint8_t *p);
uint32_t dm_mc02_wire_get32(const uint8_t *p);
uint64_t dm_mc02_wire_get64(const uint8_t *p);
void dm_mc02_wire_put16(uint8_t *p, uint16_t value);
void dm_mc02_wire_put32(uint8_t *p, uint32_t value);
void dm_mc02_wire_put64(uint8_t *p, uint64_t value);

#endif
