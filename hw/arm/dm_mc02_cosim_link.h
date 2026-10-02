/*
 * QEMU-native DM-MC02 co-simulation link.
 *
 * This is deliberately a small, fixed-buffer implementation of the v1/v2
 * wire formats. Chardev streams use the same 4-byte little-endian outer frame
 * length as the host transport, followed by either the 28-byte v1 frame or
 * the v2 magic/header. It does not include the host-side transport.
 * The link is one client in one QEMU process and has no snapshot or
 * migration semantics. The machine supplies a lightweight telemetry provider
 * for board-output changes; this is not a full peripheral waveform model.
 */
#ifndef HW_ARM_DM_MC02_COSIM_LINK_H
#define HW_ARM_DM_MC02_COSIM_LINK_H

#include "chardev/char-fe.h"
#include "qemu/timer.h"
#include "../../../../cosim/dm_mc02_v2_wire.h"
#include "../../../../cosim/dm_mc02_v2_payload.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_COSIM_TX_QUEUE_SIZE 8
/* 4-byte outer length + v2 magic/header + a bounded control payload. */
#define DM_MC02_COSIM_TX_FRAME_SIZE \
    (4u + DM_MC02_V2_WIRE_BODY_PREFIX_SIZE + DM_MC02_V2_WIRE_HEADER_SIZE + \
     DM_MC02_V2_WIRE_MAX_PAYLOAD)
#define DM_MC02_COSIM_IMU_QUEUE_SIZE 64
#define DM_MC02_COSIM_ADC_QUEUE_SIZE 64
#define DM_MC02_COSIM_V2_RETRY_PAYLOAD_SIZE DM_MC02_V2_WIRE_MAX_PAYLOAD
#define DM_MC02_COSIM_V2_MOTOR_STATE_PAYLOAD_SIZE DM_MC02_V2_WIRE_MAX_PAYLOAD

typedef void DmMc02CosimImuHandler(void *opaque, const float gyro[3],
                                   const float accel[3],
                                   uint64_t sample_sequence,
                                   uint64_t step_id,
                                   uint64_t sensor_time_ns);

/* ADC_INPUT payload is: channel:u16, raw:u16, reserved:u32. */
#define DM_MC02_COSIM_FRAME_ADC_INPUT 5u
#define DM_MC02_COSIM_ADC_INPUT_PAYLOAD_SIZE DM_MC02_V2_ADC_INPUT_PAYLOAD_SIZE

/* ADC_PIN_VOLTAGE payload is: channel:u16, flags:u16, voltage_uv:u32,
 * reserved:u32.  A zero flags value is accepted for simple sources;
 * PIN_OVERRIDE is an explicit external-override annotation. */
#define DM_MC02_COSIM_FRAME_ADC_PIN_VOLTAGE 6u
#define DM_MC02_COSIM_ADC_PIN_VOLTAGE_PAYLOAD_SIZE DM_MC02_V2_ADC_VOLTAGE_PAYLOAD_SIZE
#define DM_MC02_COSIM_ADC_PIN_VOLTAGE_FLAG_PIN_OVERRIDE \
    DM_MC02_V2_ADC_VOLTAGE_FLAG_PIN_OVERRIDE
#define DM_MC02_COSIM_ADC_PIN_VOLTAGE_MAX_UV DM_MC02_V2_ADC_VOLTAGE_MAX_UV

typedef void DmMc02CosimAdcInputHandler(void *opaque, uint16_t channel,
                                        uint16_t raw);
typedef void DmMc02CosimAdcPinVoltageHandler(void *opaque, uint16_t channel,
                                             uint16_t flags,
                                             uint32_t voltage_uv);

/* The callback receives one validated MotorCommand payload from a v2 STEP.
 * It may synchronously return a canonical MotorState payload.  Returning
 * false applies the same bounded retry/backpressure result as another input
 * queue that cannot accept this step.  The link owns neither command nor
 * state semantics and never depends on a plant implementation. A false return
 * means the command was not committed and may be called again on retry; the
 * callback must not publish a committed side effect before returning false.
 * A true return is a commit boundary, so the state payload must already be a
 * valid canonical payload; an invalid state is reported as PROTOCOL and is
 * never retried as QUEUE_FULL. */
