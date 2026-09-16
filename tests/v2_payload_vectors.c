#include "dm_mc02_v2_payload.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>

static int hex_value(char value)
{
    if (value >= '0' && value <= '9') return value - '0';
    if (value >= 'a' && value <= 'f') return value - 'a' + 10;
    if (value >= 'A' && value <= 'F') return value - 'A' + 10;
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

static bool decode_and_reencode(const char *name, const uint8_t *wire,
                                size_t length, uint8_t *encoded)
{
    size_t encoded_length;

    if (strcmp(name, "reset_ack") == 0) {
        DmMc02V2ResetAckPayload value;
        if (!dm_mc02_v2_payload_decode_reset_ack(&value, wire, length)) return false;
        encoded_length = dm_mc02_v2_payload_encode_reset_ack(
            encoded, DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE, &value);
    } else if (strcmp(name, "step_ack") == 0) {
        DmMc02V2StepAckPayload value;
        if (!dm_mc02_v2_payload_decode_step_ack(&value, wire, length)) return false;
        encoded_length = dm_mc02_v2_payload_encode_step_ack(
            encoded, DM_MC02_V2_STEP_ACK_PAYLOAD_SIZE, &value);
    } else if (strcmp(name, "diagnostics") == 0) {
        DmMc02V2DiagnosticsPayload value;
        if (!dm_mc02_v2_payload_decode_diagnostics(&value, wire, length)) return false;
        encoded_length = dm_mc02_v2_payload_encode_diagnostics(
            encoded, DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE, &value);
    } else if (strcmp(name, "step_done") == 0) {
        DmMc02V2StepDonePayload value;
        if (!dm_mc02_v2_payload_decode_step_done(&value, wire, length)) return false;
        encoded_length = dm_mc02_v2_payload_encode_step_done(
            encoded, DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE, &value);
    } else if (strcmp(name, "board_telemetry") == 0) {
        DmMc02V2BoardTelemetryPayload value;
        if (!dm_mc02_v2_payload_decode_board_telemetry(&value, wire, length)) return false;
        encoded_length = dm_mc02_v2_payload_encode_board_telemetry(
            encoded, DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE, &value);
    } else {
        return false;
    }
    return encoded_length == length && memcmp(encoded, wire, length) == 0;
}

static bool valid_vector(const char *name, const uint8_t *wire, size_t length)
{
    if (strcmp(name, "reset_ack") == 0 ||
        strcmp(name, "invalid_reset_ack_reserved") == 0) {
        return dm_mc02_v2_payload_validate_reset_ack(wire, length);
    }
    if (strcmp(name, "step_ack") == 0) {
        return dm_mc02_v2_payload_validate_step_ack(wire, length);
    }
    if (strcmp(name, "diagnostics") == 0) {
        return dm_mc02_v2_payload_validate_diagnostics(wire, length);
    }
    if (strcmp(name, "step_done") == 0 ||
        strcmp(name, "invalid_step_done_mask") == 0) {
        return dm_mc02_v2_payload_validate_step_done(wire, length);
    }
    if (strcmp(name, "board_telemetry") == 0) {
        return dm_mc02_v2_payload_validate_board_telemetry(wire, length);
    }
    return false;
}

int main(int argc, char **argv)
{
    FILE *file;
    char line[2048];
    char name[64];
    char hex[1900];
    uint8_t wire[DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE];
    uint8_t encoded[DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE];
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
        bool invalid;

        if (line[0] == '#' || line[0] == '\n' || line[0] == '\0') continue;
        if (sscanf(line, "%63s %1899s", name, hex) != 2 ||
            !parse_hex(hex, wire, sizeof(wire), &length)) {
            fclose(file);
            return 1;
        }
        invalid = strncmp(name, "invalid_", 8) == 0;
        if (expect(valid_vector(name, wire, length) != invalid,
                   "vector validation result") ||
            (!invalid && expect(decode_and_reencode(name, wire, length, encoded),
                                "C decode/re-encode is byte identical"))) {
            fclose(file);
            return 1;
        }
        vectors++;
    }
    fclose(file);
    if (vectors != 7) {
        fprintf(stderr, "FAIL: expected 7 v2 payload vectors, got %u\n", vectors);
        return 1;
    }
    printf("RESULT: C/Python v2 payload vectors passed (%u vectors)\n", vectors);
    return 0;
}
