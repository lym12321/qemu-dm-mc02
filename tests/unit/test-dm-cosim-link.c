/* Production co-sim lifecycle, with only clock and transport isolated. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_cosim_link.h"

static int64_t test_now_ns;
static GByteArray *test_tx;
static bool test_tx_blocked;
static int test_tx_budget;
static bool test_tx_close_on_write;
static IOEventHandler *test_chr_event;
static void *test_chr_opaque;

static int64_t test_clock_get_ns(QEMUClockType clock)
{
    g_assert_cmpint(clock, ==, QEMU_CLOCK_VIRTUAL);
    return test_now_ns;
}

static QEMUTimer *test_timer_new_ns(QEMUClockType clock, QEMUTimerCB *cb,
                                   void *opaque)
{
    QEMUTimer *timer = g_new0(QEMUTimer, 1);

    g_assert_cmpint(clock, ==, QEMU_CLOCK_VIRTUAL);
    timer->cb = cb;
    timer->opaque = opaque;
    timer->expire_time = -1;
    return timer;
}

static void test_timer_mod(QEMUTimer *timer, int64_t deadline)
{
    timer->expire_time = deadline;
}

static void test_timer_del(QEMUTimer *timer)
{
    timer->expire_time = -1;
}

static bool test_chr_init(CharBackend *backend, Chardev *chardev, Error **errp)
{
    return true;
}

static void test_chr_deinit(CharBackend *backend, bool remove)
{
}

static bool test_chr_open(CharBackend *backend)
{
    return true;
}

static void test_chr_handlers(CharBackend *backend, IOCanReadHandler *can_read,
                              IOReadHandler *read, IOEventHandler *event,
                              BackendChangeHandler *changed, void *opaque,
                              GMainContext *context, bool open)
{
    test_chr_event = event;
    test_chr_opaque = opaque;
}

static void test_chr_accept_input(CharBackend *backend)
{
}

static int test_chr_write(CharBackend *backend, const uint8_t *bytes, int length)
{
    if (test_tx_close_on_write) {
        test_tx_close_on_write = false;
        test_chr_event(test_chr_opaque, CHR_EVENT_CLOSED);
        return -1;
    }
    if (test_tx_blocked || test_tx_budget == 0) {
        return 0;
    }
    if (test_tx_budget > 0) {
        length = MIN(length, test_tx_budget);
        test_tx_budget -= length;
    }
    g_byte_array_append(test_tx, bytes, length);
    return length;
}

/* Keep the real parser, queues, delivery and encoding in this test TU. */
#define qemu_clock_get_ns test_clock_get_ns
#define timer_new_ns test_timer_new_ns
#define timer_mod test_timer_mod
#define timer_del test_timer_del
#define timer_free g_free
#define qemu_chr_fe_init test_chr_init
#define qemu_chr_fe_deinit test_chr_deinit
#define qemu_chr_fe_backend_open test_chr_open
#define qemu_chr_fe_set_handlers test_chr_handlers
#define qemu_chr_fe_accept_input test_chr_accept_input
#define qemu_chr_fe_write test_chr_write
#include "../../hw/arm/dm_mc02_cosim_link.c"

typedef struct Fixture {
    DmMc02CosimLink link;
    Chardev chardev;
    uint64_t newest_source;
    uint64_t retired_source;
    unsigned deliveries;
    unsigned motor_steps;
    uint32_t admission_mask;
    uint32_t newest_mask;
    bool probe_mid_delivery;
} Fixture;

static uint32_t retained(void *opaque, uint64_t source_id)
{
    Fixture *f = opaque;

    if (!source_id || source_id == f->retired_source ||
        source_id > f->newest_source) {
        return 0;
    }
    return source_id == f->newest_source ? f->newest_mask : 3;
}

