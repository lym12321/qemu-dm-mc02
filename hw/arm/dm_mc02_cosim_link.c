/*
 * DM-MC02 v1 wire parser/encoder on a QEMU CharBackend.
 *
 * Chardev framing matches dm_mc02_transport: a 4-byte little-endian outer
 * frame length in [28, 156], followed by exactly that many protocol bytes.
 * The parser runs only from the chardev callback and uses a bounded receive
 * buffer.  TX uses a bounded non-blocking frame queue: slow consumers cause
 * counted whole-frame telemetry drops and never stall the guest.
 * This link is intentionally single-client, single-process, and has no
 * snapshot/migration state semantics.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/timer.h"
#include "hw/arm/dm_mc02_adc.h"
#include "hw/arm/dm_mc02_cosim_link.h"
#include "../../../../cosim/dm_mc02_wire.h"
#include "../../../../cosim/dm_mc02_v2_payload.h"
#include "../../../../cosim/dm_mc02_v2_wire.h"

#include <math.h>

#define DM_MC02_OUTER_SIZE       4u
#define DM_MC02_IMU_PAYLOAD_SIZE 24u
#define DM_MC02_TLM_PAYLOAD_SIZE 12u
#define DM_MC02_COSIM_TX_RETRY_NS UINT64_C(1000000)
#define DM_MC02_ADC_INPUT_PAYLOAD_SIZE \
    DM_MC02_COSIM_ADC_INPUT_PAYLOAD_SIZE
#define DM_MC02_ADC_PIN_VOLTAGE_PAYLOAD_SIZE \
    DM_MC02_COSIM_ADC_PIN_VOLTAGE_PAYLOAD_SIZE

enum {
    DM_MC02_FRAME_RESET = DM_MC02_WIRE_FRAME_RESET,
    DM_MC02_FRAME_IMU_SAMPLE = DM_MC02_WIRE_FRAME_IMU_SAMPLE,
    DM_MC02_FRAME_TELEMETRY = DM_MC02_WIRE_FRAME_TELEMETRY,
    DM_MC02_FRAME_ACK = DM_MC02_WIRE_FRAME_ACK,
    DM_MC02_FRAME_ADC_INPUT = DM_MC02_WIRE_FRAME_ADC_INPUT,
    DM_MC02_FRAME_ADC_PIN_VOLTAGE = DM_MC02_WIRE_FRAME_ADC_VOLTAGE,
};

#define DM_MC02_COSIM_MAX_FRAME_BODY \
    (DM_MC02_V2_WIRE_HEADER_SIZE + DM_MC02_V2_WIRE_BODY_PREFIX_SIZE + \
     DM_MC02_V2_WIRE_MAX_PAYLOAD)

static uint16_t get16le(const uint8_t *p)
{
    return dm_mc02_wire_get16(p);
}

static uint32_t get32le(const uint8_t *p)
{
    return dm_mc02_wire_get32(p);
}

static uint64_t get64le(const uint8_t *p)
{
    return dm_mc02_wire_get64(p);
}

static void put32le(uint8_t *p, uint32_t value)
{
    dm_mc02_wire_put32(p, value);
}

static uint32_t next_v2_session_id(DmMc02CosimLink *link)
{
    uint32_t session_id = ++link->v2_next_session_id;

    /* Zero is reserved for the legacy v2 mode. */
    if (session_id == 0) {
        session_id = ++link->v2_next_session_id;
    }
    return session_id;
}

static float get_float_le(const uint8_t *p)
{
    uint32_t bits = get32le(p);
    float value;

    memcpy(&value, &bits, sizeof(value));
    return value;
}

static unsigned expected_payload_size(uint16_t type)
{
    uint32_t size = dm_mc02_wire_expected_payload_len(type);

    return size == UINT32_MAX ? UINT_MAX : size;
}

static bool known_type(uint16_t type)
{
    return dm_mc02_wire_known_type(type);
}

static void rx_reset(DmMc02CosimLink *link)
{
    link->rx_head = 0;
    link->rx_len = 0;
    link->rx_initialized = false;
    link->rx_protocol_version = 0;
    link->rx_last_sequence = 0;
    link->rx_last_virtual_time_ns = 0;
    link->v2_initialized = false;
    link->v2_session_id = 0;
    link->v2_last_step_id = 0;
    link->v2_last_t_sim_ns = 0;
    link->v2_last_step_dt_ns = 0;
    link->v2_last_step_status = 0;
    link->v2_last_step_payload_len = 0;
    link->v2_last_step_valid = false;
    link->v2_last_motor_state_len = 0;
    link->v2_last_step_done_valid = false;
    link->v2_last_step_done_id = 0;
    link->v2_last_step_done_t_sim_ns = 0;
    link->v2_last_step_done_dt_ns = 0;
    link->v2_last_step_done_consumed_mask = 0;
    link->v2_retry_pending = false;
    link->v2_retry_step_id = 0;
    link->v2_retry_t_sim_ns = 0;
    link->v2_retry_dt_ns = 0;
    link->v2_retry_payload_len = 0;
    link->v2_retry_motor_state_valid = false;
    link->v2_retry_motor_state_len = 0;
}

static void rx_validator_reset(DmMc02CosimLink *link)
{
    link->rx_initialized = false;
    link->rx_last_sequence = 0;
    link->rx_last_virtual_time_ns = 0;
}

static void v2_validator_reset(DmMc02CosimLink *link)
{
    link->v2_initialized = false;
    link->v2_last_step_id = 0;
    link->v2_last_t_sim_ns = 0;
    link->v2_last_step_dt_ns = 0;
    link->v2_last_step_status = 0;
    link->v2_last_step_payload_len = 0;
    link->v2_last_step_valid = false;
    link->v2_last_motor_state_len = 0;
    link->v2_last_step_done_valid = false;
    link->v2_last_step_done_id = 0;
    link->v2_last_step_done_t_sim_ns = 0;
    link->v2_last_step_done_dt_ns = 0;
    link->v2_last_step_done_consumed_mask = 0;
    link->v2_retry_pending = false;
    link->v2_retry_step_id = 0;
    link->v2_retry_t_sim_ns = 0;
    link->v2_retry_dt_ns = 0;
    link->v2_retry_payload_len = 0;
    link->v2_retry_motor_state_valid = false;
    link->v2_retry_motor_state_len = 0;
}

static void rx_drop_prefix(DmMc02CosimLink *link, size_t count, bool bad)
{
    size_t consumed = MIN(count, link->rx_len);

    if (consumed >= link->rx_len) {
        if (bad) {
            link->rx_dropped_bytes += link->rx_len;
        }
        link->rx_head = 0;
        link->rx_len = 0;
    } else {
        link->rx_head += consumed;
        link->rx_len -= consumed;
        if (bad) {
            link->rx_dropped_bytes += consumed;
        }
    }
    if (bad) {
        link->rx_bad_frames++;
    }
}

static bool validate_frame(DmMc02CosimLink *link, uint16_t type,
                           uint32_t payload_len, uint64_t sequence,
                           uint64_t virtual_time_ns, const uint8_t *payload)
{
    unsigned expected = expected_payload_size(type);

    if (!known_type(type) || payload_len != expected ||
        !dm_mc02_wire_payload_valid(type, payload, payload_len)) {
        return false;
    }
    if (type == DM_MC02_FRAME_RESET) {
        /* A chardev session chooses its protocol on the first RESET.  Do not
         * let a legacy RESET downgrade an active v2 session: the peer would
         * then continue sending v2 frames that the validator silently drops. */
        if (link->rx_protocol_version == DM_MC02_V2_WIRE_VERSION) {
            return false;
        }
        /* RESET starts a new RX session and establishes its ordering baseline. */
        rx_validator_reset(link);
        link->rx_protocol_version = DM_MC02_WIRE_VERSION;
        link->rx_last_protocol_version = DM_MC02_WIRE_VERSION;
        link->rx_initialized = true;
        link->rx_last_sequence = sequence;
        link->rx_last_virtual_time_ns = virtual_time_ns;
        return true;
    }
    if (link->rx_protocol_version == DM_MC02_V2_WIRE_VERSION) {
        return false;
    }
    if (link->rx_initialized &&
        (sequence <= link->rx_last_sequence ||
         virtual_time_ns <= link->rx_last_virtual_time_ns)) {
        return false;
    }
    link->rx_initialized = true;
    link->rx_protocol_version = DM_MC02_WIRE_VERSION;
    link->rx_last_sequence = sequence;
    link->rx_last_virtual_time_ns = virtual_time_ns;
    return true;
}

static void imu_schedule_clear(DmMc02CosimLink *link)
{
    link->imu_queue_head = 0;
    link->imu_queue_count = 0;
    link->adc_queue_head = 0;
    link->adc_queue_count = 0;
    link->consume_count = 0;
    link->imu_delivery_blocked = false;
    link->imu_time_base_valid = false;
    if (link->imu_timer) {
        timer_del(link->imu_timer);
    }
}

static void imu_schedule_session(DmMc02CosimLink *link,
                                 uint64_t host_time_ns)
{
    link->imu_queue_head = 0;
    link->imu_queue_count = 0;
    link->adc_queue_head = 0;
    link->adc_queue_count = 0;
    link->consume_count = 0;
    link->imu_delivery_blocked = false;
    link->imu_time_base_host_ns = host_time_ns;
    link->imu_time_base_qemu_ns =
        qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    link->imu_time_base_valid = true;
    if (link->imu_timer) {
        timer_del(link->imu_timer);
    }
}