typedef bool DmMc02CosimMotorStepHandler(
    void *opaque, uint64_t step_id, uint64_t t_sim_ns, uint64_t dt_ns,
    const uint8_t *command_payload, size_t command_payload_len,
    uint8_t *state_payload, size_t state_payload_capacity,
    size_t *state_payload_len);

typedef struct DmMc02CosimTelemetry {
    uint32_t led_rgb;
    uint32_t led_brightness;
    bool buzzer;
    uint8_t board_flags;
    uint32_t buzzer_frequency_hz;
    uint32_t buzzer_duty_permille;
} DmMc02CosimTelemetry;

typedef void DmMc02CosimTelemetryProvider(void *opaque,
                                          DmMc02CosimTelemetry *telemetry);

typedef struct DmMc02CosimImuSample {
    float gyro[3];
    float accel[3];
    uint64_t sequence;
    uint64_t step_id;
    uint64_t sensor_time_ns;
    uint64_t due_qemu_ns;
    uint64_t step_t_sim_ns;
    uint64_t step_dt_ns;
} DmMc02CosimImuSample;

typedef struct DmMc02CosimConsumeToken {
    uint64_t step_id;
    uint64_t t_sim_ns;
    uint64_t dt_ns;
    uint32_t consumed_mask;
} DmMc02CosimConsumeToken;

typedef struct DmMc02CosimAdcSample {
    uint16_t channel;
    uint16_t flags;
    uint32_t value;
    uint64_t due_qemu_ns;
    bool voltage;
} DmMc02CosimAdcSample;