static void inject_imu(void *opaque, const float gyro[3], const float accel[3],
                       uint64_t source_id, uint64_t step_id,
                       uint64_t sensor_time_ns)
{
    Fixture *f = opaque;

    g_assert_cmpuint(source_id, >, f->newest_source);
    f->newest_source = source_id;
    f->deliveries++;
    if (f->probe_mid_delivery) {
        /* One die has admitted the sample; the other has not run yet. */
        f->newest_mask = 2;
        dm_mc02_cosim_link_notify_imu_source_changed(&f->link);
    }
    f->newest_mask = f->admission_mask;
    dm_mc02_cosim_link_notify_imu_source_changed(&f->link);
}

static bool motor_step(void *opaque, uint64_t step_id, uint64_t timestamp,
                       uint64_t dt, const uint8_t *command,
                       size_t command_len, uint8_t *state, size_t capacity,
                       size_t *state_len)
{
    Fixture *f = opaque;
    static const uint8_t empty_command[4] = { 0 };

    g_assert_cmpmem(command, command_len, empty_command, 4);
    g_assert_cmpuint(capacity, >=, 2);
    state[0] = 0;
    state[1] = 0;
    *state_len = 2;
    f->motor_steps++;
    return true;
}

static void feed_frame(Fixture *f, uint16_t kind, uint64_t step_id,
                       uint64_t timestamp, uint64_t dt,
                       const uint8_t *payload, size_t payload_len,
                       uint32_t session_id)
{
    DmMc02V2WireFrame frame = {
        .header = { .version = 2, .kind = kind, .step_id = step_id,
                    .t_sim_ns = timestamp, .dt_ns = dt,
                    .payload_len = payload_len, .session_id = session_id },
    };
    uint8_t bytes[DM_MC02_COSIM_TX_FRAME_SIZE];
    size_t body_len;

    if (payload_len) {
        memcpy(frame.payload, payload, payload_len);
    }
    body_len = dm_mc02_v2_wire_encode(bytes + 4, sizeof(bytes) - 4, &frame);
    g_assert_cmpuint(body_len, >, 0);
    dm_mc02_wire_put32(bytes, body_len);
    cosim_receive(&f->link, bytes, body_len + 4);
}

static void feed_step(Fixture *f, unsigned step_id, bool immediately_due)
{
    uint8_t imu[60] = { 0 };
    uint64_t timestamp = f->link.v2_last_t_sim_ns + 1000000;

    dm_mc02_wire_put64(imu + 48, timestamp);
    dm_mc02_wire_put32(imu + 56, 42);
    if (immediately_due) {
        test_now_ns += 1000000;
    }
    feed_frame(f, 2, step_id, timestamp, 1000000, imu, sizeof(imu),
               f->link.v2_session_id);
}

static void feed_motor_step(Fixture *f, unsigned step_id)
{
    uint8_t sections[84] = { 2, 0, 0x43, 0x53, 1, 0, 0, 0, 60 };
    uint64_t timestamp = f->link.v2_last_t_sim_ns + 1000000;

    dm_mc02_wire_put64(sections + 12 + 48, timestamp);
    dm_mc02_wire_put32(sections + 12 + 56, 42);
    sections[72] = 2; /* MOTOR_COMMAND section with zero motors. */
    sections[76] = 4;
    test_now_ns += 1000000;
    feed_frame(f, 2, step_id, timestamp, 1000000, sections, sizeof(sections),
               f->link.v2_session_id);
}

static DmMc02V2WireFrame take_frame(uint16_t kind, uint64_t step_id)
{
    DmMc02V2WireFrame frame;
    size_t length;

    g_assert_cmpuint(test_tx->len, >=, 4);
    length = dm_mc02_wire_get32(test_tx->data);
    g_assert_cmpuint(test_tx->len, >=, length + 4);
    g_assert_true(dm_mc02_v2_wire_decode(&frame, test_tx->data + 4, length));
    g_assert_cmpuint(frame.header.kind, ==, kind);
    if (step_id != UINT64_MAX) {
        g_assert_cmpuint(frame.header.step_id, ==, step_id);
    }
    g_byte_array_remove_range(test_tx, 0, length + 4);
    return frame;
}