static bool cosim_arm_imu_consumption(DmMc02CosimLink *link,
                                      uint64_t step_id,
                                      uint64_t t_sim_ns, uint64_t dt_ns)
{
    DmMc02CosimConsumeToken *token;

    if (!link->v2_initialized ||
        link->consume_count == DM_MC02_COSIM_IMU_QUEUE_SIZE) {
        if (link->v2_initialized &&
            link->consume_count == DM_MC02_COSIM_IMU_QUEUE_SIZE) {
            link->consume_dropped++;
        }
        return false;
    }
    token = &link->consume_queue[link->consume_count++];
    token->step_id = step_id;
    token->t_sim_ns = t_sim_ns;
    token->dt_ns = dt_ns;
    token->consumed_mask = 0;
    return true;
}

static void cosim_remove_consume_token(DmMc02CosimLink *link, unsigned index)
{
    if (index + 1 < link->consume_count) {
        memmove(&link->consume_queue[index],
                &link->consume_queue[index + 1],
                (link->consume_count - index - 1) *
                sizeof(link->consume_queue[0]));
    }
    link->consume_count--;
}

static void cosim_deliver_due(DmMc02CosimLink *link)
{
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    while (link->imu_queue_count || link->adc_queue_count) {
        DmMc02CosimImuSample *imu = link->imu_queue_count ?
            &link->imu_queue[link->imu_queue_head] : NULL;
        DmMc02CosimAdcSample *adc = link->adc_queue_count ?
            &link->adc_queue[link->adc_queue_head] : NULL;
        bool deliver_adc = adc && (!imu || adc->due_qemu_ns < imu->due_qemu_ns);
        uint64_t due_qemu_ns = deliver_adc ? adc->due_qemu_ns :
                               imu->due_qemu_ns;

        if (due_qemu_ns > now) {
            break;
        }
        if (deliver_adc) {
            if (adc->voltage) {
                if (link->adc_pin_voltage_handler) {
                    link->adc_pin_voltage_handler(link->opaque, adc->channel,
                                                  adc->flags, adc->value);
                }
            } else if (link->adc_input_handler) {
                link->adc_input_handler(link->opaque, adc->channel,
                                        (uint16_t)adc->value);
            }
            link->adc_queue_head = (link->adc_queue_head + 1) %
                                   DM_MC02_COSIM_ADC_QUEUE_SIZE;
            link->adc_queue_count--;
        } else {
            if (link->v2_initialized && link->imu_handler &&
                !cosim_arm_imu_consumption(link, imu->step_id,
                                            imu->step_t_sim_ns,
                                            imu->step_dt_ns)) {
                /* Keep the sample at the queue head until the guest releases
                 * a consumption token.  Do not re-arm the timer at the same
                 * deadline while the queue is blocked. */
                link->imu_delivery_blocked = true;
                break;
            }
            if (link->imu_handler) {
                link->imu_handler(link->opaque, imu->gyro, imu->accel,
                                  imu->sequence, imu->step_id,
                                  imu->sensor_time_ns);
            }
            link->imu_queue_head = (link->imu_queue_head + 1) %
                                   DM_MC02_COSIM_IMU_QUEUE_SIZE;
            link->imu_queue_count--;
        }
    }
    if (link->imu_timer && link->imu_delivery_blocked) {
        timer_del(link->imu_timer);
    } else if (link->imu_timer && (link->imu_queue_count ||
                                   link->adc_queue_count)) {
        uint64_t imu_due = link->imu_queue_count ?
            link->imu_queue[link->imu_queue_head].due_qemu_ns : UINT64_MAX;
        uint64_t adc_due = link->adc_queue_count ?
            link->adc_queue[link->adc_queue_head].due_qemu_ns : UINT64_MAX;

        timer_mod(link->imu_timer, MIN(imu_due, adc_due));
    }
}

static uint64_t cosim_due_qemu_ns(const DmMc02CosimLink *link,
                                  uint64_t host_time_ns)
{
    uint64_t delta;

    if (host_time_ns < link->imu_time_base_host_ns) {
        /* A validated v2 session cannot normally take this branch.  Treat a
         * late legacy sample as immediately due instead of allowing unsigned
         * subtraction to turn it into a far-future timestamp. */
        return qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    }
    delta = host_time_ns - link->imu_time_base_host_ns;
    if (delta > UINT64_MAX - link->imu_time_base_qemu_ns) {
        return UINT64_MAX;
    }
    return link->imu_time_base_qemu_ns + delta;
}

static void imu_timer_cb(void *opaque)
{
    cosim_deliver_due(opaque);
}

static bool cosim_queue_tx(DmMc02CosimLink *link, const uint8_t *wire,
                           size_t length, bool control);
static bool cosim_control_can_queue(const DmMc02CosimLink *link,
                                    unsigned needed);
static bool imu_dispatch(DmMc02CosimLink *link, const uint8_t *payload,
                         uint64_t sequence, uint64_t sensor_time_ns,
                         uint64_t due_time_ns, uint64_t step_id,
                         uint64_t step_dt_ns);
static void send_telemetry(DmMc02CosimLink *link, bool force);

static bool send_v1_frame(DmMc02CosimLink *link, uint16_t type,
                          uint64_t sequence, uint64_t virtual_time_ns,
                          const uint8_t *payload, size_t payload_len,
                          bool control)
{
    uint8_t wire[DM_MC02_OUTER_SIZE + DM_MC02_WIRE_HEADER_SIZE +
                 DM_MC02_WIRE_MAX_PAYLOAD] = { 0 };
    DmMc02WireFrame frame = {
        .header = {
            .version = DM_MC02_WIRE_VERSION,
            .type = type,
            .payload_len = payload_len,
            .sequence = sequence,
            .virtual_time_ns = virtual_time_ns,
        },
    };
    size_t body_len;

    if (payload_len > DM_MC02_WIRE_MAX_PAYLOAD ||
        (payload_len && !payload)) {
        return false;
    }
    if (payload_len) {
        memcpy(frame.payload, payload, payload_len);
    }
    body_len = dm_mc02_wire_encode(wire + DM_MC02_OUTER_SIZE,
                                   sizeof(wire) - DM_MC02_OUTER_SIZE,
                                   &frame);
    if (!body_len) {
        return false;
    }
    dm_mc02_wire_put32(wire, body_len);
    return cosim_queue_tx(link, wire, DM_MC02_OUTER_SIZE + body_len, control);
}

static bool send_v2_frame(DmMc02CosimLink *link, uint16_t kind,
                          uint64_t step_id, uint64_t t_sim_ns,
                          uint64_t dt_ns, uint32_t flags,
                          const uint8_t *payload, size_t payload_len)
{
    uint8_t wire[DM_MC02_COSIM_TX_FRAME_SIZE] = { 0 };
    DmMc02V2WireFrame frame = {
        .header = {
            .version = DM_MC02_V2_WIRE_VERSION,
            .kind = kind,
            .payload_len = payload_len,
            .step_id = step_id,
            .t_sim_ns = t_sim_ns,
            .dt_ns = dt_ns,
            .session_id = link->v2_session_id,
        },
    };
    size_t body_len;

    if (payload_len > DM_MC02_V2_WIRE_MAX_PAYLOAD ||
        (payload_len && !payload)) {
        link->tx_dropped++;
        return false;
    }
    if (payload_len) {
        memcpy(frame.payload, payload, payload_len);
    }
    body_len = dm_mc02_v2_wire_encode(wire + DM_MC02_OUTER_SIZE,
                                      sizeof(wire) - DM_MC02_OUTER_SIZE,
                                      &frame);
    if (!body_len) {
        link->tx_dropped++;
        return false;
    }
    dm_mc02_wire_put32(wire, body_len);
    (void)flags;
    return cosim_queue_tx(link, wire, DM_MC02_OUTER_SIZE + body_len, true);
}

static void send_v2_reset_ack(DmMc02CosimLink *link, uint64_t step_id,
                              uint64_t t_sim_ns)
{
    uint8_t payload[DM_MC02_V2_RESET_ACK_PAYLOAD_SIZE];
    DmMc02V2ResetAckPayload value = {
        .status = DM_MC02_V2_STATUS_OK,
        .capabilities = DM_MC02_V2_CAP_STEP | DM_MC02_V2_CAP_IMU |
                        DM_MC02_V2_CAP_ADC | DM_MC02_V2_CAP_DIAGNOSTICS |
                        DM_MC02_V2_CAP_TELEMETRY |
                        (link->motor_step_handler ? DM_MC02_V2_CAP_MOTOR : 0),
        .virtual_time_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
        .queue_depth = link->imu_queue_count,
    };

    if (!dm_mc02_v2_payload_encode_reset_ack(payload, sizeof(payload),
                                              &value)) {
        link->tx_dropped++;
        return;
    }
    send_v2_frame(link, DM_MC02_V2_WIRE_RESET_ACK, step_id, t_sim_ns, 0, 0,
                  payload, sizeof(payload));
}

