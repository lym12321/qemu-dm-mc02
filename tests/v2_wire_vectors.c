#include "dm_mc02_v2_wire.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

static int hex_value(char value)
{
    if (value >= '0' && value <= '9') {
        return value - '0';
    }
    if (value >= 'a' && value <= 'f') {
        return value - 'a' + 10;
    }
    if (value >= 'A' && value <= 'F') {
        return value - 'A' + 10;
    }
    return -1;
}

static bool parse_hex(const char *text, uint8_t *output, size_t capacity,
                      size_t *length)
{
    size_t count = 0;

    while (*text != '\0' && !isspace((unsigned char)*text)) {
        int high = hex_value(*text++);
        int low;

        if (high < 0 || *text == '\0' || isspace((unsigned char)*text) ||
            (low = hex_value(*text++)) < 0 || count == capacity) {
            return false;
        }
        output[count++] = (uint8_t)((high << 4) | low);
    }
    *length = count;
    return count != 0;
}

static int expect(bool condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        return 1;
    }
    return 0;
}

static int verify_vector(const char *name, const uint8_t *wire, size_t length)
{
    DmMc02V2WireFrame decoded = { 0 };
    uint8_t encoded[DM_MC02_V2_WIRE_BODY_PREFIX_SIZE +
                    DM_MC02_V2_WIRE_HEADER_SIZE +
                    DM_MC02_V2_WIRE_MAX_PAYLOAD];
    DmMc02V2WireSection section;
    size_t body_length;
    size_t encoded_length;
    size_t offset;
    uint16_t count;
    uint16_t marker;

    if (expect(length >= 4, "vector has outer length") ||
        expect((body_length = dm_mc02_wire_get32(wire)) + 4 == length,
               "outer length matches vector") ||
        expect(dm_mc02_v2_wire_decode(&decoded, wire + 4, body_length),
               "C decodes golden vector") ||
        expect((encoded_length = dm_mc02_v2_wire_encode(
                    encoded, sizeof(encoded), &decoded)) == body_length,
               "C re-encodes golden vector") ||
        expect(memcmp(encoded, wire + 4, body_length) == 0,
               "C re-encode is byte identical")) {
        return 1;
    }
    if (strcmp(name, "reset") == 0) {
        return expect(decoded.header.kind == DM_MC02_V2_WIRE_RESET &&
                          decoded.header.payload_len == 0 &&
                          decoded.header.session_id == 0,
                      "RESET fields");
    }
    if (strcmp(name, "session_reset") == 0) {
        return expect(decoded.header.kind == DM_MC02_V2_WIRE_RESET &&
                          decoded.header.step_id == 7 &&
                          decoded.header.t_sim_ns == 1000 &&
                          decoded.header.session_id == UINT32_C(0x10203040),
                      "session RESET fields");
    }
    if (strcmp(name, "compact_imu") == 0) {
        return expect(decoded.header.kind == DM_MC02_V2_WIRE_STEP &&
                          decoded.header.payload_len == 60 &&
                          dm_mc02_v2_wire_is_compact_imu(
                              decoded.payload, decoded.header.payload_len),
                      "compact IMU fields");
    }
    if (strcmp(name, "section_imu") == 0) {
        offset = DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE;
        if (expect(decoded.header.payload_len == 72,
                   "section IMU payload length") ||
            expect(dm_mc02_v2_wire_step_payload_begin(
                       decoded.payload, decoded.header.payload_len, &count,
                       &marker),
                   "section IMU header") ||
            expect(count == 1 && marker ==
                                      DM_MC02_V2_WIRE_SECTION_PAYLOAD_MARKER,
                   "section IMU marker") ||
            expect(dm_mc02_v2_wire_next_section(
                       decoded.payload, decoded.header.payload_len, &offset,
                       &section),
                   "section IMU entry") ||
            expect(section.type == 1 && section.payload_len == 60 &&
                       offset == decoded.header.payload_len,
                   "section IMU entry fields")) {
            return 1;
        }
        return 0;
    }
    if (strcmp(name, "mixed") == 0) {
        offset = DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE;
        if (expect(decoded.header.session_id == UINT32_C(0x10203040),
                   "mixed session") ||
            expect(dm_mc02_v2_wire_step_payload_begin(
                       decoded.payload, decoded.header.payload_len, &count,
                       &marker),
                   "mixed section header") ||
            expect(count == 2 && marker ==
                                      DM_MC02_V2_WIRE_SECTION_PAYLOAD_MARKER,
                   "mixed section marker") ||
            expect(dm_mc02_v2_wire_next_section(
                       decoded.payload, decoded.header.payload_len, &offset,
                       &section),
                   "mixed ADC input section") ||
            expect(section.type == 3 && section.payload_len == 8 &&
                       dm_mc02_wire_get16(section.payload) == 4 &&
                       dm_mc02_wire_get16(section.payload + 2) == 0x1234,
                   "mixed ADC input fields") ||
            expect(dm_mc02_v2_wire_next_section(
                       decoded.payload, decoded.header.payload_len, &offset,
                       &section),
                   "mixed ADC voltage section") ||
            expect(section.type == 4 && section.payload_len == 12 &&
                       dm_mc02_wire_get16(section.payload) == 19 &&
                       dm_mc02_wire_get16(section.payload + 2) == 1 &&
                       dm_mc02_wire_get32(section.payload + 4) == 2400000 &&
                       offset == decoded.header.payload_len,
                   "mixed ADC voltage fields")) {
            return 1;
        }
        return 0;
    }
    return expect(false, "known vector name");
}

int main(int argc, char **argv)
{
    FILE *file;
    char line[2048];
    char name[64];
    char hex[1900];
    uint8_t wire[4 + 4 + 36 + 512];
    size_t length;
    unsigned vectors = 0;

    if (argc != 2) {
        fprintf(stderr, "usage: %s VECTOR_FILE\n", argv[0]);
        return 2;
    }
    file = fopen(argv[1], "r");
    if (!file) {
        perror(argv[1]);
        return 2;
    }
    while (fgets(line, sizeof(line), file)) {
        if (line[0] == '#' || line[0] == '\n' || line[0] == '\0') {
            continue;
        }
        if (sscanf(line, "%63s %1899s", name, hex) != 2 ||
            !parse_hex(hex, wire, sizeof(wire), &length) ||
            verify_vector(name, wire, length)) {
            fclose(file);
            return 1;
        }
        vectors++;
    }
    fclose(file);
    if (vectors != 5) {
        fprintf(stderr, "FAIL: expected 5 v2 vectors, got %u\n", vectors);
        return 1;
    }
    printf("RESULT: C/Python v2 wire vector source ready (%u vectors)\n",
           vectors);
    return 0;
}