static void expect_done(uint64_t step_id, const uint8_t expected[20])
{
    DmMc02V2WireFrame frame = take_frame(6, step_id);

    g_assert_cmpuint(frame.header.payload_len, ==, 20);
    g_assert_cmpmem(frame.payload, 20, expected, 20);
}

static void expect_ack(uint64_t step_id)
{
    DmMc02V2WireFrame frame = take_frame(3, step_id);

    g_assert_cmpuint(frame.header.payload_len, ==, 16);
    g_assert_cmpuint(dm_mc02_wire_get32(frame.payload), ==, 0);
}

static Fixture *fixture_new(void)
{
    Fixture *f = g_new0(Fixture, 1);

    test_now_ns = 0;
    test_tx_blocked = false;
    test_tx_budget = -1;
    test_tx_close_on_write = false;
    test_tx = g_byte_array_new();
    f->admission_mask = 3;
    g_assert_true(dm_mc02_cosim_link_init(&f->link, &f->chardev,
                                         inject_imu, NULL, f, NULL));
    dm_mc02_cosim_link_set_imu_retained_handler(&f->link, retained);
    g_byte_array_set_size(test_tx, 0); /* Initial v1 telemetry. */
    feed_frame(f, 1, 0, 0, 0, NULL, 0, 0);
    take_frame(5, 0);
    take_frame(7, UINT64_MAX);
    g_assert_cmpuint(test_tx->len, ==, 0);
    return f;
}

static void fixture_free(Fixture *f)
{
    dm_mc02_cosim_link_cleanup(&f->link);
    g_byte_array_unref(test_tx);
    g_free(f);
}

static void test_rejection_ack_and_exact_replay(void)
{
    static const uint8_t unavailable[20] = {
        2, 0, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    };
    Fixture *f = fixture_new();

    f->admission_mask = 0; /* Powered-off/ODR-rejected producer. */
    dm_mc02_cosim_link_set_motor_step_handler(&f->link, motor_step);
    feed_motor_step(f, 1);
    expect_ack(1);
    take_frame(8, 1);
    expect_done(1, unavailable);
    g_assert_cmpuint(f->link.consume_count, ==, 0);
    g_assert_cmpuint(test_tx->len, ==, 0);

    /* A delayed future input changes queue_depth; replay stays byte-exact. */
    {
        uint8_t imu[60] = { 0 };
        dm_mc02_wire_put64(imu + 48, 2000000);
        dm_mc02_wire_put32(imu + 56, 42);
        g_assert_true(imu_dispatch(&f->link, imu, 42, 2000000, 2000000,
                                   99, 1000000));
    }
    feed_frame(f, 2, 1, 1000000, 1000000,
               f->link.v2_last_step_payload,
               f->link.v2_last_step_payload_len, 0);
    expect_ack(1);
    take_frame(8, 1);
    expect_done(1, unavailable);
    g_assert_cmpuint(f->deliveries, ==, 1);
    g_assert_cmpuint(f->motor_steps, ==, 1);
    fixture_free(f);
}

static void test_source_exhaustion_precedes_motor_commit(void)
{
    Fixture *f = fixture_new();
    DmMc02V2WireFrame ack;

    dm_mc02_cosim_link_set_motor_step_handler(&f->link, motor_step);
    f->link.imu_source_id = UINT64_MAX;
    feed_motor_step(f, 1);
    ack = take_frame(3, 1);
    g_assert_cmpuint(dm_mc02_wire_get32(ack.payload), ==, 2);
    g_assert_cmpuint(f->link.imu_source_id, ==, UINT64_MAX);
    g_assert_cmpuint(f->deliveries, ==, 0);
    g_assert_cmpuint(f->motor_steps, ==, 0);
    g_assert_cmpuint(f->link.consume_count, ==, 0);
    g_assert_cmpuint(test_tx->len, ==, 0);
    fixture_free(f);
}