static void send_v2_step_ack(DmMc02CosimLink *link, uint64_t step_id,
                             uint64_t t_sim_ns, uint64_t dt_ns,
                             uint32_t status)
{
    uint8_t payload[DM_MC02_V2_STEP_ACK_PAYLOAD_SIZE];
    DmMc02V2StepAckPayload value = {
        .status = status,
        .queue_depth = link->imu_queue_count,
        .dropped_count = (uint32_t)MIN(link->imu_dropped, UINT32_MAX),
    };

    if (!dm_mc02_v2_payload_encode_step_ack(payload, sizeof(payload),
                                             &value)) {
        link->tx_dropped++;
        return;
    }
    send_v2_frame(link, DM_MC02_V2_WIRE_STEP_ACK, step_id, t_sim_ns, dt_ns, 0,
                  payload, sizeof(payload));
}

static void send_v2_diagnostics(DmMc02CosimLink *link, uint64_t step_id,
                               uint64_t t_sim_ns)
{
    uint8_t payload[DM_MC02_V2_DIAGNOSTICS_PAYLOAD_SIZE];
    DmMc02V2DiagnosticsPayload value = {
        .rx_frames = link->rx_frames,
        .rx_bad_frames = link->rx_bad_frames,
        .rx_dropped_bytes = link->rx_dropped_bytes,
        .tx_frames = link->tx_frames,
        .tx_dropped = link->tx_dropped,
    };

    if (!dm_mc02_v2_payload_encode_diagnostics(payload, sizeof(payload),
                                                &value)) {
        link->tx_dropped++;
        return;
    }
    send_v2_frame(link, DM_MC02_V2_WIRE_DIAGNOSTICS, step_id, t_sim_ns, 0, 0,
                  payload, sizeof(payload));
}

static bool send_v2_step_done(DmMc02CosimLink *link,
                              const DmMc02CosimConsumeToken *token)
{
    uint8_t payload[DM_MC02_V2_STEP_DONE_PAYLOAD_SIZE];
    uint32_t missing = (DM_MC02_V2_CONSUMED_ACCEL |
                        DM_MC02_V2_CONSUMED_GYRO) &
                       ~token->consumed_mask;
    DmMc02V2StepDonePayload value = {
        .status = DM_MC02_V2_STATUS_OK,
        .consumed_mask = token->consumed_mask,
        .missing_mask = missing,
        .queue_depth = link->imu_queue_count,
        .dropped_count = (uint32_t)MIN(link->imu_dropped, UINT32_MAX),
    };

    if (!dm_mc02_v2_payload_encode_step_done(payload, sizeof(payload),
                                              &value)) {
        link->tx_dropped++;
        return false;
    }
    return send_v2_frame(link, DM_MC02_V2_WIRE_STEP_DONE, token->step_id,
                         token->t_sim_ns, token->dt_ns, 0,
                         payload, sizeof(payload));
}

static void v2_step_done_record(DmMc02CosimLink *link,
                                const DmMc02CosimConsumeToken *token)
{
    link->v2_last_step_done_valid = true;
    link->v2_last_step_done_id = token->step_id;
    link->v2_last_step_done_t_sim_ns = token->t_sim_ns;
    link->v2_last_step_done_dt_ns = token->dt_ns;
    link->v2_last_step_done_consumed_mask = token->consumed_mask;
}

static bool send_v2_step_done_cached(DmMc02CosimLink *link)
{
    DmMc02CosimConsumeToken token;

    if (!link->v2_last_step_done_valid) {
        return false;
    }
    token.step_id = link->v2_last_step_done_id;
    token.t_sim_ns = link->v2_last_step_done_t_sim_ns;
    token.dt_ns = link->v2_last_step_done_dt_ns;
    token.consumed_mask = link->v2_last_step_done_consumed_mask;
    return send_v2_step_done(link, &token);
}

static void cosim_retry_step_done(DmMc02CosimLink *link)
{
    const uint32_t all_consumed = DM_MC02_V2_CONSUMED_ACCEL |
                                  DM_MC02_V2_CONSUMED_GYRO;

    for (unsigned i = 0; i < link->consume_count;) {
        DmMc02CosimConsumeToken *token = &link->consume_queue[i];

        if ((token->consumed_mask & all_consumed) != all_consumed) {
            i++;
            continue;
        }
        if (!send_v2_step_done(link, token)) {
            return;
        }
        v2_step_done_record(link, token);
        cosim_remove_consume_token(link, i);
    }
}

static bool v2_imu_payload_valid(const uint8_t *payload, size_t payload_len)
{
    if (payload_len != DM_MC02_V2_WIRE_IMU_PAYLOAD_SIZE) {
        return false;
    }
    for (unsigned i = 0; i < 12; ++i) {
        if (!isfinite(get_float_le(payload + i * sizeof(uint32_t)))) {
            return false;
        }
    }
    return true;
}

static bool v2_motor_command_payload_valid(const uint8_t *payload,
                                           size_t payload_len)
{
    uint16_t count;
    uint32_t expected;

    if (payload_len < 4) {
        return false;
    }
    count = get16le(payload);
    expected = 4u + (uint32_t)count * 6u;
    /* flags are endpoint-defined command metadata.  The link does not
     * interpret them, so preserve forward-compatible non-zero values. */
    if (expected != payload_len) {
        return false;
    }
    for (unsigned i = 0; i < count; ++i) {
        const uint8_t *entry = payload + 4u + i * 6u;
        uint16_t index = get16le(entry);

        if (index >= count || !isfinite(get_float_le(entry + 2))) {
            return false;
        }
        for (unsigned j = 0; j < i; ++j) {
            if (get16le(payload + 4u + j * 6u) == index) {
                return false;
            }
        }
    }
    return true;
}

static bool v2_motor_state_payload_valid(const uint8_t *payload,
                                         size_t payload_len)
{
    uint16_t count;
    uint32_t expected;

    if (payload_len < 2) {
        return false;
    }
    count = get16le(payload);
    expected = 2u + (uint32_t)count * 14u;
    if (expected != payload_len) {
        return false;
    }
    for (unsigned i = 0; i < count; ++i) {
        const uint8_t *entry = payload + 2u + i * 14u;
        uint16_t index = get16le(entry);

        if (index >= count ||
            !isfinite(get_float_le(entry + 2)) ||
            !isfinite(get_float_le(entry + 6)) ||
            !isfinite(get_float_le(entry + 10))) {
            return false;
        }
        for (unsigned j = 0; j < i; ++j) {
            if (get16le(payload + 2u + j * 14u) == index) {
                return false;
            }
        }
    }
    return true;
}

static bool v2_step_payload_valid(const uint8_t *payload, size_t payload_len)
{
    size_t offset;
    uint16_t section_count;
    uint16_t marker;
    uint32_t seen_adc_inputs = 0;
    uint32_t seen_adc_voltages = 0;
    bool has_imu = false;
    bool has_adc = false;
    bool has_motor = false;

    /* A section payload can also be 60 bytes long.  New section encoders set
     * an explicit marker in the payload header so that it cannot be mistaken
     * for the compact ImuSampleV2 form.  Keep accepting the old zero-reserved
     * header for all other lengths. */
    if (dm_mc02_v2_wire_is_compact_imu(payload, payload_len)) {
        return v2_imu_payload_valid(payload, payload_len);
    }
    if (!dm_mc02_v2_wire_step_payload_begin(payload, payload_len,
                                             &section_count, &marker)) {
        return false;
    }
    offset = DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE;
    for (unsigned i = 0; i < section_count; ++i) {
        DmMc02V2WireSection section;

        if (!dm_mc02_v2_wire_next_section(payload, payload_len, &offset,
                                          &section)) {
            return false;
        }
        switch (section.type) {
        case DM_MC02_V2_WIRE_SECTION_IMU_SAMPLE:
            if (has_imu || !v2_imu_payload_valid(section.payload,
                                                 section.payload_len)) {
                return false;
            }
            has_imu = true;
            break;
        case DM_MC02_V2_WIRE_SECTION_ADC_INPUT:
        {
            DmMc02V2AdcInputPayload value;

            if (!dm_mc02_v2_payload_decode_adc_input(
                    &value, section.payload, section.payload_len) ||
                (seen_adc_inputs & (UINT32_C(1) << value.channel)) != 0) {
                return false;
            }
            seen_adc_inputs |= UINT32_C(1) << value.channel;
            has_adc = true;
            break;
        }
        case DM_MC02_V2_WIRE_SECTION_ADC_VOLTAGE:
        {
            DmMc02V2AdcVoltagePayload value;

            if (!dm_mc02_v2_payload_decode_adc_voltage(
                    &value, section.payload, section.payload_len) ||
                (seen_adc_voltages & (UINT32_C(1) << value.channel)) != 0) {
                return false;
            }
            seen_adc_voltages |= UINT32_C(1) << value.channel;
            has_adc = true;
            break;
        }
        case DM_MC02_V2_WIRE_SECTION_MOTOR_COMMAND:
            if (has_motor || !v2_motor_command_payload_valid(
                                  section.payload, section.payload_len)) {
                return false;
            }
            has_motor = true;
            break;
        case DM_MC02_V2_WIRE_SECTION_MOTOR_STATE:
            /* State is an endpoint response, never a STEP input. */
            return false;
        default:
            return false;
        }
    }
    /* ADC sections are valid standalone inputs.  An IMU section remains
     * optional so a caller can update board analog inputs without fabricating
     * a sensor sample for the same simulation step. */
    return (has_imu || has_adc || has_motor) && offset == payload_len;
}

