#include "dm_mc02_v2_payload.h"
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
    uint8_t payload[DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE] = { 0 };
    DmMc02V2ResetAckPayload reset = {
        .status = DM_MC02_V2_STATUS_OK,
        .capabilities = DM_MC02_V2_CAP_STEP | DM_MC02_V2_CAP_IMU |
                        DM_MC02_V2_CAP_TELEMETRY,
        .virtual_time_ns = UINT64_C(0x0102030405060708),
        .queue_depth = 9,
    };
    DmMc02V2ResetAckPayload decoded_reset;
    DmMc02V2StepAckPayload step_ack = {
        .status = DM_MC02_V2_STATUS_QUEUE_FULL,
        .queue_depth = 3,
        .dropped_count = 4,
    };
    DmMc02V2StepAckPayload decoded_step_ack;
    DmMc02V2DiagnosticsPayload diagnostics = {
        .rx_frames = 1,
        .rx_bad_frames = 2,
        .rx_dropped_bytes = 3,
        .tx_frames = 4,
        .tx_dropped = 5,
    };
    DmMc02V2DiagnosticsPayload decoded_diagnostics;
    DmMc02V2StepDonePayload done = {
        .status = DM_MC02_V2_STATUS_OK,
        .consumed_mask = DM_MC02_V2_CONSUMED_MASK,
        .missing_mask = 0,
        .queue_depth = 6,
        .dropped_count = 7,
    };
    DmMc02V2StepDonePayload decoded_done;
    DmMc02V2BoardTelemetryPayload telemetry = {
        .led_rgb = 0x10203040,
        .led_brightness = 999,
        .buzzer = 1,
        .board_flags = 0xa5,
        .buzzer_frequency_hz = 4000,
        .buzzer_duty_permille = 500,
    };
    DmMc02V2BoardTelemetryPayload decoded_telemetry;

    if (expect(dm_mc02_v2_payload_encode_reset_ack(
                   payload, sizeof(payload), &reset) ==
                   DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE,
               "RESET_ACK encode") ||
        expect(dm_mc02_v2_payload_decode_reset_ack(
                   &decoded_reset, payload,
                   DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE),
               "RESET_ACK decode") ||
        expect(reset.status == decoded_reset.status &&
                   reset.capabilities == decoded_reset.capabilities &&
                   reset.virtual_time_ns == decoded_reset.virtual_time_ns &&
                   reset.queue_depth == decoded_reset.queue_depth,
               "RESET_ACK round trip") ||
        expect(payload[20] == 0 && payload[21] == 0 && payload[22] == 0 &&
                   payload[23] == 0,
               "RESET_ACK reserved bytes")) {
        return 1;
    }

    if (expect(dm_mc02_v2_payload_encode_step_ack(
                   payload, sizeof(payload), &step_ack) ==
                   DM_MC02_V2_STEP_ACK_PAYLOAD_SIZE,
               "STEP_ACK encode") ||
        expect(dm_mc02_v2_payload_decode_step_ack(
                   &decoded_step_ack, payload,
                   DM_MC02_V2_STEP_ACK_PAYLOAD_SIZE),
               "STEP_ACK decode") ||
        expect(step_ack.status == decoded_step_ack.status &&
                   step_ack.queue_depth == decoded_step_ack.queue_depth &&
                   step_ack.dropped_count == decoded_step_ack.dropped_count,
               "STEP_ACK round trip")) {
        return 1;
    }

    if (expect(dm_mc02_v2_payload_encode_diagnostics(
                   payload, sizeof(payload), &diagnostics) ==
                   DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE,
               "diagnostics encode") ||
        expect(dm_mc02_v2_payload_decode_diagnostics(
                   &decoded_diagnostics, payload,
                   DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE),
               "diagnostics decode") ||
        expect(diagnostics.rx_frames == decoded_diagnostics.rx_frames &&
                   diagnostics.rx_bad_frames == decoded_diagnostics.rx_bad_frames &&
                   diagnostics.rx_dropped_bytes == decoded_diagnostics.rx_dropped_bytes &&
                   diagnostics.tx_frames == decoded_diagnostics.tx_frames &&
                   diagnostics.tx_dropped == decoded_diagnostics.tx_dropped,
               "diagnostics round trip")) {
        return 1;
    }

    if (expect(dm_mc02_v2_payload_encode_step_done(
                   payload, sizeof(payload), &done) ==
                   DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE,
               "STEP_DONE encode") ||
        expect(dm_mc02_v2_payload_decode_step_done(
                   &decoded_done, payload, DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE),
               "STEP_DONE decode") ||
        expect(done.status == decoded_done.status &&
                   done.consumed_mask == decoded_done.consumed_mask &&
                   done.missing_mask == decoded_done.missing_mask &&
                   done.queue_depth == decoded_done.queue_depth &&
                   done.dropped_count == decoded_done.dropped_count,
               "STEP_DONE round trip")) {
        return 1;
    }

    if (expect(dm_mc02_v2_payload_encode_board_telemetry(
                   payload, sizeof(payload), &telemetry) ==
                   DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE,
               "telemetry encode") ||
        expect(dm_mc02_v2_payload_decode_board_telemetry(
                   &decoded_telemetry, payload,
                   DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE),
               "telemetry decode") ||
        expect(telemetry.led_rgb == decoded_telemetry.led_rgb &&
                   telemetry.led_brightness == decoded_telemetry.led_brightness &&
                   telemetry.buzzer == decoded_telemetry.buzzer &&
                   telemetry.board_flags == decoded_telemetry.board_flags &&
                   telemetry.buzzer_frequency_hz == decoded_telemetry.buzzer_frequency_hz &&
                   telemetry.buzzer_duty_permille == decoded_telemetry.buzzer_duty_permille,
               "telemetry round trip")) {
        return 1;
    }

    if (expect(dm_mc02_v2_payload_encode_reset_ack(
                   payload, sizeof(payload), &reset) ==
                   DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE,
               "RESET_ACK re-encode") ) {
        return 1;
    }
    payload[20] = 1;
    if (expect(!dm_mc02_v2_payload_validate_reset_ack(
                   payload, DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE),
               "reject RESET_ACK reserved bytes")) {
        return 1;
    }
    memset(payload, 0, sizeof(payload));
    dm_mc02_wire_put32(payload, DM_MC02_V2_STATUS_OK);
    dm_mc02_wire_put32(payload + 4, DM_MC02_V2_CONSUMED_ACCEL);
    dm_mc02_wire_put32(payload + 8, DM_MC02_V2_CONSUMED_ACCEL);
    if (expect(!dm_mc02_v2_payload_validate_step_done(
                   payload, DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE),
               "reject inconsistent STEP_DONE masks")) {
        return 1;
    }
    puts("RESULT: shared v2 payload codec smoke passed");
    return 0;
}