static void test_historical_sources_and_order(void)
{
    static const uint8_t consumed[20] = {
        0, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    };
    Fixture *f = fixture_new();
    uint64_t first, second;

    f->probe_mid_delivery = true;
    feed_step(f, 1, true);
    first = f->newest_source;
    expect_ack(1);
    feed_step(f, 2, true);
    second = f->newest_source;
    expect_ack(2);
    g_assert_cmpuint(f->link.consume_count, ==, 2);
    g_assert_cmpuint(test_tx->len, ==, 0);

    /* Retained FIFO history can complete after a newer raw source. */
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, second, 3);
    g_assert_cmpuint(test_tx->len, ==, 0);
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, first, 1);
    g_assert_cmpuint(test_tx->len, ==, 0);
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, first, 2);
    expect_done(1, consumed);
    expect_done(2, consumed);
    g_assert_cmpuint(f->link.consume_count, ==, 0);
    fixture_free(f);
}

static void test_backpressure_freezes_terminal_and_resumes_delivery(void)
{
    static const uint8_t failed_partial[20] = {
        2, 0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0,
    };
    Fixture *f = fixture_new();
    uint64_t first;
    uint8_t diagnostic[40] = { 0 };

    feed_step(f, 1, true);
    first = f->newest_source;
    expect_ack(1);
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, first, 1);
    for (unsigned i = 2; i <= 63; i++) {
        feed_step(f, i, true);
        expect_ack(i);
    }
    feed_step(f, 64, false);
    expect_ack(64);
    feed_step(f, 65, false);
    expect_ack(65);
    test_now_ns = 65000000;
    f->link.imu_timer->cb(f->link.imu_timer->opaque);
    g_assert_cmpuint(f->link.consume_count, ==, 64);
    g_assert_cmpuint(f->link.imu_queue_count, ==, 1);
    g_assert_true(f->link.imu_delivery_blocked);

    test_tx_blocked = true;
    for (unsigned i = 0; i < 8; i++) {
        g_assert_true(send_v2_frame(&f->link, 4, 100 + i, 65000000, 0, 0,
                                    diagnostic, sizeof(diagnostic)));
    }
    f->retired_source = first;
    dm_mc02_cosim_link_notify_imu_source_changed(&f->link);
    g_assert_true(f->link.consume_queue[0].terminal);
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, first, 2);
    g_assert_cmpuint(f->link.consume_queue[0].consumed_mask, ==, 1);
    g_assert_cmpuint(f->link.consume_count, ==, 64);

    test_tx_blocked = false;
    f->link.tx_timer->cb(f->link.tx_timer->opaque);
    for (unsigned i = 0; i < 8; i++) {
        take_frame(4, 100 + i);
    }
    expect_done(1, failed_partial);
    g_assert_cmpuint(f->deliveries, ==, 65);
    g_assert_cmpuint(f->link.consume_count, ==, 64);
    g_assert_cmpuint(f->link.imu_queue_count, ==, 0);
    g_assert_false(f->link.imu_delivery_blocked);
    g_assert_cmpuint(test_tx->len, ==, 0);
    fixture_free(f);
}