static bool v2_step_payload_has_motor(const uint8_t *payload,
                                      size_t payload_len)
{
    size_t offset = DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE;
    uint16_t section_count;
    uint16_t marker;

    if (dm_mc02_v2_wire_is_compact_imu(payload, payload_len) ||
        !dm_mc02_v2_wire_step_payload_begin(payload, payload_len,
                                             &section_count, &marker)) {
        return false;
    }
    for (unsigned i = 0; i < section_count; ++i) {
        DmMc02V2WireSection section;

        if (!dm_mc02_v2_wire_next_section(payload, payload_len, &offset,
                                          &section)) {
            return false;
        }
        if (section.type == DM_MC02_V2_WIRE_SECTION_MOTOR_COMMAND ||
            section.type == DM_MC02_V2_WIRE_SECTION_MOTOR_STATE) {
            return true;
        }
    }
    return false;
}

static bool v2_step_session_valid(const DmMc02CosimLink *link,
                                  uint64_t step_id, uint64_t t_sim_ns,
                                  uint64_t dt_ns)
{
    return link->v2_initialized && !link->v2_retry_pending &&
           step_id > link->v2_last_step_id &&
           t_sim_ns > link->v2_last_t_sim_ns && dt_ns != 0 &&
           dt_ns == t_sim_ns - link->v2_last_t_sim_ns;
}

static bool v2_step_is_duplicate(const DmMc02CosimLink *link,
                                 uint64_t step_id, uint64_t t_sim_ns,
                                 uint64_t dt_ns, const uint8_t *payload,
                                 uint32_t payload_len)
{
    return link->v2_last_step_valid && step_id == link->v2_last_step_id &&
           t_sim_ns == link->v2_last_t_sim_ns &&
           dt_ns == link->v2_last_step_dt_ns &&
           payload_len == link->v2_last_step_payload_len &&
           memcmp(payload, link->v2_last_step_payload, payload_len) == 0;
}

static void v2_step_record(DmMc02CosimLink *link, uint64_t step_id,
                           uint64_t t_sim_ns, uint64_t dt_ns,
                           uint32_t status, const uint8_t *payload,
                           uint32_t payload_len,
                           const uint8_t *motor_state_payload,
                           uint32_t motor_state_len)
{
    link->v2_last_step_id = step_id;
    link->v2_last_t_sim_ns = t_sim_ns;
    link->v2_last_step_dt_ns = dt_ns;
    link->v2_last_step_status = status;
    link->v2_last_step_payload_len = payload_len;
    memcpy(link->v2_last_step_payload, payload, payload_len);
    link->v2_last_step_valid = true;
    link->v2_last_motor_state_len = motor_state_len;
    if (motor_state_len) {
        memcpy(link->v2_last_motor_state_payload, motor_state_payload,
               motor_state_len);
    }
    link->v2_last_step_done_valid = false;
    link->v2_retry_motor_state_valid = false;
    link->v2_retry_motor_state_len = 0;
}

static bool v2_dispatch_step_payload(DmMc02CosimLink *link,
                                     const uint8_t *payload,
                                     size_t payload_len, uint64_t step_id,
                                     uint64_t t_sim_ns, uint64_t dt_ns,
                                     uint8_t *motor_state_payload,
                                     size_t *motor_state_len,
                                     bool *motor_protocol_error)
{
    size_t offset;
    uint16_t section_count;
    bool accepted = true;
    unsigned adc_sections = 0;
    bool imu_section = false;
    const uint8_t *motor_command_payload = NULL;
    size_t motor_command_len = 0;
    bool callback_accepted;

    *motor_state_len = 0;
    *motor_protocol_error = false;

    if (dm_mc02_v2_wire_is_compact_imu(payload, payload_len)) {
        cosim_deliver_due(link);
        if (link->imu_handler &&
            link->consume_count == DM_MC02_COSIM_IMU_QUEUE_SIZE) {
            return false;
        }
        return !link->imu_handler ||
               imu_dispatch(link, payload, get32le(payload + 56),
                            get64le(payload + 48), t_sim_ns, step_id, dt_ns);
    }
    {
        uint16_t marker;

        if (!dm_mc02_v2_wire_step_payload_begin(payload, payload_len,
                                                 &section_count, &marker)) {
            return false;
        }
    }
    offset = DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE;
    for (unsigned i = 0; i < section_count; ++i) {
        DmMc02V2WireSection section;

        if (!dm_mc02_v2_wire_next_section(payload, payload_len, &offset,
                                          &section)) {
            return false;
        }

        if (section.type == DM_MC02_V2_WIRE_SECTION_ADC_INPUT ||
            section.type == DM_MC02_V2_WIRE_SECTION_ADC_VOLTAGE) {
            adc_sections++;
        } else if (section.type == DM_MC02_V2_WIRE_SECTION_IMU_SAMPLE) {
            imu_section = true;
        } else if (section.type == DM_MC02_V2_WIRE_SECTION_MOTOR_COMMAND) {
            motor_command_payload = section.payload;
            motor_command_len = section.payload_len;
        }
    }
    offset = DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE;
    if (!link->imu_time_base_valid) {
        imu_schedule_session(link, t_sim_ns);
    }
    cosim_deliver_due(link);
    /* A STEP is one logical input transaction.  Reject it before dispatch if
     * any section cannot fit, so an earlier section cannot commit while a
     * later section of the same STEP is rejected. */
    if (imu_section && link->imu_handler &&
        link->imu_queue_count == DM_MC02_COSIM_IMU_QUEUE_SIZE) {
        return false;
    }
    if (imu_section && link->imu_handler &&
        link->consume_count == DM_MC02_COSIM_IMU_QUEUE_SIZE) {
        return false;
    }
    if (adc_sections &&
        link->adc_queue_count + adc_sections > DM_MC02_COSIM_ADC_QUEUE_SIZE) {
        return false;
    }
    if (motor_command_payload) {
        if (link->v2_retry_pending && link->v2_retry_motor_state_valid) {
            *motor_state_len = link->v2_retry_motor_state_len;
            memcpy(motor_state_payload, link->v2_retry_motor_state_payload,
                   *motor_state_len);
        } else {
            if (!link->motor_step_handler) {
                *motor_state_len = 0;
                link->v2_retry_motor_state_valid = false;
                link->v2_retry_motor_state_len = 0;
                return false;
            }
            callback_accepted = link->motor_step_handler(
                link->opaque, step_id, t_sim_ns, dt_ns,
                motor_command_payload, motor_command_len, motor_state_payload,
                DM_MC02_COSIM_V2_MOTOR_STATE_PAYLOAD_SIZE, motor_state_len);
            if (!callback_accepted) {
                *motor_state_len = 0;
                link->v2_retry_motor_state_valid = false;
                link->v2_retry_motor_state_len = 0;
                return false;
            }
            /* A true callback return is a commit boundary.  An invalid state
             * after that boundary is an endpoint protocol error, not a queue
             * retry; otherwise a committed plant step could be executed again
             * on every retry. */
            if (*motor_state_len >
                    DM_MC02_COSIM_V2_MOTOR_STATE_PAYLOAD_SIZE ||
                (*motor_state_len != 0 &&
                 !v2_motor_state_payload_valid(motor_state_payload,
                                                *motor_state_len))) {
                *motor_protocol_error = true;
                *motor_state_len = 0;
                link->v2_retry_motor_state_valid = false;
                link->v2_retry_motor_state_len = 0;
                return false;
            }
            link->v2_retry_motor_state_len = *motor_state_len;
            if (*motor_state_len) {
                memcpy(link->v2_retry_motor_state_payload,
                       motor_state_payload, *motor_state_len);
            }
            link->v2_retry_motor_state_valid = true;
        }
    }
    offset = DM_MC02_V2_WIRE_PAYLOAD_HEADER_SIZE;
    for (unsigned i = 0; i < section_count; ++i) {
        DmMc02V2WireSection section;

        if (!dm_mc02_v2_wire_next_section(payload, payload_len, &offset,
                                          &section)) {
            return false;
        }
        if (section.type == DM_MC02_V2_WIRE_SECTION_IMU_SAMPLE) {
            if (link->imu_handler &&
                !imu_dispatch(link, section.payload,
                              get32le(section.payload + 56),
                              get64le(section.payload + 48), t_sim_ns,
                              step_id, dt_ns)) {
                accepted = false;
            }
        } else if (section.type == DM_MC02_V2_WIRE_SECTION_ADC_INPUT) {
            DmMc02V2AdcInputPayload value;
            DmMc02CosimAdcSample sample;

            if (!dm_mc02_v2_payload_decode_adc_input(
                    &value, section.payload, section.payload_len)) {
                return false;
            }
            sample = (DmMc02CosimAdcSample) {
                .channel = value.channel,
                .flags = 0,
                .value = value.raw,
                .due_qemu_ns = 0,
                .voltage = false,
            };

            sample.due_qemu_ns = cosim_due_qemu_ns(link, t_sim_ns);
            cosim_deliver_due(link);
            if (sample.due_qemu_ns <= qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) &&
                !link->adc_queue_count) {
                if (link->adc_input_handler) {
                    link->adc_input_handler(link->opaque, sample.channel,
                                            (uint16_t)sample.value);
                }
            } else if (link->adc_queue_count ==
                       DM_MC02_COSIM_ADC_QUEUE_SIZE) {
                link->adc_dropped++;
                accepted = false;
            } else {
                unsigned slot = (link->adc_queue_head +
                                 link->adc_queue_count) %
                                DM_MC02_COSIM_ADC_QUEUE_SIZE;
                link->adc_queue[slot] = sample;
                link->adc_queue_count++;
                if (link->imu_timer) {
                    uint64_t imu_due = link->imu_queue_count ?
                        link->imu_queue[link->imu_queue_head].due_qemu_ns :
                        UINT64_MAX;
                    timer_mod(link->imu_timer,
                              MIN(imu_due, sample.due_qemu_ns));
                }
            }
        } else if (section.type == DM_MC02_V2_WIRE_SECTION_ADC_VOLTAGE) {
            DmMc02V2AdcVoltagePayload value;
            DmMc02CosimAdcSample sample;

            if (!dm_mc02_v2_payload_decode_adc_voltage(
                    &value, section.payload, section.payload_len)) {
                return false;
            }
            sample = (DmMc02CosimAdcSample) {
                .channel = value.channel,
                .flags = value.flags,
                .value = value.voltage_uv,
                .due_qemu_ns = 0,
                .voltage = true,
            };

            sample.due_qemu_ns = cosim_due_qemu_ns(link, t_sim_ns);
            cosim_deliver_due(link);
            if (sample.due_qemu_ns <= qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) &&
                !link->adc_queue_count) {
                if (link->adc_pin_voltage_handler) {
                    link->adc_pin_voltage_handler(link->opaque, sample.channel,
                                                  sample.flags, sample.value);
                }
            } else if (link->adc_queue_count ==
                       DM_MC02_COSIM_ADC_QUEUE_SIZE) {
                link->adc_dropped++;
                accepted = false;
            } else {
                unsigned slot = (link->adc_queue_head +
                                 link->adc_queue_count) %
                                DM_MC02_COSIM_ADC_QUEUE_SIZE;
                link->adc_queue[slot] = sample;
                link->adc_queue_count++;
                if (link->imu_timer) {
                    uint64_t imu_due = link->imu_queue_count ?
                        link->imu_queue[link->imu_queue_head].due_qemu_ns :
                        UINT64_MAX;
                    timer_mod(link->imu_timer,
                              MIN(imu_due, sample.due_qemu_ns));
                }
            }
        }
    }
    return accepted;
}