typedef struct DmMc02CosimLink {
    CharBackend chr;
    DmMc02CosimImuHandler *imu_handler;
    DmMc02CosimAdcInputHandler *adc_input_handler;
    DmMc02CosimAdcPinVoltageHandler *adc_pin_voltage_handler;
    DmMc02CosimMotorStepHandler *motor_step_handler;
    DmMc02CosimTelemetryProvider *telemetry_provider;
    void *opaque;
    uint8_t rx_buf[4096];
    size_t rx_head;
    size_t rx_len;
    bool rx_initialized;
    uint16_t rx_protocol_version;
    /* Protocol selected by the most recent completed peer session.  The
     * active validator is reset on disconnect, but reconnect handshakes must
     * still use the same wire version before the peer can send its RESET. */
    uint16_t rx_last_protocol_version;
    uint64_t rx_last_sequence;
    uint64_t rx_last_virtual_time_ns;
    bool v2_initialized;
    /* v2 uses the existing header flags word as an optional session id.
     * Zero keeps compatibility with pre-session-ID v2 peers. */
    uint32_t v2_session_id;
    uint32_t v2_next_session_id;
    uint64_t v2_last_step_id;
    uint64_t v2_last_t_sim_ns;
    uint64_t v2_last_step_dt_ns;
    uint32_t v2_last_step_status;
    uint32_t v2_last_step_payload_len;
    bool v2_last_step_valid;
    uint8_t v2_last_step_payload[DM_MC02_COSIM_V2_RETRY_PAYLOAD_SIZE];
    uint32_t v2_last_motor_state_len;
    uint8_t v2_last_motor_state_payload[
        DM_MC02_COSIM_V2_MOTOR_STATE_PAYLOAD_SIZE];
    bool v2_last_step_done_valid;
    uint64_t v2_last_step_done_id;
    uint64_t v2_last_step_done_t_sim_ns;
    uint64_t v2_last_step_done_dt_ns;
    uint32_t v2_last_step_done_consumed_mask;
    bool v2_retry_pending;
    uint64_t v2_retry_step_id;
    uint64_t v2_retry_t_sim_ns;
    uint64_t v2_retry_dt_ns;
    uint32_t v2_retry_payload_len;
    uint8_t v2_retry_payload[DM_MC02_COSIM_V2_RETRY_PAYLOAD_SIZE];
    bool v2_retry_motor_state_valid;
    uint32_t v2_retry_motor_state_len;
    uint8_t v2_retry_motor_state_payload[
        DM_MC02_COSIM_V2_MOTOR_STATE_PAYLOAD_SIZE];
    uint64_t tx_sequence;
    uint64_t tx_last_virtual_time_ns;
    bool tx_time_valid;
    bool tx_session_started;
    bool opened;
    bool enabled;

    /* Diagnostic counters are intentionally monotonic and non-fatal. */
    uint64_t rx_frames;
    uint64_t rx_bad_frames;
    uint64_t rx_dropped_bytes;
    uint64_t tx_frames;
    uint64_t tx_dropped;
    uint64_t tx_short_writes;
    uint64_t imu_dropped;
    uint64_t consume_dropped;
    uint64_t adc_dropped;
    uint64_t imu_time_base_qemu_ns;
    uint64_t imu_time_base_host_ns;
    bool imu_time_base_valid;
    QEMUTimer *imu_timer;
    DmMc02CosimImuSample imu_queue[DM_MC02_COSIM_IMU_QUEUE_SIZE];
    uint8_t imu_queue_head;
    uint8_t imu_queue_count;
    DmMc02CosimAdcSample adc_queue[DM_MC02_COSIM_ADC_QUEUE_SIZE];
    uint8_t adc_queue_head;
    uint8_t adc_queue_count;
    DmMc02CosimConsumeToken consume_queue[DM_MC02_COSIM_IMU_QUEUE_SIZE];
    uint8_t consume_count;
    bool imu_delivery_blocked;
    DmMc02CosimTelemetry last_telemetry;
    bool telemetry_valid;
    bool telemetry_pending;
    uint8_t tx_queue[DM_MC02_COSIM_TX_QUEUE_SIZE][DM_MC02_COSIM_TX_FRAME_SIZE];
    uint16_t tx_queue_len[DM_MC02_COSIM_TX_QUEUE_SIZE];
    uint16_t tx_queue_offset[DM_MC02_COSIM_TX_QUEUE_SIZE];
    bool tx_queue_control[DM_MC02_COSIM_TX_QUEUE_SIZE];
    uint8_t tx_queue_head;
    uint8_t tx_queue_count;
    QEMUTimer *tx_timer;
} DmMc02CosimLink;

bool dm_mc02_cosim_link_init(DmMc02CosimLink *link, Chardev *chardev,
                             DmMc02CosimImuHandler *imu_handler,
                             DmMc02CosimTelemetryProvider *telemetry_provider,
                             void *opaque, Error **errp);
void dm_mc02_cosim_link_notify_telemetry(DmMc02CosimLink *link);

/* Install the ADC1 sink after link initialization.  Keeping this setter
 * separate preserves the existing init ABI for native users. */
void dm_mc02_cosim_link_set_adc_handler(DmMc02CosimLink *link,
                                        DmMc02CosimAdcInputHandler *handler);
void dm_mc02_cosim_link_set_adc_pin_voltage_handler(
    DmMc02CosimLink *link,
    DmMc02CosimAdcPinVoltageHandler *handler);
/* Install an optional v2 MotorCommand/MotorState endpoint.  The endpoint is
 * intentionally independent from the board machine and may be backed by an
 * external plant adapter or a future board-local motor model. */
void dm_mc02_cosim_link_set_motor_step_handler(
    DmMc02CosimLink *link, DmMc02CosimMotorStepHandler *handler);
/* Notify the link that a complete raw accel/gyro data path was consumed by
 * the guest.  bit 0 is accel and bit 1 is gyro. */
void dm_mc02_cosim_link_notify_imu_consumed(DmMc02CosimLink *link,
                                            uint64_t step_id,
                                            uint32_t consumed_mask);
void dm_mc02_cosim_link_cleanup(DmMc02CosimLink *link);
void dm_mc02_cosim_link_reset(DmMc02CosimLink *link);

#endif