static void test_reset_does_not_reuse_sources(void)
{
    static const uint8_t consumed[20] = {
        0, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    };
    Fixture *f = fixture_new();
    uint64_t old_source, session_source;

    feed_step(f, 1, true);
    old_source = f->newest_source;
    expect_ack(1);
    feed_frame(f, 1, 0, 0, 0, NULL, 0, 7);
    take_frame(5, 0);
    take_frame(7, UINT64_MAX);
    feed_step(f, 1, true); /* Same dataset sequence and STEP id. */
    session_source = f->newest_source;
    expect_ack(1);
    g_assert_cmpuint(session_source, >, old_source);
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, old_source, 3);
    g_assert_cmpuint(test_tx->len, ==, 0);
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, session_source, 3);
    expect_done(1, consumed);

    feed_step(f, 2, true);
    old_source = f->newest_source;
    expect_ack(2);
    dm_mc02_cosim_link_clear_inputs(&f->link);
    f->retired_source = old_source;
    dm_mc02_cosim_link_notify_imu_source_changed(&f->link);
    g_assert_cmpuint(test_tx->len, ==, 0);
    dm_mc02_cosim_link_reset(&f->link);
    take_frame(1, 0);
    take_frame(5, 0);
    take_frame(7, UINT64_MAX);
    feed_step(f, 1, true);
    expect_ack(1);
    g_assert_cmpuint(f->newest_source, >, old_source);
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, old_source, 3);
    g_assert_cmpuint(test_tx->len, ==, 0);
    dm_mc02_cosim_link_notify_imu_consumed(&f->link, f->newest_source, 3);
    expect_done(1, consumed);
    fixture_free(f);
}

static void test_partial_frame_survives_machine_reset(void)
{
    /* Literal v2 DIAGNOSTICS frame: 4-byte length, magic, 36-byte header,
     * and a 40-byte payload. No encoder supplies the expected bytes. */
    static const uint8_t old_wire[84] = {
        [0] = 80, [4] = 0x44, [5] = 0x4d, [6] = 0x43, [7] = 0x32,
        [8] = 2, [10] = 4, [12] = 40, [16] = 100,
    };
    uint8_t payload[40] = { 0 };
    Fixture *f = fixture_new();
    DmMc02V2WireFrame reset, ack, snapshot;
    uint64_t short_writes = f->link.tx_short_writes;

    /* The ring has wrapped before the failing frame begins. */
    f->link.tx_queue_head = DM_MC02_COSIM_TX_QUEUE_SIZE - 1;
    test_tx_budget = 47; /* Header plus three payload bytes. */
    g_assert_true(send_v2_frame(&f->link, 4, 100, 0, 0, 0,
                                payload, sizeof(payload)));
    g_assert_cmpmem(test_tx->data, test_tx->len, old_wire, 47);
    g_assert_cmpuint(f->link.tx_short_writes, ==, short_writes + 1);
    g_assert_true(send_v2_frame(&f->link, 4, 101, 0, 0, 0,
                                payload, sizeof(payload)));
    dm_mc02_cosim_link_reset(&f->link);
    /* A second reset cancels the first reset's unstarted frames as well. */
    dm_mc02_cosim_link_reset(&f->link);
    g_assert_cmpuint(test_tx->len, ==, 47);

    test_tx_budget = -1;
    f->link.tx_timer->cb(f->link.tx_timer->opaque);
    g_assert_cmpuint(test_tx->len, >=, sizeof(old_wire));
    g_assert_cmpmem(test_tx->data, sizeof(old_wire),
                    old_wire, sizeof(old_wire));
    take_frame(4, 100);
    reset = take_frame(1, 0);
    ack = take_frame(5, 0);
    g_assert_cmpuint(reset.header.session_id, ==, f->link.v2_session_id);
    g_assert_cmpuint(ack.header.session_id, ==, reset.header.session_id);
    snapshot = take_frame(7, UINT64_MAX);
    g_assert_cmpuint(snapshot.header.t_sim_ns, >, reset.header.t_sim_ns);
    g_assert_cmpuint(test_tx->len, ==, 0); /* No frame 101 or stale RESET. */
    g_assert_cmpuint(f->link.tx_queue_count, ==, 0);
    feed_step(f, 1, true);
    expect_ack(1);
    fixture_free(f);
}