static bool validate_v2_frame(DmMc02CosimLink *link, uint16_t kind,
                              uint32_t payload_len, uint64_t step_id,
                              uint64_t t_sim_ns, uint64_t dt_ns,
                              uint32_t flags,
                              const uint8_t *payload)
{
    if (link->rx_protocol_version == DM_MC02_WIRE_VERSION) {
        return false;
    }
    if (kind != DM_MC02_V2_WIRE_RESET && kind != DM_MC02_V2_WIRE_STEP &&
        kind != DM_MC02_V2_WIRE_DIAGNOSTICS) {
        return false;
    }
    if ((kind == DM_MC02_V2_WIRE_RESET || kind == DM_MC02_V2_WIRE_DIAGNOSTICS) &&
        payload_len != 0) {
        return false;
    }
    if (kind == DM_MC02_V2_WIRE_STEP &&
        !v2_step_payload_valid(payload, payload_len)) {
        return false;
    }
    if (kind == DM_MC02_V2_WIRE_RESET) {
        v2_validator_reset(link);
        link->rx_protocol_version = DM_MC02_V2_WIRE_VERSION;
        link->rx_last_protocol_version = DM_MC02_V2_WIRE_VERSION;
        link->v2_session_id = flags;
        link->v2_initialized = true;
        link->v2_last_step_id = step_id;
        link->v2_last_t_sim_ns = t_sim_ns;
        return true;
    }
    if (!link->v2_initialized) {
        return false;
    }
    if (flags != link->v2_session_id) {
        return false;
    }
    if (kind == DM_MC02_V2_WIRE_STEP) {
        if (link->v2_retry_pending) {
            if (step_id != link->v2_retry_step_id ||
                t_sim_ns != link->v2_retry_t_sim_ns ||
                dt_ns != link->v2_retry_dt_ns ||
                payload_len != link->v2_retry_payload_len ||
                memcmp(payload, link->v2_retry_payload, payload_len) != 0) {
                return false;
            }
            return true;
        }
        if (step_id <= link->v2_last_step_id ||
            t_sim_ns <= link->v2_last_t_sim_ns || dt_ns == 0 ||
            dt_ns != t_sim_ns - link->v2_last_t_sim_ns) {
            return false;
        }
        /* The input step is committed only after its payload is dispatched. */
        return true;
    }
    if (link->v2_retry_pending || step_id <= link->v2_last_step_id ||
        t_sim_ns <= link->v2_last_t_sim_ns) {
        return false;
    }
    link->v2_last_step_id = step_id;
    link->v2_last_t_sim_ns = t_sim_ns;
    return true;
}

static bool process_v2_frame(DmMc02CosimLink *link, const uint8_t *frame,
                             size_t frame_len)
{
    DmMc02V2WireFrame decoded;
    uint16_t kind;
    uint32_t payload_len;
    uint64_t step_id;
    uint64_t t_sim_ns;
    uint64_t dt_ns;
    uint32_t flags;
    const uint8_t *payload;
    bool accepted;
    bool motor_protocol_error = false;
    uint8_t motor_state_payload[DM_MC02_COSIM_V2_MOTOR_STATE_PAYLOAD_SIZE];
    size_t motor_state_len = 0;

    if (!dm_mc02_v2_wire_decode(&decoded, frame, frame_len)) {
        return false;
    }
    kind = decoded.header.kind;
    payload_len = decoded.header.payload_len;
    step_id = decoded.header.step_id;
    t_sim_ns = decoded.header.t_sim_ns;
    dt_ns = decoded.header.dt_ns;
    flags = decoded.header.session_id;
    payload = decoded.payload;
    if (kind == DM_MC02_V2_WIRE_STEP && link->v2_initialized &&
        !link->v2_retry_pending && flags == link->v2_session_id &&
        step_id == link->v2_last_step_id) {
        if (!v2_step_is_duplicate(link, step_id, t_sim_ns, dt_ns,
                                  payload, payload_len)) {
            return false;
        }
        send_v2_step_ack(link, step_id, t_sim_ns, dt_ns,
                         link->v2_last_step_status);
        if (link->v2_last_step_done_valid &&
            link->v2_last_step_done_id == step_id &&
            link->v2_last_step_done_t_sim_ns == t_sim_ns &&
            link->v2_last_step_done_dt_ns == dt_ns) {
            send_v2_step_done_cached(link);
        }
        if (link->v2_last_motor_state_len) {
            send_v2_frame(link, DM_MC02_V2_WIRE_MOTOR_STATE, step_id,
                          t_sim_ns, dt_ns, 0,
                          link->v2_last_motor_state_payload,
                          link->v2_last_motor_state_len);
        }
        return true;
    }
    if (kind == DM_MC02_V2_WIRE_STEP &&
        v2_step_session_valid(link, step_id, t_sim_ns, dt_ns)) {
        /* The unsupported-endpoint fast path must not turn malformed input
         * into a committed step.  Validate the complete section structure and
         * every typed payload before deciding whether a handler is absent. */
        if (!v2_step_payload_valid(payload, payload_len)) {
            return false;
        }
        if (flags != link->v2_session_id) {
            /* Preserve the old malformed-flags diagnostic for legacy v2
             * peers. A non-zero session id is valid only after RESET; a
             * mismatch on an identified session is stale input and must not
             * advance the validator. */
            if (link->v2_session_id == 0 && flags != 0 &&
                v2_step_payload_valid(payload, payload_len)) {
                link->v2_last_step_id = step_id;
                link->v2_last_t_sim_ns = t_sim_ns;
                send_v2_step_ack(link, step_id, t_sim_ns, dt_ns,
                                 DM_MC02_V2_STATUS_PROTOCOL);
                return true;
            }
            return false;
        }
        if (v2_step_payload_has_motor(payload, payload_len) &&
            !link->motor_step_handler) {
            v2_step_record(link, step_id, t_sim_ns, dt_ns,
                           DM_MC02_V2_STATUS_UNSUPPORTED, payload,
                           payload_len, NULL, 0);
            send_v2_step_ack(link, step_id, t_sim_ns, dt_ns,
                             DM_MC02_V2_STATUS_UNSUPPORTED);
            return true;
        }
    }
    if (!validate_v2_frame(link, kind, payload_len, step_id, t_sim_ns,
                           dt_ns, flags, payload)) {
        return false;
    }
    if (kind == DM_MC02_V2_WIRE_RESET) {
        imu_schedule_session(link, t_sim_ns);
        send_v2_reset_ack(link, step_id, t_sim_ns);
        send_telemetry(link, true);
    } else if (kind == DM_MC02_V2_WIRE_STEP) {
        /* The first six floats of ImuSampleV2 are the physical sample. Bias
         * fields remain plant metadata; the board model applies its own
         * configured sensor effects before exposing BMI088 raw data. */
        accepted = v2_dispatch_step_payload(link, payload, payload_len,
                                             step_id, t_sim_ns, dt_ns,
                                             motor_state_payload,
                                             &motor_state_len,
                                             &motor_protocol_error);
        if (!accepted && motor_protocol_error) {
            link->v2_retry_pending = false;
            link->v2_retry_payload_len = 0;
            v2_step_record(link, step_id, t_sim_ns, dt_ns,
                           DM_MC02_V2_STATUS_PROTOCOL, payload, payload_len,
                           NULL, 0);
            send_v2_step_ack(link, step_id, t_sim_ns, dt_ns,
                             DM_MC02_V2_STATUS_PROTOCOL);
            return true;
        }
        if (accepted) {
            link->v2_retry_pending = false;
            link->v2_retry_payload_len = 0;
            v2_step_record(link, step_id, t_sim_ns, dt_ns,
                           DM_MC02_V2_STATUS_OK, payload, payload_len,
                           motor_state_payload, motor_state_len);
        } else {
            link->v2_retry_pending = true;
            link->v2_retry_step_id = step_id;
            link->v2_retry_t_sim_ns = t_sim_ns;
            link->v2_retry_dt_ns = dt_ns;
            link->v2_retry_payload_len = payload_len;
            memcpy(link->v2_retry_payload, payload, payload_len);
        }
        send_v2_step_ack(link, step_id, t_sim_ns, dt_ns,
                         accepted ? DM_MC02_V2_STATUS_OK :
                                    DM_MC02_V2_STATUS_QUEUE_FULL);
        if (accepted && motor_state_len) {
            send_v2_frame(link, DM_MC02_V2_WIRE_MOTOR_STATE, step_id, t_sim_ns,
                          dt_ns, 0, motor_state_payload, motor_state_len);
        }
    } else {
        send_v2_diagnostics(link, step_id, t_sim_ns);
    }
    return true;
}

static bool imu_dispatch(DmMc02CosimLink *link, const uint8_t *payload,
                         uint64_t sequence, uint64_t sensor_time_ns,
                         uint64_t due_time_ns, uint64_t step_id,
                         uint64_t step_dt_ns)
{
    DmMc02CosimImuSample sample;
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    unsigned slot;

    if (!link->imu_time_base_valid) {
        imu_schedule_session(link, due_time_ns);
    }
    sample.sequence = sequence;
    sample.step_id = step_id;
    sample.sensor_time_ns = sensor_time_ns;
    sample.due_qemu_ns = cosim_due_qemu_ns(link, due_time_ns);
    sample.step_t_sim_ns = due_time_ns;
    sample.step_dt_ns = step_dt_ns;
    for (unsigned i = 0; i < 3; ++i) {
        sample.gyro[i] = get_float_le(payload + i * sizeof(uint32_t));
        sample.accel[i] = get_float_le(payload + 12 + i * sizeof(uint32_t));
    }

    cosim_deliver_due(link);
    if (!link->imu_queue_count && sample.due_qemu_ns <= now) {
        if (link->v2_initialized &&
            !cosim_arm_imu_consumption(link, sample.step_id,
                                        sample.step_t_sim_ns,
                                        sample.step_dt_ns)) {
            link->imu_delivery_blocked = true;
            if (link->imu_timer) {
                timer_del(link->imu_timer);
            }
            return false;
        }
        link->imu_handler(link->opaque, sample.gyro, sample.accel,
                          sample.sequence, sample.step_id,
                          sample.sensor_time_ns);
        return true;
    }
    if (link->imu_queue_count == DM_MC02_COSIM_IMU_QUEUE_SIZE) {
        /* The worker is normally paced with QEMU virtual time.  If a paused
         * guest receives a long future burst, retain the already ordered
         * samples and count the newest overflow rather than blocking QEMU. */
        link->imu_dropped++;
        return false;
    }
    slot = (link->imu_queue_head + link->imu_queue_count) %
           DM_MC02_COSIM_IMU_QUEUE_SIZE;
    link->imu_queue[slot] = sample;
    link->imu_queue_count++;
    if (link->imu_queue_count == 1 && link->imu_timer) {
        timer_mod(link->imu_timer, sample.due_qemu_ns);
    }
    return true;
}

static bool process_frame(DmMc02CosimLink *link, const uint8_t *frame,
                          size_t frame_len)
{
    DmMc02WireFrame decoded;
    uint16_t type;
    uint64_t sequence;
    uint64_t virtual_time_ns;

    if (frame_len >= 8 && get32le(frame) == DM_MC02_WIRE_MAGIC &&
        get16le(frame + 4) == DM_MC02_V2_WIRE_VERSION) {
        return process_v2_frame(link, frame, frame_len);
    }
    if (!dm_mc02_wire_decode(&decoded, frame, frame_len)) {
        return false;
    }
    type = decoded.header.type;
    sequence = decoded.header.sequence;
    virtual_time_ns = decoded.header.virtual_time_ns;

    if (!validate_frame(link, type, decoded.header.payload_len, sequence,
                        virtual_time_ns, decoded.payload)) {
        return false;
    }
    if (type == DM_MC02_FRAME_RESET) {
        imu_schedule_session(link, virtual_time_ns);
    }
    if (type == DM_MC02_FRAME_IMU_SAMPLE && link->imu_handler) {
        imu_dispatch(link, decoded.payload, sequence,
                     virtual_time_ns, virtual_time_ns, sequence, 0);
    } else if (type == DM_MC02_FRAME_ADC_INPUT && link->adc_input_handler) {
        link->adc_input_handler(link->opaque,
                                dm_mc02_wire_get16(decoded.payload),
                                dm_mc02_wire_get16(decoded.payload + 2));
    } else if (type == DM_MC02_FRAME_ADC_PIN_VOLTAGE &&
               link->adc_pin_voltage_handler) {
        link->adc_pin_voltage_handler(
            link->opaque, dm_mc02_wire_get16(decoded.payload),
            dm_mc02_wire_get16(decoded.payload + 2),
            dm_mc02_wire_get32(decoded.payload + 4));
    }
    return true;
}

static bool cosim_control_can_queue(const DmMc02CosimLink *link,
                                    unsigned needed)
{
    unsigned available = DM_MC02_COSIM_TX_QUEUE_SIZE - link->tx_queue_count;

    for (unsigned i = 0; i < link->tx_queue_count; ++i) {
        unsigned slot = (link->tx_queue_head + i) %
                        DM_MC02_COSIM_TX_QUEUE_SIZE;

        if (!link->tx_queue_control[slot] &&
            link->tx_queue_offset[slot] == 0) {
            available++;
        }
    }
    return available >= needed;
}

static void parse_rx(DmMc02CosimLink *link)
{
    while (link->rx_len != 0) {
        uint32_t frame_len;
        size_t framed_len;
        const uint8_t *frame_start = link->rx_buf + link->rx_head;

        if (link->rx_len < DM_MC02_OUTER_SIZE) {
            return;
        }
        frame_len = get32le(frame_start);
        /* The outer size includes the protocol header, not this prefix. */
        if (frame_len < DM_MC02_WIRE_HEADER_SIZE ||
            frame_len > DM_MC02_COSIM_MAX_FRAME_BODY) {
            rx_drop_prefix(link, 1, true);
            continue;
        }
        framed_len = DM_MC02_OUTER_SIZE + frame_len;
        if (link->rx_len < framed_len) {
            return;
        }
        /* Every accepted v2 request produces one immediate control response.
         * Leave a complete request in the RX buffer until that response can be
         * queued; CharBackend backpressure then bounds both directions without
         * blocking the QEMU main loop or silently dropping an ACK. */
        if (frame_len >= 8 &&
            get32le(frame_start + DM_MC02_OUTER_SIZE) == DM_MC02_WIRE_MAGIC &&
            get16le(frame_start + DM_MC02_OUTER_SIZE + 4) ==
                DM_MC02_V2_WIRE_VERSION &&
            frame_len >= 4u + DM_MC02_V2_WIRE_HEADER_SIZE &&
            get32le(frame_start + 12) <= DM_MC02_V2_WIRE_MAX_PAYLOAD &&
            4u + DM_MC02_V2_WIRE_HEADER_SIZE +
                get32le(frame_start + 12) == frame_len &&
            !cosim_control_can_queue(
                link,
                (get16le(frame_start + DM_MC02_OUTER_SIZE + 6) ==
                     DM_MC02_V2_WIRE_STEP && link->motor_step_handler &&
                 v2_step_payload_has_motor(
                     frame_start + DM_MC02_OUTER_SIZE + 4u +
                         DM_MC02_V2_WIRE_HEADER_SIZE,
                     get32le(frame_start + 12))) ? 2u : 1u)) {
            return;
        }
        if (!process_frame(link, frame_start + DM_MC02_OUTER_SIZE,
                           frame_len)) {
            rx_drop_prefix(link, framed_len, true);
            continue;
        } else {
            link->rx_frames++;
        }
        rx_drop_prefix(link, framed_len, false);
    }
}

static int cosim_can_receive(void *opaque)
{
    DmMc02CosimLink *link = opaque;

    if (link->rx_protocol_version == DM_MC02_V2_WIRE_VERSION &&
        !cosim_control_can_queue(link, 1)) {
        return 0;
    }
    return sizeof(link->rx_buf) - link->rx_len;
}

static void cosim_receive(void *opaque, const uint8_t *buf, int size)
{
    DmMc02CosimLink *link = opaque;
    size_t tail;
    size_t copy_size;

    if (size <= 0) {
        return;
    }
    tail = link->rx_head + link->rx_len;
    if (tail + (size_t)size > sizeof(link->rx_buf) && link->rx_head != 0) {
        /* Reclaim consumed space only when the incoming chunk cannot fit at
         * the current tail.  Normal frame-by-frame parsing stays linear. */
        memmove(link->rx_buf, link->rx_buf + link->rx_head, link->rx_len);
        link->rx_head = 0;
        tail = link->rx_len;
    }
    copy_size = MIN((size_t)size, sizeof(link->rx_buf) - tail);
    memcpy(link->rx_buf + tail, buf, copy_size);
    link->rx_len += copy_size;
    if (copy_size != (size_t)size) {
        link->rx_dropped_bytes += (size_t)size - copy_size;
        link->rx_bad_frames++;
    }
    parse_rx(link);
    qemu_chr_fe_accept_input(&link->chr);
}