static void test_partial_v1_header_survives_machine_reset(void)
{
    /* Literal empty v1 RESET: a 28-byte body. */
    static const uint8_t old_wire[32] = {
        [0] = 28, [4] = 0x44, [5] = 0x4d, [6] = 0x43, [7] = 0x32,
        [8] = 1, [10] = 1,
    };
    Fixture *f = fixture_new();
    DmMc02WireFrame frame;

    f->link.rx_protocol_version = 1;
    f->link.rx_last_protocol_version = 1;
    test_tx_budget = 2; /* Half of the outer length word. */
    g_assert_true(send_v1_frame(&f->link, 1, 0, 0, NULL, 0, true));
    dm_mc02_cosim_link_reset(&f->link);
    test_tx_budget = -1;
    f->link.tx_timer->cb(f->link.tx_timer->opaque);
    g_assert_cmpuint(test_tx->len, >=, sizeof(old_wire) * 2);
    g_assert_cmpmem(test_tx->data, sizeof(old_wire),
                    old_wire, sizeof(old_wire));
    g_byte_array_remove_range(test_tx, 0, sizeof(old_wire));
    g_assert_cmpmem(test_tx->data, sizeof(old_wire),
                    old_wire, sizeof(old_wire));
    g_byte_array_remove_range(test_tx, 0, sizeof(old_wire));
    g_assert_cmpuint(dm_mc02_wire_get32(test_tx->data), ==,
                     test_tx->len - 4);
    g_assert_true(dm_mc02_wire_decode(&frame, test_tx->data + 4,
                                      test_tx->len - 4));
    g_assert_cmpuint(frame.header.type, ==, 3);
    fixture_free(f);
}

static void test_disconnect_drops_partial_frame_and_reentrant_enqueue(void)
{
    uint8_t payload[40] = { 0 };
    Fixture *f = fixture_new();
    DmMc02V2WireFrame reset;

    test_tx_budget = 47;
    g_assert_true(send_v2_frame(&f->link, 4, 100, 0, 0, 0,
                                payload, sizeof(payload)));
    test_tx_close_on_write = true;
    /* Socket CharBackend may raise CLOSED synchronously during a write. */
    g_assert_false(send_v2_frame(&f->link, 4, 101, 0, 0, 0,
                                 payload, sizeof(payload)));
    g_assert_false(f->link.opened);
    g_assert_cmpuint(f->link.tx_queue_count, ==, 0);
    g_assert_cmpint(f->link.tx_timer->expire_time, ==, -1);

    g_byte_array_set_size(test_tx, 0); /* The peer discarded its old socket. */
    test_tx_budget = -1;
    cosim_event(&f->link, CHR_EVENT_OPENED);
    reset = take_frame(1, 0);
    g_assert_cmpuint(reset.header.session_id, ==, f->link.v2_session_id);
    take_frame(5, 0);
    take_frame(7, UINT64_MAX);
    g_assert_cmpuint(test_tx->len, ==, 0);
    fixture_free(f);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-cosim-link/rejection-replay",
                    test_rejection_ack_and_exact_replay);
    g_test_add_func("/dm-cosim-link/historical-order",
                    test_historical_sources_and_order);
    g_test_add_func("/dm-cosim-link/backpressure-resume",
                    test_backpressure_freezes_terminal_and_resumes_delivery);
    g_test_add_func("/dm-cosim-link/reset-source-identity",
                    test_reset_does_not_reuse_sources);
    g_test_add_func("/dm-cosim-link/exhaustion-before-motor",
                    test_source_exhaustion_precedes_motor_commit);
    g_test_add_func("/dm-cosim-link/partial-tx-machine-reset",
                    test_partial_frame_survives_machine_reset);
    g_test_add_func("/dm-cosim-link/partial-v1-header-reset",
                    test_partial_v1_header_survives_machine_reset);
    g_test_add_func("/dm-cosim-link/partial-tx-disconnect",
                    test_disconnect_drops_partial_frame_and_reentrant_enqueue);
    return g_test_run();
}