static bool telemetry_equal(const DmMc02CosimTelemetry *a,
                            const DmMc02CosimTelemetry *b)
{
    return a->led_rgb == b->led_rgb &&
           a->led_brightness == b->led_brightness &&
           a->buzzer == b->buzzer && a->board_flags == b->board_flags &&
           a->buzzer_frequency_hz == b->buzzer_frequency_hz &&
           a->buzzer_duty_permille == b->buzzer_duty_permille;
}

static void cosim_flush_tx(DmMc02CosimLink *link);

static void cosim_tx_timer_cb(void *opaque)
{
    DmMc02CosimLink *link = opaque;

    cosim_flush_tx(link);
    cosim_retry_step_done(link);
    if (link->telemetry_pending && link->enabled && link->opened) {
        /* A state change may have happened while control frames occupied every
         * TX slot.  Retry the latest snapshot after control traffic drains. */
        send_telemetry(link, false);
    }
    parse_rx(link);
    qemu_chr_fe_accept_input(&link->chr);
}

static void cosim_schedule_tx_retry(DmMc02CosimLink *link)
{
    if (link->tx_timer && link->tx_queue_count) {
        timer_mod(link->tx_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  DM_MC02_COSIM_TX_RETRY_NS);
    }
}

static void cosim_flush_tx(DmMc02CosimLink *link)
{
    if (!link->tx_queue_count) {
        if (link->tx_timer) {
            if (link->telemetry_pending && link->enabled && link->opened) {
                timer_mod(link->tx_timer,
                          qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                          DM_MC02_COSIM_TX_RETRY_NS);
            } else {
                timer_del(link->tx_timer);
            }
        }
        return;
    }
    while (link->tx_queue_count != 0) {
        unsigned slot = link->tx_queue_head;
        int remaining = link->tx_queue_len[slot] - link->tx_queue_offset[slot];
        int written = qemu_chr_fe_write(&link->chr,
                                         link->tx_queue[slot] +
                                         link->tx_queue_offset[slot],
                                         remaining);

        if (written <= 0) {
            cosim_schedule_tx_retry(link);
            return;
        }
        if (written < remaining) {
            link->tx_short_writes++;
        }
        link->tx_queue_offset[slot] += written;
        if (link->tx_queue_offset[slot] < link->tx_queue_len[slot]) {
            cosim_schedule_tx_retry(link);
            return;
        }
        link->tx_queue_head = (link->tx_queue_head + 1) %
                              DM_MC02_COSIM_TX_QUEUE_SIZE;
        link->tx_queue_count--;
        link->tx_frames++;
    }
    if (link->tx_timer) {
        if (link->telemetry_pending && link->enabled && link->opened) {
            timer_mod(link->tx_timer,
                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                      DM_MC02_COSIM_TX_RETRY_NS);
        } else {
            timer_del(link->tx_timer);
        }
    }
}

static bool cosim_queue_tx(DmMc02CosimLink *link, const uint8_t *wire,
                           size_t length, bool control)
{
    unsigned slot;

    cosim_flush_tx(link);
    if (length > DM_MC02_COSIM_TX_FRAME_SIZE ||
        link->tx_queue_count == DM_MC02_COSIM_TX_QUEUE_SIZE) {
        if (length > DM_MC02_COSIM_TX_FRAME_SIZE) {
            link->tx_dropped++;
            return false;
        }
        if (link->tx_queue_count == DM_MC02_COSIM_TX_QUEUE_SIZE) {
            unsigned drop = DM_MC02_COSIM_TX_QUEUE_SIZE;

            /* Control frames (RESET/ACK/STEP_DONE) are never displaced by
             * telemetry.  Only remove an unwritten telemetry frame; an
             * already partially written frame must remain at the head or the
             * byte stream would be corrupted. */
            for (unsigned i = 0; i < link->tx_queue_count; ++i) {
                unsigned candidate = (link->tx_queue_head + i) %
                                     DM_MC02_COSIM_TX_QUEUE_SIZE;

                if (!link->tx_queue_control[candidate] &&
                    link->tx_queue_offset[candidate] == 0) {
                    drop = candidate;
                    break;
                }
            }
            if (drop == DM_MC02_COSIM_TX_QUEUE_SIZE) {
                /* A full control queue cannot be made lossless without
                 * blocking QEMU.  Keep the stream valid and expose the drop. */
                link->tx_dropped++;
                return false;
            }
            /* A telemetry frame can have been accepted into the queue but
             * still be unwritten.  If control traffic displaces it, the
             * deduplication baseline must become pending again. */
            link->telemetry_pending = true;
            for (unsigned i = 0; i + 1 < link->tx_queue_count; ++i) {
                unsigned dst = (link->tx_queue_head +
                                 ((drop - link->tx_queue_head +
                                   DM_MC02_COSIM_TX_QUEUE_SIZE) %
                                  DM_MC02_COSIM_TX_QUEUE_SIZE) + i) %
                                DM_MC02_COSIM_TX_QUEUE_SIZE;
                unsigned src = (dst + 1) % DM_MC02_COSIM_TX_QUEUE_SIZE;

                if (dst == (link->tx_queue_head + link->tx_queue_count - 1) %
                           DM_MC02_COSIM_TX_QUEUE_SIZE) {
                    break;
                }
                memcpy(link->tx_queue[dst], link->tx_queue[src],
                       link->tx_queue_len[src]);
                link->tx_queue_len[dst] = link->tx_queue_len[src];
                link->tx_queue_offset[dst] = link->tx_queue_offset[src];
                link->tx_queue_control[dst] = link->tx_queue_control[src];
            }
            link->tx_queue_count--;
            link->tx_dropped++;
        }
    }
    slot = (link->tx_queue_head + link->tx_queue_count) %
           DM_MC02_COSIM_TX_QUEUE_SIZE;
    memcpy(link->tx_queue[slot], wire, length);
    link->tx_queue_len[slot] = length;
    link->tx_queue_offset[slot] = 0;
    link->tx_queue_control[slot] = control;
    link->tx_queue_count++;
    cosim_flush_tx(link);
    return true;
}

static void cosim_clear_tx(DmMc02CosimLink *link)
{
    link->tx_queue_head = 0;
    link->tx_queue_count = 0;
    memset(link->tx_queue_len, 0, sizeof(link->tx_queue_len));
    memset(link->tx_queue_offset, 0, sizeof(link->tx_queue_offset));
    memset(link->tx_queue_control, 0, sizeof(link->tx_queue_control));
}

static void send_telemetry(DmMc02CosimLink *link, bool force)
{
    uint8_t wire[DM_MC02_OUTER_SIZE + DM_MC02_WIRE_HEADER_SIZE +
                 DM_MC02_V2_WIRE_HEADER_SIZE +
                 DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE] = { 0 };
    DmMc02CosimTelemetry telemetry = { 0 };
    uint64_t virtual_time_ns;

    if (!qemu_chr_fe_backend_open(&link->chr)) {
        link->tx_dropped++;
        return;
    }
    if (link->telemetry_provider) {
        link->telemetry_provider(link->opaque, &telemetry);
    }
    if (!force && link->telemetry_valid && !link->telemetry_pending &&
        telemetry_equal(&telemetry, &link->last_telemetry)) {
        /* A pending state may have changed back to the last delivered
         * snapshot before the retry ran.  It is already synchronized. */
        link->telemetry_pending = false;
        return;
    }
    virtual_time_ns = (uint64_t)qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    /* A reset frame and its forced snapshot can be emitted at the same
     * virtual instant. Keep the normal frame strictly after the session
     * marker without advancing the machine clock. */
    if (link->tx_time_valid && virtual_time_ns <= link->tx_last_virtual_time_ns) {
        if (link->tx_last_virtual_time_ns == UINT64_MAX) {
            link->tx_dropped++;
            return;
        }
        virtual_time_ns = link->tx_last_virtual_time_ns + 1;
    }
    if (link->rx_protocol_version == DM_MC02_V2_WIRE_VERSION) {
        DmMc02V2WireFrame frame = {
            .header = {
                .version = DM_MC02_V2_WIRE_VERSION,
                .kind = DM_MC02_V2_WIRE_TELEMETRY,
                .payload_len = DM_MC02_V2_BOARD_TELEMETRY_PAYLOAD_SIZE,
                .step_id = link->tx_sequence++,
                .t_sim_ns = virtual_time_ns,
                .dt_ns = 0,
                .session_id = link->v2_session_id,
            },
        };
        size_t body_len;
        DmMc02V2BoardTelemetryPayload value = {
            .led_rgb = telemetry.led_rgb,
            .led_brightness = telemetry.led_brightness,
            .buzzer = telemetry.buzzer ? 1u : 0u,
            .board_flags = telemetry.board_flags,
            .buzzer_frequency_hz = telemetry.buzzer_frequency_hz,
            .buzzer_duty_permille = telemetry.buzzer_duty_permille,
        };

        if (!dm_mc02_v2_payload_encode_board_telemetry(
                frame.payload, sizeof(frame.payload), &value)) {
            link->tx_dropped++;
            link->telemetry_pending = true;
            return;
        }
        body_len = dm_mc02_v2_wire_encode(
            wire + DM_MC02_OUTER_SIZE, sizeof(wire) - DM_MC02_OUTER_SIZE,
            &frame);
        if (!body_len) {
            link->tx_dropped++;
            link->telemetry_pending = true;
            return;
        }
        dm_mc02_wire_put32(wire, body_len);
        if (!cosim_queue_tx(link, wire, DM_MC02_OUTER_SIZE + body_len,
                            false)) {
            link->telemetry_pending = true;
            return;
        }
    } else {
        uint8_t payload[DM_MC02_TLM_PAYLOAD_SIZE] = { 0 };

        put32le(payload, telemetry.led_rgb);
        put32le(payload + 4, telemetry.led_brightness);
        payload[8] = telemetry.buzzer ? 1 : 0;
        payload[9] = telemetry.board_flags;
        if (!send_v1_frame(link, DM_MC02_FRAME_TELEMETRY,
                           link->tx_sequence++, virtual_time_ns, payload,
                           sizeof(payload), false)) {
            link->telemetry_pending = true;
            return;
        }
    }
    link->last_telemetry = telemetry;
    link->telemetry_valid = true;
    link->tx_last_virtual_time_ns = virtual_time_ns;
    link->tx_time_valid = true;
    link->telemetry_pending = false;
}

static void send_reset(DmMc02CosimLink *link)
{
    uint64_t virtual_time_ns = (uint64_t)qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    (void)send_v1_frame(link, DM_MC02_FRAME_RESET, 0, virtual_time_ns,
                        NULL, 0, true);
    link->tx_last_virtual_time_ns = virtual_time_ns;
    link->tx_time_valid = true;
}

void dm_mc02_cosim_link_notify_telemetry(DmMc02CosimLink *link)
{
    if (link && link->enabled && link->opened) {
        send_telemetry(link, false);
    }
}

void dm_mc02_cosim_link_reset(DmMc02CosimLink *link)
{
    uint16_t protocol_version;
    uint64_t virtual_time_ns;

    if (!link) {
        return;
    }
    protocol_version = link->rx_protocol_version;
    if (protocol_version == 0) {
        protocol_version = link->rx_last_protocol_version;
    }
    rx_reset(link);
    imu_schedule_clear(link);
    link->tx_sequence = 1;
    link->tx_time_valid = false;
    link->telemetry_valid = false;
    link->telemetry_pending = false;
    cosim_clear_tx(link);
    if (link->enabled && link->opened) {
        if (protocol_version == DM_MC02_V2_WIRE_VERSION) {
            virtual_time_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            /* The peer observes this RESET as the start of the new session.
             * Keep the native validator in the same state so its first STEP
             * after RESET/RESET_ACK is accepted without requiring a second
             * host-generated RESET. */
            link->rx_protocol_version = DM_MC02_V2_WIRE_VERSION;
            link->rx_last_protocol_version = DM_MC02_V2_WIRE_VERSION;
            link->v2_session_id = next_v2_session_id(link);
            link->v2_initialized = true;
            link->v2_last_step_id = 0;
            link->v2_last_t_sim_ns = virtual_time_ns;
            send_v2_frame(link, DM_MC02_V2_WIRE_RESET, 0,
                          virtual_time_ns, 0, 0,
                          NULL, 0);
            send_v2_reset_ack(link, 0, virtual_time_ns);
            send_telemetry(link, true);
        } else {
            send_reset(link);
            send_telemetry(link, true);
        }
    }
}

void dm_mc02_cosim_link_set_adc_handler(
    DmMc02CosimLink *link, DmMc02CosimAdcInputHandler *handler)
{
    if (link) {
        link->adc_input_handler = handler;
    }
}

void dm_mc02_cosim_link_set_adc_pin_voltage_handler(
    DmMc02CosimLink *link,
    DmMc02CosimAdcPinVoltageHandler *handler)
{
    if (link) {
        link->adc_pin_voltage_handler = handler;
    }
}

void dm_mc02_cosim_link_set_motor_step_handler(
    DmMc02CosimLink *link, DmMc02CosimMotorStepHandler *handler)
{
    if (link) {
        link->motor_step_handler = handler;
    }
}

void dm_mc02_cosim_link_notify_imu_consumed(DmMc02CosimLink *link,
                                            uint64_t step_id,
                                            uint32_t consumed_mask)
{
    const uint32_t all_consumed = DM_MC02_V2_CONSUMED_ACCEL |
                                  DM_MC02_V2_CONSUMED_GYRO;

    if (!link || !link->v2_initialized || !(consumed_mask & all_consumed)) {
        return;
    }
    consumed_mask &= all_consumed;
    for (unsigned i = 0; i < link->consume_count; ++i) {
        DmMc02CosimConsumeToken *token = &link->consume_queue[i];

        if (token->step_id != step_id) {
            continue;
        }
        token->consumed_mask |= consumed_mask;
        if ((token->consumed_mask & all_consumed) == all_consumed) {
            if (send_v2_step_done(link, token)) {
                v2_step_done_record(link, token);
                cosim_remove_consume_token(link, i);
                if (link->imu_delivery_blocked) {
                    link->imu_delivery_blocked = false;
                    cosim_deliver_due(link);
                }
            }
        }
        return;
    }
}

static void cosim_event(void *opaque, QEMUChrEvent event)
{
    DmMc02CosimLink *link = opaque;

    if (event == CHR_EVENT_OPENED) {
        bool reconnect = link->tx_session_started;
        uint16_t protocol_version = link->rx_protocol_version ?
            link->rx_protocol_version : link->rx_last_protocol_version;
        uint64_t virtual_time_ns;

        /* A peer may have closed immediately after sending a finite future
         * burst. Drop samples from the old session only when the replacement
         * session is actually established; until then the queued samples are
         * still valid input for the running QEMU virtual clock. */
        imu_schedule_clear(link);
        rx_reset(link);
        link->tx_sequence = 1;
        link->tx_time_valid = false;
        link->telemetry_valid = false;
        link->telemetry_pending = false;
        link->opened = true;
        cosim_flush_tx(link);
        if (reconnect && protocol_version == DM_MC02_V2_WIRE_VERSION) {
            virtual_time_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            link->rx_protocol_version = DM_MC02_V2_WIRE_VERSION;
            link->rx_last_protocol_version = DM_MC02_V2_WIRE_VERSION;
            link->v2_session_id = next_v2_session_id(link);
            link->v2_initialized = true;
            link->v2_last_step_id = 0;
            link->v2_last_t_sim_ns = virtual_time_ns;
            send_v2_frame(link, DM_MC02_V2_WIRE_RESET, 0, virtual_time_ns,
                          0, 0, NULL, 0);
            send_v2_reset_ack(link, 0, virtual_time_ns);
        } else if (reconnect) {
            send_reset(link);
        }
        send_telemetry(link, true);
        link->tx_session_started = true;
    } else if (event == CHR_EVENT_CLOSED) {
        link->opened = false;
        link->telemetry_valid = false;
        link->telemetry_pending = false;
        cosim_clear_tx(link);
        rx_reset(link);
        /* Keep already accepted future IMU samples alive. A disconnect is
         * not a machine reset, and clearing here loses finite non-realtime
         * bursts before their virtual timestamps become due. A later OPENED
         * event clears them before starting a new input session. */
        if (link->tx_timer) {
            timer_del(link->tx_timer);
        }
    }
}

bool dm_mc02_cosim_link_init(DmMc02CosimLink *link, Chardev *chardev,
                             DmMc02CosimImuHandler *imu_handler,
                             DmMc02CosimTelemetryProvider *telemetry_provider,
                             void *opaque, Error **errp)
{
    if (!link || !chardev) {
        return true;
    }
    memset(link, 0, sizeof(*link));
    link->imu_handler = imu_handler;
    link->telemetry_provider = telemetry_provider;
    link->opaque = opaque;
    link->tx_sequence = 1;
    link->tx_time_valid = false;
    link->imu_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, imu_timer_cb, link);
    link->tx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cosim_tx_timer_cb, link);
    if (!qemu_chr_fe_init(&link->chr, chardev, errp)) {
        timer_free(link->imu_timer);
        timer_free(link->tx_timer);
        link->imu_timer = NULL;
        link->tx_timer = NULL;
        return false;
    }
    link->enabled = true;
    qemu_chr_fe_set_handlers(&link->chr, cosim_can_receive, cosim_receive,
                             cosim_event, NULL, link, NULL, true);
    /* A backend which was already open may not need to emit a second event. */
    if (qemu_chr_fe_backend_open(&link->chr) && !link->opened) {
        cosim_event(link, CHR_EVENT_OPENED);
    }
    return true;
}

void dm_mc02_cosim_link_cleanup(DmMc02CosimLink *link)
{
    if (!link || !link->enabled) {
        return;
    }
    qemu_chr_fe_set_handlers(&link->chr, NULL, NULL, NULL, NULL, NULL, NULL,
                             false);
    qemu_chr_fe_deinit(&link->chr, false);
    timer_free(link->imu_timer);
    timer_free(link->tx_timer);
    link->imu_timer = NULL;
    link->tx_timer = NULL;
    link->enabled = false;
    link->opened = false;
}
