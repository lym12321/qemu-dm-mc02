/* Focused VMState and host transport tests for the reusable FDCAN component. */
#include "qemu/osdep.h"
#include "chardev/char.h"
#include "hw/arm/dm_mc02_fdcan.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/config-file.h"
#include "qemu/main-loop.h"
#include "qemu/module.h"
#include "qemu/option.h"
#include "qemu/sockets.h"
#include "io/channel-file.h"
#include "sysemu/cpu-timers.h"
#include "sysemu/sysemu.h"

#include <fcntl.h>
#include <unistd.h>

#define FDCAN_IR   0x050
#define FDCAN_IE   0x054
#define FDCAN_ILS  0x058
#define FDCAN_ILE  0x05c
#define FDCAN_RXF0C 0x0a0
#define FDCAN_RXF0S 0x0a4
#define FDCAN_RXF1C 0x0b0
#define FDCAN_TXBC  0x0c0
#define FDCAN_PSR   0x044
#define FDCAN_CCCR  0x018
#define FDCAN_TXESC 0x0c8
#define FDCAN_TXBAR 0x0d0
#define FDCAN_TXBTO 0x0d8

static int temp_fd;
static int64_t test_clock_ns;

int64_t cpu_get_clock(void)
{
    return test_clock_ns;
}

#define TYPE_TX_CAPTURE_CHARDEV "dm-fdcan-test-write-capture"

typedef struct WriteAttempt {
    int length;
    uint8_t bytes[84];
} WriteAttempt;

typedef struct TxCaptureSource {
    GSource parent;
    GIOCondition condition;
} TxCaptureSource;

typedef struct TxCaptureChardev {
    Chardev parent_obj;
    int results[4];
    unsigned result_count;
    bool writable;
    bool close_on_failure;
    int failure_errno;
    bool watch_supported;
    unsigned watch_created;
    GSource *watch_source;
    unsigned calls;
    WriteAttempt attempts[32];
    unsigned accepted_length;
    uint8_t accepted[16 * 84];
} TxCaptureChardev;

static gboolean capture_source_dispatch(GSource *source, GSourceFunc callback,
                                         gpointer opaque)
{
    TxCaptureSource *capture_source = (TxCaptureSource *)source;
    FEWatchFunc func = (FEWatchFunc)callback;

    g_source_set_ready_time(source, -1);
    return func(NULL, capture_source->condition, opaque);
}

static GSourceFuncs capture_source_funcs = {
    .dispatch = capture_source_dispatch,
};

static GSource *capture_add_watch(Chardev *chr, GIOCondition condition)
{
    TxCaptureChardev *capture = (TxCaptureChardev *)chr;
    TxCaptureSource *source;

    if (!capture->watch_supported) {
        return NULL;
    }
    source = (TxCaptureSource *)g_source_new(&capture_source_funcs,
                                            sizeof(*source));
    source->condition = condition & G_IO_OUT;
    g_source_set_ready_time(&source->parent, -1);
    g_clear_pointer(&capture->watch_source, g_source_unref);
    capture->watch_source = g_source_ref(&source->parent);
    capture->watch_created++;
    return &source->parent;
}

static void capture_finalize(Object *obj)
{
    TxCaptureChardev *capture = (TxCaptureChardev *)obj;

    g_clear_pointer(&capture->watch_source, g_source_unref);
}

/* The mock controls backend writes/readiness and records actual bytes. */
static int capture_write(Chardev *chr, const uint8_t *bytes, int length)
{
    TxCaptureChardev *capture = (TxCaptureChardev *)chr;
    WriteAttempt *attempt;
    int result;

    g_assert_cmpint(length, >, 0);
    g_assert_cmpint(length, <=, sizeof(capture->attempts[0].bytes));
    g_assert_cmpuint(capture->calls, <, G_N_ELEMENTS(capture->attempts));
    attempt = &capture->attempts[capture->calls];
    attempt->length = length;
    memcpy(attempt->bytes, bytes, length);
    result = capture->calls < capture->result_count ?
             capture->results[capture->calls] :
             (capture->writable ? length : -1);
    capture->calls++;
    if (result < 0) {
        if (capture->close_on_failure) {
            qemu_chr_be_event(chr, CHR_EVENT_CLOSED);
        }
        errno = capture->close_on_failure ? EINVAL :
                (capture->failure_errno ? capture->failure_errno : EAGAIN);
    } else {
        g_assert_cmpint(result, <=, length);
        g_assert_cmpuint(capture->accepted_length + result, <=,
                         sizeof(capture->accepted));
        memcpy(capture->accepted + capture->accepted_length, bytes, result);
        capture->accepted_length += result;
    }
    return result;
}

static void capture_class_init(ObjectClass *klass, void *data)
{
    CHARDEV_CLASS(klass)->chr_write = capture_write;
    CHARDEV_CLASS(klass)->chr_add_watch = capture_add_watch;
}

static const TypeInfo capture_type = {
    .name = TYPE_TX_CAPTURE_CHARDEV,
    .parent = TYPE_CHARDEV,
    .instance_size = sizeof(TxCaptureChardev),
    .instance_finalize = capture_finalize,
    .class_init = capture_class_init,
};

/*
 * Fixed sim/transports/can_codec.py <IIB3xQ64s wire at 1000 ns,
 * independent of the FDCAN serializer, not a silicon/physical ACK oracle.
 * All unspecified bytes are zero, including padding and the payload tail.
 */
static const uint8_t expected_wire[84] = {
    [1] = 0x02, [8] = 0x08, [12] = 0xe8, [13] = 0x03,
    [21] = 0x40, [22] = 0xff, [23] = 0xc0,
};

/*
 * RM0468 TX element / ST HAL: standard ID 0x200 at bits 28:18,
 * DLC 8 at bits 19:16, followed by eight literal data bytes.
 */
static const uint8_t tx_element[16] = {
    0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x08, 0x00,
    0x00, 0x40, 0xff, 0xc0, 0x00, 0x00, 0x00, 0x00,
};

static TxCaptureChardev *init_tx_fixture(DmMc02Fdcan *fdcan, uint8_t *ram,
                                        const int *results, unsigned count)
{
    TxCaptureChardev *capture =
        (TxCaptureChardev *)object_new(TYPE_TX_CAPTURE_CHARDEV);

    g_assert_false(qemu_clock_has_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(count, <=, G_N_ELEMENTS(capture->results));
    memcpy(capture->results, results, count * sizeof(*results));
    capture->result_count = count;
    test_clock_ns = 1000;
    memcpy(ram, tx_element, sizeof(tx_element));
    qemu_chr_be_event(CHARDEV(capture), CHR_EVENT_OPENED);
    g_assert_true(dm_mc02_fdcan_init(fdcan, NULL, "test-fdcan-tx",
                                     DM_MC02_FDCAN_REGION_SIZE, ram,
                                     sizeof(tx_element), CHARDEV(capture),
                                     &error_abort));
    fdcan->iomem.ops->write(fdcan->iomem.opaque, FDCAN_CCCR, 0, 4);
    fdcan->iomem.ops->write(fdcan->iomem.opaque, FDCAN_TXBC, 0x01000000, 4);
    fdcan->iomem.ops->write(fdcan->iomem.opaque, FDCAN_TXESC, 0, 4);
    /*
     * This model policy acknowledges queue admission, not suffix delivery
     * or a physical CAN ACK.  Keep unrelated no-ACK errors out of this gate.
     */
    dm_mc02_fdcan_set_host_ack(fdcan, true);
    return capture;
}

static void cleanup_tx_fixture(DmMc02Fdcan *fdcan,
                               TxCaptureChardev *capture)
{
    dm_mc02_fdcan_cleanup(fdcan);
    g_assert_null(fdcan->tx_timer);
    g_assert_cmpuint(fdcan->tx_next_ns, ==, 0);
    g_assert_null(capture->parent_obj.be);
    g_assert_false(qemu_clock_has_timers(QEMU_CLOCK_VIRTUAL));
    object_unref(OBJECT(capture));
}

static void assert_attempt(const TxCaptureChardev *capture, unsigned call,
                            const uint8_t *expected, unsigned length)
{
    g_assert_cmpuint(call, <, capture->calls);
    g_assert_cmpuint(capture->attempts[call].length, ==, length);
    g_assert_cmpmem(capture->attempts[call].bytes, length, expected, length);
}

static void test_tx_shortwrite_retry(void)
{
    const int results[] = { 17, 0, -1, 67 };
    uint8_t ram[16];
    DmMc02Fdcan fdcan;
    TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                               G_N_ELEMENTS(results));

    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(capture->calls, ==, 1);
    assert_attempt(capture, 0, expected_wire, 84);
    g_assert_cmpuint(capture->accepted_length, ==, 17);
    g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
    g_assert_cmpuint(fdcan.tx_short_writes, ==, 1);
    g_assert_cmpuint(fdcan.iomem.ops->read(fdcan.iomem.opaque,
                                          FDCAN_TXBTO, 4), ==, 1);

    /* Only the real virtual retry timer may release this pending suffix. */
    for (unsigned retry = 1; retry <= 3; ++retry) {
        int64_t deadline = 1000 + retry * INT64_C(1000000);

        g_assert_true(timer_pending(fdcan.tx_timer));
        g_assert_cmpuint(fdcan.tx_next_ns, ==, deadline);
        test_clock_ns = deadline - 1;
        g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
        g_assert_cmpuint(capture->calls, ==, retry);
        g_assert_cmpuint(capture->accepted_length, ==, 17);
        g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
        test_clock_ns = deadline;
        g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
        g_assert_cmpuint(capture->calls, ==, retry + 1);
        assert_attempt(capture, retry, expected_wire + 17, 67);
        if (retry < 3) {
            g_assert_cmpuint(capture->accepted_length, ==, 17);
            g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
        }
    }
    g_assert_cmpuint(capture->accepted_length, ==, 84);
    g_assert_cmpmem(capture->accepted, 84, expected_wire, 84);
    g_assert_cmpuint(fdcan.tx_short_writes, ==, 1);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    test_clock_ns = 4001000;
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(capture->calls, ==, 4);
    g_assert_cmpuint(capture->accepted_length, ==, 84);
    cleanup_tx_fixture(&fdcan, capture);
}

static void test_tx_writable_source_retry(void)
{
    const int results[] = { 17, 0, -1, 67 };
    uint8_t ram[16];
    DmMc02Fdcan fdcan;
    TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                               G_N_ELEMENTS(results));

    capture->watch_supported = true;
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(capture->watch_created, ==, 1);
    g_assert_cmpuint(fdcan.tx_watch_tag, !=, 0);
    g_assert_cmpuint(capture->calls, ==, 1);
    assert_attempt(capture, 0, expected_wire, 84);
    g_assert_cmpuint(capture->accepted_length, ==, 17);
    g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 1);
    g_assert_cmpuint(fdcan.tx_queue_offset[fdcan.tx_queue_head], ==, 17);

    for (unsigned retry = 1; retry <= 3; ++retry) {
        g_assert_nonnull(capture->watch_source);
        g_assert_false(g_source_is_destroyed(capture->watch_source));
        g_assert_cmpint(g_source_get_ready_time(capture->watch_source), ==, -1);
        g_assert_false(timer_pending(fdcan.tx_timer));
        g_assert_cmpuint(fdcan.tx_next_ns, ==, 1001000);
        g_main_context_iteration(NULL, FALSE);
        g_assert_cmpuint(capture->calls, ==, retry);
        g_assert_cmpuint(capture->watch_created, ==, retry);
        g_assert_cmpuint(capture->accepted_length, ==, 17);
        g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);

        /* Dispatch the installed public frontend callback through GLib. */
        g_source_set_ready_time(capture->watch_source, 0);
        g_assert_true(g_main_context_iteration(NULL, FALSE));
        g_assert_cmpuint(capture->calls, ==, retry + 1);
        assert_attempt(capture, retry, expected_wire + 17, 67);
        g_assert_cmpint(test_clock_ns, ==, 1000);
    }
    g_assert_cmpuint(capture->watch_created, ==, 3);
    g_assert_true(g_source_is_destroyed(capture->watch_source));
    g_assert_cmpuint(capture->accepted_length, ==, 84);
    g_assert_cmpmem(capture->accepted, 84, expected_wire, 84);
    g_assert_cmpuint(fdcan.tx_short_writes, ==, 1);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_watch_tag, ==, 0);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_main_context_iteration(NULL, FALSE);
    g_assert_cmpuint(capture->calls, ==, 4);
    g_assert_cmpint(test_clock_ns, ==, 1000);
    cleanup_tx_fixture(&fdcan, capture);
}

static void test_tx_writable_source_single_owner(void)
{
    const int results[] = { 17, 0, 67 };
    uint8_t ram[16];
    DmMc02Fdcan fdcan;
    TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                               G_N_ELEMENTS(results));
    guint tag;

    capture->watch_supported = true;
    capture->writable = true;
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    tag = fdcan.tx_watch_tag;
    g_assert_cmpuint(tag, !=, 0);
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(fdcan.tx_watch_tag, ==, tag);
    g_assert_cmpuint(capture->watch_created, ==, 1);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 2);
    assert_attempt(capture, 1, expected_wire + 17, 67);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_source_set_ready_time(capture->watch_source, 0);
    g_assert_true(g_main_context_iteration(NULL, FALSE));
    g_assert_cmpuint(capture->calls, ==, 4);
    assert_attempt(capture, 2, expected_wire + 17, 67);
    assert_attempt(capture, 3, expected_wire, 84);
    g_assert_cmpuint(capture->accepted_length, ==, 168);
    g_assert_cmpmem(capture->accepted, 84, expected_wire, 84);
    g_assert_cmpmem(capture->accepted + 84, 84, expected_wire, 84);
    g_assert_cmpuint(fdcan.tx_watch_tag, ==, 0);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_true(g_source_is_destroyed(capture->watch_source));
    g_assert_cmpint(test_clock_ns, ==, 1000);
    cleanup_tx_fixture(&fdcan, capture);
}

static void test_tx_writable_source_errors(void)
{
    /* Fatal write, HUP before native CLOSED, and synchronous write CLOSED. */
    for (unsigned mode = 0; mode < 3; ++mode) {
        const int results[] = { 17, -1 };
        uint8_t ram[16];
        DmMc02Fdcan fdcan;
        TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                                   G_N_ELEMENTS(results));

        capture->watch_supported = true;
        fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
        if (mode == 0) {
            capture->failure_errno = EPIPE;
            /* A second producer call must remove the already armed source. */
            fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
        } else {
            if (mode == 1) {
                TxCaptureSource *source =
                    (TxCaptureSource *)capture->watch_source;

                source->condition = G_IO_OUT | G_IO_HUP;
            } else {
                capture->close_on_failure = true;
            }
            g_source_set_ready_time(capture->watch_source, 0);
            g_assert_true(g_main_context_iteration(NULL, FALSE));
        }
        g_assert_cmpuint(fdcan.tx_watch_tag, ==, 0);
        g_assert_true(g_source_is_destroyed(capture->watch_source));
        g_assert_cmpuint(capture->watch_created, ==, 1);
        g_assert_cmpuint(capture->calls, ==, mode == 1 ? 1 : 2);
        if (mode != 1) {
            assert_attempt(capture, 1, expected_wire + 17, 67);
        }
        g_assert_cmpuint(capture->accepted_length, ==, 17);
        g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
        if (mode < 2) {
            g_assert_true(qemu_chr_fe_backend_open(&fdcan.chr));
            g_assert_true(timer_pending(fdcan.tx_timer));
            g_assert_cmpuint(fdcan.tx_next_ns, ==, 1001000);
            g_assert_cmpuint(fdcan.tx_queue_count, ==, mode == 0 ? 2 : 1);
        } else {
            g_assert_false(qemu_chr_fe_backend_open(&fdcan.chr));
            g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
            g_assert_cmpuint(fdcan.tx_dropped, ==, 1);
        }
        g_main_context_iteration(NULL, FALSE);
        g_assert_cmpuint(capture->calls, ==, mode == 1 ? 1 : 2);
        qemu_chr_be_event(CHARDEV(capture), CHR_EVENT_CLOSED);
        g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
        g_assert_false(timer_pending(fdcan.tx_timer));
        g_assert_cmpint(test_clock_ns, ==, 1000);
        cleanup_tx_fixture(&fdcan, capture);
    }
}

static void test_tx_writable_source_terminal_cancel(void)
{
    /* Distinct owners must cancel before replacing state or releasing it. */
    for (unsigned mode = 0; mode < 4; ++mode) {
        const int results[] = { 17 };
        uint8_t ram[16];
        DmMc02Fdcan fdcan;
        TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                                   G_N_ELEMENTS(results));

        capture->watch_supported = true;
        fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
        g_assert_cmpuint(fdcan.tx_watch_tag, !=, 0);
        g_source_set_ready_time(capture->watch_source, 0);
        switch (mode) {
        case 0:
            dm_mc02_fdcan_reset(&fdcan);
            break;
        case 1:
            dm_mc02_fdcan_set_powered(&fdcan, false);
            break;
        case 2:
            qemu_chr_be_event(CHARDEV(capture), CHR_EVENT_CLOSED);
            break;
        default:
            dm_mc02_fdcan_cleanup(&fdcan);
            break;
        }
        g_assert_cmpuint(fdcan.tx_watch_tag, ==, 0);
        g_assert_true(g_source_is_destroyed(capture->watch_source));
        g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
        g_assert_false(qemu_clock_has_timers(QEMU_CLOCK_VIRTUAL));
        /* An already-ready source cannot invoke the detached component. */
        g_main_context_iteration(NULL, FALSE);
        g_assert_cmpuint(capture->calls, ==, 1);
        g_assert_cmpuint(capture->accepted_length, ==, 17);
        g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
        g_assert_cmpint(test_clock_ns, ==, 1000);
        if (mode == 3) {
            g_assert_null(fdcan.tx_timer);
            g_assert_null(capture->parent_obj.be);
            object_unref(OBJECT(capture));
        } else {
            g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
            cleanup_tx_fixture(&fdcan, capture);
        }
    }
}

static void test_tx_bounded_queue_newest_drop(void)
{
    const int results[] = { 17 };
    uint8_t ram[16];
    uint8_t expected[84];
    DmMc02Fdcan fdcan;
    DmMc02FdcanTxState tx_state;
    TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                               G_N_ELEMENTS(results));

    for (unsigned tag = 0; tag < 17; ++tag) {
        ram[8] = tag;
        fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    }
    g_assert_cmpuint(capture->calls, ==, 16);
    assert_attempt(capture, 0, expected_wire, 84);
    for (unsigned call = 1; call < 16; ++call) {
        assert_attempt(capture, call, expected_wire + 17, 67);
    }
    g_assert_cmpuint(capture->accepted_length, ==, 17);
    g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
    g_assert_cmpuint(fdcan.tx_short_writes, ==, 1);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 16);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 1);
    g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
    g_assert_cmpuint(tx_state.pending_frames, ==, 16);
    g_assert_cmpuint(tx_state.pending_bytes, ==, 1327);
    g_assert_cmpuint(tx_state.tx_dropped, ==, 1);
    g_assert_cmpuint(tx_state.tx_short_writes, ==, 1);
    g_assert_true(timer_pending(fdcan.tx_timer));
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 1001000);
    capture->writable = true;
    test_clock_ns = 1000999;
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(capture->calls, ==, 16);
    g_assert_cmpuint(capture->accepted_length, ==, 17);
    g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);

    test_clock_ns = 1001000;
    g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(capture->calls, ==, 32);
    assert_attempt(capture, 16, expected_wire + 17, 67);
    g_assert_cmpuint(capture->accepted_length, ==, 16 * 84);
    for (unsigned tag = 0; tag < 16; ++tag) {
        memcpy(expected, expected_wire, sizeof(expected));
        expected[20] = tag; /* Independent wire payload byte zero. */
        g_assert_cmpmem(capture->accepted + tag * 84, 84, expected, 84);
        if (tag) {
            assert_attempt(capture, 16 + tag, expected, 84);
        }
    }
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 1);
    g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
    g_assert_cmpuint(tx_state.pending_frames, ==, 0);
    g_assert_cmpuint(tx_state.pending_bytes, ==, 0);
    g_assert_cmpuint(tx_state.tx_dropped, ==, 1);
    g_assert_cmpuint(tx_state.tx_short_writes, ==, 1);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    test_clock_ns = 2001000;
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(capture->calls, ==, 32);
    g_assert_cmpuint(capture->accepted_length, ==, 16 * 84);
    cleanup_tx_fixture(&fdcan, capture);
}

static void test_tx_disconnect_during_write(void)
{
    const int results[] = { 17, -1 };
    uint8_t ram[16];
    DmMc02Fdcan fdcan;
    TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                               G_N_ELEMENTS(results));

    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 1);
    capture->close_on_failure = true;
    /* Public backend event reentrancy, not normal socket RX/HUP timing. */
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_false(qemu_chr_fe_backend_open(&fdcan.chr));
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 2);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpuint(capture->calls, ==, 2);
    assert_attempt(capture, 1, expected_wire + 17, 67);
    g_assert_cmpuint(capture->accepted_length, ==, 17);
    g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
    cleanup_tx_fixture(&fdcan, capture);
}

static void test_tx_disconnect_drops_old_session(void)
{
    const int results[] = { 17, -1 };
    uint8_t ram[16];
    uint8_t fresh[84];
    DmMc02Fdcan fdcan;
    TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                               G_N_ELEMENTS(results));

    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    ram[8] = 1;
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_true(qemu_chr_fe_backend_open(&fdcan.chr));
    g_assert_cmpint(qemu_chr_be_can_write(CHARDEV(capture)), >, 0);
    g_assert_cmpuint(capture->calls, ==, 2);
    assert_attempt(capture, 0, expected_wire, 84);
    assert_attempt(capture, 1, expected_wire + 17, 67);
    g_assert_cmpuint(capture->accepted_length, ==, 17);
    g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 2);
    g_assert_cmpuint(fdcan.tx_queue_offset[fdcan.tx_queue_head], ==, 17);
    g_assert_cmpuint(fdcan.tx_queue_offset[(fdcan.tx_queue_head + 1) %
                                          DM_MC02_FDCAN_TX_QUEUE_SIZE], ==, 0);
    g_assert_cmpuint(fdcan.tx_short_writes, ==, 1);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 0);
    g_assert_true(timer_pending(fdcan.tx_timer));
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 1001000);
    g_assert_cmpuint(fdcan.iomem.ops->read(fdcan.iomem.opaque,
                                          FDCAN_TXBTO, 4), ==, 1);

    /* Delayed native CLOSED is the boundary, not the blocked write itself. */
    qemu_chr_be_event(CHARDEV(capture), CHR_EVENT_CLOSED);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_false(qemu_chr_fe_backend_open(&fdcan.chr));
    g_assert_cmpuint(fdcan.tx_queue_head, ==, 0);
    for (unsigned slot = 0; slot < DM_MC02_FDCAN_TX_QUEUE_SIZE; ++slot) {
        g_assert_cmpuint(fdcan.tx_queue_offset[slot], ==, 0);
    }
    g_assert_cmpuint(fdcan.tx_dropped, ==, 2);
    g_assert_cmpuint(fdcan.tx_short_writes, ==, 1);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpuint(capture->calls, ==, 2);
    g_assert_cmpuint(capture->accepted_length, ==, 17);
    g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
    g_assert_cmpuint(fdcan.iomem.ops->read(fdcan.iomem.opaque,
                                          FDCAN_TXBTO, 4), ==, 1);

    qemu_chr_be_event(CHARDEV(capture), CHR_EVENT_CLOSED);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 2);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));

    /* Existing bounded admission while closed retains a fresh whole record. */
    ram[8] = 0x55;
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 1);
    g_assert_cmpuint(fdcan.tx_queue_offset[fdcan.tx_queue_head], ==, 0);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 2);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpuint(capture->calls, ==, 2);
    g_assert_cmpuint(capture->accepted_length, ==, 17);
    g_assert_cmpuint(fdcan.iomem.ops->read(fdcan.iomem.opaque,
                                          FDCAN_TXBTO, 4), ==, 1);

    capture->writable = true;
    qemu_chr_be_event(CHARDEV(capture), CHR_EVENT_OPENED);
    memcpy(fresh, expected_wire, sizeof(fresh));
    fresh[20] = 0x55; /* Independent wire payload byte zero. */
    g_assert_true(qemu_chr_fe_backend_open(&fdcan.chr));
    g_assert_cmpuint(capture->calls, ==, 3);
    assert_attempt(capture, 2, fresh, 84);
    g_assert_cmpuint(capture->accepted_length, ==, 17 + 84);
    /* Never erase the prefix already accepted by the old connection. */
    g_assert_cmpmem(capture->accepted, 17, expected_wire, 17);
    g_assert_cmpmem(capture->accepted + 17, 84, fresh, 84);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 2);
    g_assert_cmpuint(fdcan.tx_short_writes, ==, 1);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));

    /* Advance after OPENED completes; the old retry must stay cancelled. */
    test_clock_ns = 1001000;
    g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_cmpuint(capture->calls, ==, 3);
    g_assert_cmpuint(capture->accepted_length, ==, 17 + 84);
    cleanup_tx_fixture(&fdcan, capture);
}

#ifdef CONFIG_LINUX
static gboolean socket_deadline_wakeup(gpointer opaque)
{
    return G_SOURCE_REMOVE;
}

static void wait_socket_state(Chardev *chr, DmMc02Fdcan *fdcan, bool connected)
{
    int64_t deadline = g_get_monotonic_time() + 2 * G_USEC_PER_SEC;
    GSource *timeout = g_timeout_source_new(2000);

    g_source_set_callback(timeout, socket_deadline_wakeup, NULL, NULL);
    g_source_set_ready_time(timeout, deadline);
    g_source_attach(timeout, NULL);
    for (;;) {
        bool actual = object_property_get_bool(OBJECT(chr), "connected",
                                               &error_abort);
        bool opened = qemu_chr_fe_backend_open(&fdcan->chr);

        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
        if (actual == connected && opened == connected) {
            break;
        }
        /* Native listener/read/HUP only; do not run the virtual retry timer. */
        g_main_context_iteration(NULL, TRUE);
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
    }
    g_source_destroy(timeout);
    g_source_unref(timeout);
}

static int connect_socket_peer(const char *path)
{
    struct sockaddr_un addr = { .sun_family = AF_UNIX };
    int64_t deadline = g_get_monotonic_time() + 2 * G_USEC_PER_SEC;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    int ret;

    g_assert_cmpint(fd, >=, 0);
    g_assert_cmpuint(strlen(path), <, sizeof(addr.sun_path));
    strcpy(addr.sun_path, path);
    ret = connect(fd, (struct sockaddr *)&addr, sizeof(addr));
    if (ret < 0) {
        GPollFD pfd = { .fd = fd, .events = G_IO_OUT };
        int error;
        socklen_t length = sizeof(error);

        g_assert_cmpint(errno, ==, EINPROGRESS);
        do {
            int64_t remaining = deadline - g_get_monotonic_time();

            g_assert_cmpint(remaining, >, 0);
            ret = qemu_poll_ns(&pfd, 1, remaining * 1000);
        } while (ret < 0 && errno == EINTR);
        g_assert_cmpint(ret, ==, 1);
        g_assert_cmpint(getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length),
                        ==, 0);
        g_assert_cmpint(error, ==, 0);
    }
    g_assert_cmpint(g_get_monotonic_time(), <, deadline);
    return fd;
}

static void recv_socket_wire(int fd, uint8_t wire[84])
{
    int64_t deadline = g_get_monotonic_time() + 2 * G_USEC_PER_SEC;
    unsigned received = 0;

    while (received < 84) {
        int64_t remaining = deadline - g_get_monotonic_time();
        ssize_t length;

        g_assert_cmpint(remaining, >, 0);
        length = recv(fd, wire + received, 84 - received, 0);
        if (length < 0 && errno == EINTR) {
            continue;
        }
        if (length < 0 && errno == EAGAIN) {
            GPollFD pfd = { .fd = fd, .events = G_IO_IN };
            int ret = qemu_poll_ns(&pfd, 1, remaining * 1000);

            if (ret < 0 && errno == EINTR) {
                continue;
            }
            g_assert_cmpint(ret, ==, 1);
        } else {
            g_assert_cmpint(length, >, 0);
            received += length;
        }
    }
    g_assert_cmpint(g_get_monotonic_time(), <, deadline);
}

static Chardev *init_socket_fixture(DmMc02Fdcan *fdcan, uint8_t ram[16],
                                    char *directory, char **path)
{
    QemuOpts *opts;
    Chardev *chr;

    g_assert_nonnull(g_mkdtemp(directory));
    *path = g_build_filename(directory, "can.sock", NULL);
    opts = qemu_opts_create(qemu_find_opts("chardev"), "test-fdcan-socket",
                            1, &error_abort);
    qemu_opt_set(opts, "backend", "socket", &error_abort);
    qemu_opt_set(opts, "path", *path, &error_abort);
    qemu_opt_set_bool(opts, "server", true, &error_abort);
    qemu_opt_set_bool(opts, "wait", false, &error_abort);
    chr = qemu_chr_new_from_opts(opts, NULL, &error_abort);
    qemu_opts_del(opts);
    g_assert_nonnull(chr);
    g_assert_false(object_property_get_bool(OBJECT(chr), "connected",
                                            &error_abort));
    g_assert_false(qemu_clock_has_timers(QEMU_CLOCK_VIRTUAL));
    test_clock_ns = 1000;
    memcpy(ram, tx_element, sizeof(tx_element));
    ram[8] = 0xa5;
    g_assert_true(dm_mc02_fdcan_init(fdcan, NULL, "test-fdcan-socket",
                                     DM_MC02_FDCAN_REGION_SIZE, ram,
                                     sizeof(tx_element), chr, &error_abort));
    fdcan->iomem.ops->write(fdcan->iomem.opaque, FDCAN_CCCR, 0, 4);
    fdcan->iomem.ops->write(fdcan->iomem.opaque, FDCAN_TXBC, 0x01000000, 4);
    fdcan->iomem.ops->write(fdcan->iomem.opaque, FDCAN_TXESC, 0, 4);
    dm_mc02_fdcan_set_host_ack(fdcan, true);
    return chr;
}

static void cleanup_socket_fixture(DmMc02Fdcan *fdcan, Chardev *chr,
                                    const char *directory, const char *path)
{
    dm_mc02_fdcan_cleanup(fdcan);
    g_assert_null(fdcan->tx_timer);
    g_assert_cmpuint(fdcan->tx_watch_tag, ==, 0);
    g_assert_cmpuint(fdcan->tx_next_ns, ==, 0);
    g_assert_null(chr->be);
    g_assert_false(qemu_clock_has_timers(QEMU_CLOCK_VIRTUAL));
    object_unparent(OBJECT(chr));
    g_assert_cmpint(access(path, F_OK), ==, -1);
    g_assert_cmpint(errno, ==, ENOENT);
    g_assert_cmpint(rmdir(directory), ==, 0);
}

static void test_tx_native_socket_reconnect(void)
{
    g_autofree char *directory =
        g_strdup_printf("%s/dm-qemu.fdcan.XXXXXX", g_get_tmp_dir());
    g_autofree char *path = NULL;
    DmMc02Fdcan fdcan;
    uint8_t ram[16], wire[84], expected[84], extra;
    Chardev *chr = init_socket_fixture(&fdcan, ram, directory, &path);
    GSource *old_watch;
    unsigned attempt;
    int peer;
    int64_t deadline;

    peer = connect_socket_peer(path);
    wait_socket_state(chr, &fdcan, true);

    /* Stop at the first pending record, before queue admission drops. */
    deadline = g_get_monotonic_time() + 2 * G_USEC_PER_SEC;
    for (attempt = 0; attempt < 65536; ++attempt) {
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
        fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
        if (fdcan.tx_queue_count) {
            break;
        }
    }
    g_assert_cmpuint(attempt, >, 0);
    g_assert_cmpuint(attempt, <, 65536);
    g_assert_true(object_property_get_bool(OBJECT(chr), "connected",
                                           &error_abort));
    g_assert_true(qemu_chr_fe_backend_open(&fdcan.chr));
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 1);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 0);
    g_assert_cmpuint(fdcan.tx_queue_offset[fdcan.tx_queue_head], <, 84);
    g_assert_cmpuint(fdcan.tx_watch_tag, !=, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    old_watch = g_main_context_find_source_by_id(NULL, fdcan.tx_watch_tag);
    g_assert_nonnull(old_watch);
    g_source_ref(old_watch);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 1001000);
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 1000);
    g_test_message("Native socket pressure after %u frames; pending offset %u",
                    attempt + 1, fdcan.tx_queue_offset[fdcan.tx_queue_head]);
    memcpy(expected, expected_wire, sizeof(expected));
    expected[20] = 0xa5;
    /* Peek proves an old complete record arrived without relieving pressure. */
    g_assert_cmpint(recv(peer, wire, sizeof(wire), MSG_PEEK), ==, 84);
    g_assert_cmpmem(wire, sizeof(wire), expected, sizeof(expected));

    g_assert_cmpint(close(peer), ==, 0);
    wait_socket_state(chr, &fdcan, false);
    /* Native CLOSED must purge before any reconnect, TX or virtual timer. */
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_queue_head, ==, 0);
    for (unsigned i = 0; i < G_N_ELEMENTS(fdcan.tx_queue_offset); ++i) {
        g_assert_cmpuint(fdcan.tx_queue_offset[i], ==, 0);
    }
    g_assert_cmpuint(fdcan.tx_dropped, ==, 1);
    g_assert_cmpuint(fdcan.tx_watch_tag, ==, 0);
    g_assert_true(g_source_is_destroyed(old_watch));
    g_source_unref(old_watch);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 1000);

    peer = connect_socket_peer(path);
    wait_socket_state(chr, &fdcan, true);
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 1);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpint(recv(peer, &extra, 1, 0), ==, -1);
    g_assert_cmpint(errno, ==, EAGAIN);
    ram[8] = 0x55;
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    recv_socket_wire(peer, wire);
    expected[20] = 0x55;
    g_assert_cmpmem(wire, sizeof(wire), expected, sizeof(expected));
    g_assert_cmpuint(fdcan.tx_queue_count, ==, 0);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 1);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 1000);
    g_assert_cmpint(recv(peer, &extra, 1, 0), ==, -1);
    g_assert_cmpint(errno, ==, EAGAIN);

    g_assert_cmpint(close(peer), ==, 0);
    wait_socket_state(chr, &fdcan, false);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 1);
    cleanup_socket_fixture(&fdcan, chr, directory, path);
}

static gboolean socket_read_wakeup(GIOChannel *channel, GIOCondition condition,
                                    gpointer opaque)
{
    return G_SOURCE_CONTINUE;
}

static void test_tx_native_socket_writable_drain(void)
{
    g_autofree char *directory =
        g_strdup_printf("%s/dm-qemu.fdcan.XXXXXX", g_get_tmp_dir());
    g_autofree char *path = NULL;
    DmMc02Fdcan fdcan;
    DmMc02FdcanTxState tx_state;
    uint8_t ram[16], wire[84], expected[84], extra;
    Chardev *chr = init_socket_fixture(&fdcan, ram, directory, &path);
    int peer = connect_socket_peer(path);
    unsigned count, received = 0, offset = 0;
    int64_t deadline;
    GIOChannel *reader;
    GSource *read_wakeup, *timeout;

    wait_socket_state(chr, &fdcan, true);
    deadline = g_get_monotonic_time() + 2 * G_USEC_PER_SEC;
    for (count = 0; count < 65536; ++count) {
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
        ram[8] = count;
        ram[9] = count >> 8;
        fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
        g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
        if (tx_state.pending_frames) {
            break;
        }
    }
    g_assert_cmpuint(count, >, 0);
    g_assert_cmpuint(count, <, 65536);
    count++; /* Include the one pending record; do not submit another TX. */
    g_assert_cmpuint(tx_state.pending_frames, ==, 1);
    g_assert_cmpuint(tx_state.pending_bytes, >, 0);
    g_assert_cmpuint(tx_state.pending_bytes, <=, 84);
    g_assert_cmpuint(tx_state.tx_dropped, ==, 0);
    g_assert_cmpuint(fdcan.tx_watch_tag, !=, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 1001000);
    g_test_message("Native fixed-clock drain of %u records; pending bytes %u",
                    count, tx_state.pending_bytes);

    /* One reader; native IN only wakes the same loop that also services OUT. */
    reader = g_io_channel_unix_new(peer);
    read_wakeup = g_io_create_watch(reader, G_IO_IN | G_IO_HUP);
    g_source_set_callback(read_wakeup, (GSourceFunc)socket_read_wakeup,
                           NULL, NULL);
    g_source_attach(read_wakeup, NULL);
    deadline = g_get_monotonic_time() + 2 * G_USEC_PER_SEC;
    timeout = g_timeout_source_new(2000);
    g_source_set_callback(timeout, socket_deadline_wakeup, NULL, NULL);
    g_source_set_ready_time(timeout, deadline);
    g_source_attach(timeout, NULL);
    while (received < count) {
        ssize_t length;

        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
        length = recv(peer, wire + offset, sizeof(wire) - offset, 0);
        if (length < 0 && errno == EINTR) {
            continue;
        }
        if (length < 0 && errno == EAGAIN) {
            g_main_context_iteration(NULL, TRUE);
        } else {
            g_assert_cmpint(length, >, 0);
            offset += length;
            if (offset == sizeof(wire)) {
                memcpy(expected, expected_wire, sizeof(expected));
                expected[20] = received;
                expected[21] = received >> 8;
                g_assert_cmpmem(wire, sizeof(wire), expected, sizeof(expected));
                received++;
                offset = 0;
            }
            g_main_context_iteration(NULL, FALSE);
        }
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
        g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 1000);
    }
    g_source_destroy(timeout);
    g_source_unref(timeout);
    g_source_destroy(read_wakeup);
    g_source_unref(read_wakeup);
    g_io_channel_unref(reader);
    g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
    g_assert_cmpuint(tx_state.pending_frames, ==, 0);
    g_assert_cmpuint(tx_state.pending_bytes, ==, 0);
    g_assert_cmpuint(tx_state.tx_dropped, ==, 0);
    g_assert_cmpuint(fdcan.tx_watch_tag, ==, 0);
    g_assert_cmpuint(fdcan.tx_next_ns, ==, 0);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_true(qemu_chr_fe_backend_open(&fdcan.chr));
    g_assert_cmpint(recv(peer, &extra, 1, 0), ==, -1);
    g_assert_cmpint(errno, ==, EAGAIN);
    g_assert_cmpint(close(peer), ==, 0);
    wait_socket_state(chr, &fdcan, false);
    g_assert_cmpuint(fdcan.tx_dropped, ==, 0);
    cleanup_socket_fixture(&fdcan, chr, directory, path);
}
#endif

typedef struct IrqCapture {
    unsigned calls;
    int level;
} IrqCapture;

static void put_le32(uint8_t *where, uint32_t value)
{
    for (unsigned i = 0; i < sizeof(value); ++i) {
        where[i] = value >> (i * 8);
    }
}

static QEMUFile *open_test_file(bool write)
{
    int fd = dup(temp_fd);
    QIOChannel *ioc;
    QEMUFile *file;

    g_assert_cmpint(fd, >=, 0);
    g_assert_cmpint(lseek(fd, 0, SEEK_SET), ==, 0);
    if (write) {
        g_assert_cmpint(ftruncate(fd, 0), ==, 0);
    }
    ioc = QIO_CHANNEL(qio_channel_file_new_fd(fd));
    file = write ? qemu_file_new_output(ioc) : qemu_file_new_input(ioc);
    object_unref(OBJECT(ioc));
    return file;
}

static void save_state_v(const DmMc02Fdcan *state,
                         const VMStateDescription *description,
                         int version_id)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state_v(file, description, (void *)state,
                                         NULL, version_id, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static void save_state(const DmMc02Fdcan *state,
                       const VMStateDescription *description)
{
    save_state_v(state, description, description->version_id);
}

static int load_state_v(DmMc02Fdcan *state,
                        const VMStateDescription *description,
                        int version_id)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, description, state, version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static int load_state(DmMc02Fdcan *state,
                      const VMStateDescription *description)
{
    return load_state_v(state, description, description->version_id);
}

static void test_tx_writable_source_restore(void)
{
    const int partial[] = { 17 };
    const int suffix[] = { 67 };
    uint8_t ram[16];
    DmMc02Fdcan source;
    TxCaptureChardev *capture = init_tx_fixture(&source, ram, partial, 1);

    capture->watch_supported = true;
    source.iomem.ops->write(source.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(source.tx_watch_tag, !=, 0);
    save_state(&source, dm_mc02_fdcan_vmstate());
    cleanup_tx_fixture(&source, capture);

    /* Producer projection only: migration does not move the physical prefix. */
    for (unsigned mode = 0; mode < 3; ++mode) {
        DmMc02Fdcan restored;

        capture = init_tx_fixture(&restored, ram, suffix, 1);
        capture->watch_supported = mode != 2;
        g_assert_cmpint(load_state(&restored, mode == 1 ?
                                   dm_mc02_fdcan_vmstate_raw() :
                                   dm_mc02_fdcan_vmstate()), ==, 0);
        g_assert_cmpuint(capture->calls, ==, 0);
        g_assert_cmpuint(restored.tx_queue_count, ==, 1);
        g_assert_cmpuint(restored.tx_queue_offset[restored.tx_queue_head],
                          ==, 17);
        g_assert_cmpuint(restored.tx_next_ns, ==, 1001000);
        if (mode == 1) {
            g_assert_cmpuint(restored.tx_watch_tag, ==, 0);
            g_assert_cmpuint(capture->watch_created, ==, 0);
            g_assert_false(timer_pending(restored.tx_timer));
            g_main_context_iteration(NULL, FALSE);
            g_assert_cmpuint(capture->calls, ==, 0);
            dm_mc02_fdcan_sync_runtime(&restored);
        } else if (mode == 0) {
            GSource *prior = g_source_ref(capture->watch_source);

            g_assert_cmpuint(capture->watch_created, ==, 1);
            dm_mc02_fdcan_sync_runtime(&restored);
            g_assert_true(g_source_is_destroyed(prior));
            g_source_unref(prior);
        }
        g_assert_cmpuint(capture->calls, ==, 0);
        if (mode < 2) {
            g_assert_cmpuint(restored.tx_watch_tag, !=, 0);
            g_assert_false(timer_pending(restored.tx_timer));
            g_assert_cmpuint(capture->watch_created, ==, mode == 0 ? 2 : 1);
            g_source_set_ready_time(capture->watch_source, 0);
            g_assert_true(g_main_context_iteration(NULL, FALSE));
            g_assert_true(g_source_is_destroyed(capture->watch_source));
            g_assert_cmpint(test_clock_ns, ==, 1000);
        } else {
            g_assert_cmpuint(restored.tx_watch_tag, ==, 0);
            g_assert_true(timer_pending(restored.tx_timer));
            test_clock_ns = 1000999;
            g_assert_false(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
            g_assert_cmpuint(capture->calls, ==, 0);
            test_clock_ns = 1001000;
            g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
        }
        g_assert_cmpuint(capture->calls, ==, 1);
        assert_attempt(capture, 0, expected_wire + 17, 67);
        g_assert_cmpuint(capture->accepted_length, ==, 67);
        g_assert_cmpmem(capture->accepted, 67, expected_wire + 17, 67);
        g_assert_cmpuint(restored.tx_queue_count, ==, 0);
        g_assert_cmpuint(restored.tx_watch_tag, ==, 0);
        g_assert_cmpuint(restored.tx_next_ns, ==, 0);
        cleanup_tx_fixture(&restored, capture);
    }
}

static void test_tx_writable_source_restore_reject(void)
{
    /* Invalid normal state and truncated raw input cannot create runtime IO. */
    for (unsigned mode = 0; mode < 2; ++mode) {
        const int results[] = { 17 };
        uint8_t ram[16];
        DmMc02Fdcan source, restored;
        TxCaptureChardev *capture = init_tx_fixture(&source, ram, results, 1);
        const VMStateDescription *description = mode == 0 ?
            dm_mc02_fdcan_vmstate() : dm_mc02_fdcan_vmstate_raw();

        capture->watch_supported = true;
        source.iomem.ops->write(source.iomem.opaque, FDCAN_TXBAR, 1, 4);
        if (mode == 0) {
            source.tx_queue_offset[source.tx_queue_head] = 84;
        }
        save_state(&source, description);
        cleanup_tx_fixture(&source, capture);
        if (mode == 1) {
            g_assert_cmpint(ftruncate(temp_fd, 7), ==, 0);
        }
        capture = init_tx_fixture(&restored, ram, results, 0);
        capture->watch_supported = true;
        g_assert_cmpint(load_state(&restored, description), !=, 0);
        g_assert_cmpuint(restored.tx_watch_tag, ==, 0);
        g_assert_cmpuint(capture->watch_created, ==, 0);
        g_assert_false(timer_pending(restored.tx_timer));
        g_main_context_iteration(NULL, FALSE);
        g_assert_cmpuint(capture->calls, ==, 0);
        cleanup_tx_fixture(&restored, capture);
    }
}

static void capture_irq(void *opaque, int n, int level)
{
    IrqCapture *capture = opaque;

    (void)n;
    capture->calls++;
    capture->level = level;
}

static void test_tx_state_observation(void)
{
    const int results[] = { 17, 0, -1 };
    uint8_t ram[16];
    DmMc02Fdcan fdcan, before;
    DmMc02FdcanTxState tx_state;
    TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results,
                                               G_N_ELEMENTS(results));
    TxCaptureChardev capture_before;
    IrqCapture irq_capture = { 0 }, irq_before;
    qemu_irq irq = qemu_allocate_irq(capture_irq, &irq_capture, 0);
    uint64_t timer_expire;
    guint source_id;

    capture->watch_supported = true;
    dm_mc02_fdcan_set_irq(&fdcan, 0, irq);
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_IE, 1u << 9, 4);
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_ILE, 1, 4);
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(capture->calls, ==, 1);
    assert_attempt(capture, 0, expected_wire, 84);
    g_assert_cmpint(irq_capture.level, ==, 1);
    g_assert_cmpuint(fdcan.tx_watch_tag, !=, 0);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpint(g_source_get_ready_time(capture->watch_source), ==, -1);

    /* Repeated observations cannot progress an armed but unready suffix. */
    memcpy(&before, &fdcan, sizeof(before));
    memcpy(&capture_before, capture, sizeof(capture_before));
    irq_before = irq_capture;
    source_id = g_source_get_id(capture->watch_source);
    timer_expire = timer_expire_time_ns(fdcan.tx_timer);
    for (unsigned query = 0; query < 3; ++query) {
        g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
        g_assert_cmpuint(tx_state.pending_frames, ==, 1);
        g_assert_cmpuint(tx_state.pending_bytes, ==, 67);
        g_assert_cmpuint(tx_state.tx_dropped, ==, 0);
        g_assert_cmpuint(tx_state.tx_short_writes, ==, 1);
    }
    g_assert_cmpmem(&fdcan, sizeof(fdcan), &before, sizeof(before));
    g_assert_cmpmem(capture, sizeof(*capture),
                    &capture_before, sizeof(capture_before));
    g_assert_cmpuint(irq_capture.calls, ==, irq_before.calls);
    g_assert_cmpint(irq_capture.level, ==, irq_before.level);
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 1000);
    g_assert_false(timer_pending(fdcan.tx_timer));
    g_assert_cmpuint(timer_expire_time_ns(fdcan.tx_timer), ==, timer_expire);
    g_assert_cmpuint(g_source_get_id(capture->watch_source), ==, source_id);
    g_assert_cmpint(g_source_get_ready_time(capture->watch_source), ==, -1);
    g_assert_false(g_source_is_destroyed(capture->watch_source));

    for (unsigned retry = 1; retry <= 2; ++retry) {
        g_source_set_ready_time(capture->watch_source, 0);
        g_assert_true(g_main_context_iteration(NULL, FALSE));
        g_assert_cmpuint(capture->calls, ==, retry + 1);
        assert_attempt(capture, retry, expected_wire + 17, 67);
        g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
        g_assert_cmpuint(tx_state.pending_frames, ==, 1);
        g_assert_cmpuint(tx_state.pending_bytes, ==, 67);
        g_assert_cmpuint(tx_state.tx_short_writes, ==, 1);
    }
    fdcan.iomem.ops->write(fdcan.iomem.opaque, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(capture->calls, ==, 4);
    assert_attempt(capture, 3, expected_wire + 17, 67);
    g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
    g_assert_cmpuint(tx_state.pending_frames, ==, 2);
    g_assert_cmpuint(tx_state.pending_bytes, ==, 151);

    capture->writable = true;
    g_source_set_ready_time(capture->watch_source, 0);
    g_assert_true(g_main_context_iteration(NULL, FALSE));
    g_assert_cmpuint(capture->calls, ==, 6);
    assert_attempt(capture, 4, expected_wire + 17, 67);
    assert_attempt(capture, 5, expected_wire, 84);
    g_assert_cmpmem(capture->accepted, 84, expected_wire, 84);
    g_assert_cmpmem(capture->accepted + 84, 84, expected_wire, 84);
    g_assert_cmpuint(fdcan.tx_queue_offset[0], ==, 84);
    g_assert_cmpuint(fdcan.tx_queue_offset[1], ==, 84);
    g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
    g_assert_cmpuint(tx_state.pending_frames, ==, 0);
    g_assert_cmpuint(tx_state.pending_bytes, ==, 0);
    g_assert_cmpuint(tx_state.tx_dropped, ==, 0);
    g_assert_cmpuint(tx_state.tx_short_writes, ==, 1);
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 1000);

    dm_mc02_fdcan_reset(&fdcan);
    g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
    g_assert_cmpuint(tx_state.pending_frames, ==, 0);
    g_assert_cmpuint(tx_state.pending_bytes, ==, 0);
    g_assert_cmpuint(tx_state.tx_dropped, ==, 0);
    g_assert_cmpuint(tx_state.tx_short_writes, ==, 0);
    cleanup_tx_fixture(&fdcan, capture);
    qemu_free_irq(irq);
}

static void test_tx_state_geometry(void)
{
    DmMc02Fdcan fdcan = {
        .tx_queue_head = 15,
        .tx_queue_count = 2,
        .tx_queue_offset = { [15] = 17, [4] = 84 },
        .tx_dropped = UINT64_C(0x8000000000000001),
        .tx_short_writes = UINT64_MAX,
        /* Unrelated controller state is outside this observation contract. */
        .rx_wire_len = UINT16_MAX,
    };
    DmMc02Fdcan before;
    DmMc02FdcanTxState tx_state, untouched;

    g_assert_true(dm_mc02_fdcan_get_tx_state(&fdcan, &tx_state));
    g_assert_cmpuint(tx_state.pending_frames, ==, 2);
    g_assert_cmpuint(tx_state.pending_bytes, ==, 151);
    g_assert_cmpuint(tx_state.tx_dropped, ==,
                     UINT64_C(0x8000000000000001));
    g_assert_cmpuint(tx_state.tx_short_writes, ==, UINT64_MAX);
    memcpy(&before, &fdcan, sizeof(before));
    g_assert_false(dm_mc02_fdcan_get_tx_state(&fdcan, NULL));
    g_assert_cmpmem(&fdcan, sizeof(fdcan), &before, sizeof(before));
    memset(&tx_state, 0xa5, sizeof(tx_state));
    memcpy(&untouched, &tx_state, sizeof(untouched));
    g_assert_false(dm_mc02_fdcan_get_tx_state(NULL, &tx_state));
    g_assert_cmpmem(&tx_state, sizeof(tx_state),
                    &untouched, sizeof(untouched));

    for (unsigned invalid = 0; invalid < 4; ++invalid) {
        DmMc02Fdcan bad = fdcan;

        switch (invalid) {
        case 0:
            bad.tx_queue_head = 16;
            break;
        case 1:
            bad.tx_queue_count = 17;
            break;
        case 2:
            bad.tx_queue_offset[15] = 84; /* Active record must be pending. */
            break;
        case 3:
            bad.tx_queue_offset[4] = 85; /* Inactive offset still bounded. */
            break;
        }
        g_assert_false(dm_mc02_fdcan_get_tx_state(&bad, &tx_state));
        g_assert_cmpmem(&tx_state, sizeof(tx_state),
                        &untouched, sizeof(untouched));
    }
}

static void assert_rx_state(const DmMc02Fdcan *fdcan, uint64_t completed,
                            unsigned partial, uint64_t dropped)
{
    DmMc02FdcanRxState state;

    g_assert_true(dm_mc02_fdcan_get_rx_state(fdcan, &state));
    g_assert_cmpuint(state.completed_frames, ==, completed);
    g_assert_cmpuint(state.partial_bytes, ==, partial);
    g_assert_cmpuint(state.rx_dropped, ==, dropped);
}

/*
 * RM0468 RX element: standard ID 0x200 at bits 28:18, DLC 8 at
 * bits 19:16, ANMF at bit 31 for global nonmatching FIFO0 acceptance.
 * This no-filter profile reports FIDX=0x7f in bits 30:24.
 * The fixed host timestamp is not an RX scheduling or storage timestamp.
 */
static const uint8_t expected_rx_element[16] = {
    0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x08, 0xff,
    0x00, 0x40, 0xff, 0xc0, 0x00, 0x00, 0x00, 0x00,
};

static TxCaptureChardev *init_rx_fixture(DmMc02Fdcan *fdcan, uint8_t *ram)
{
    const int results[] = { 0 };
    TxCaptureChardev *capture = init_tx_fixture(fdcan, ram, results, 0);

    /* One 16-byte FIFO0 element at byte offset zero; GFC reset accepts. */
    fdcan->iomem.ops->write(fdcan, FDCAN_RXF0C, 0x00010000, 4);
    fdcan->iomem.ops->write(fdcan, FDCAN_IE, 1, 4); /* RF0NE */
    fdcan->iomem.ops->write(fdcan, FDCAN_ILE, 1, 4);
    return capture;
}

static void test_rx_completed_fragments(void)
{
    uint8_t ram[16];
    DmMc02Fdcan fdcan;
    TxCaptureChardev *capture = init_rx_fixture(&fdcan, ram);
    IrqCapture irq_capture = { 0 };
    qemu_irq irq = qemu_allocate_irq(capture_irq, &irq_capture, 0);

    dm_mc02_fdcan_set_irq(&fdcan, 0, irq);
    assert_rx_state(&fdcan, 0, 0, 0);
    qemu_chr_be_write(CHARDEV(capture), expected_wire, 19);
    assert_rx_state(&fdcan, 0, 19, 0);
    qemu_chr_be_write(CHARDEV(capture), expected_wire + 19, 64);
    assert_rx_state(&fdcan, 0, 83, 0);
    g_assert_cmpint(qemu_chr_be_can_write(CHARDEV(capture)), ==, 1);
    g_assert_cmpmem(ram, sizeof(ram), tx_element, sizeof(tx_element));
    g_assert_cmphex(fdcan.iomem.ops->read(&fdcan, FDCAN_RXF0S, 4) & 0x7f,
                    ==, 0);
    g_assert_cmphex(fdcan.iomem.ops->read(&fdcan, FDCAN_IR, 4), ==, 0);
    g_assert_cmpint(irq_capture.level, ==, 0);

    qemu_chr_be_write(CHARDEV(capture), expected_wire + 83, 1);
    /* A receipt is visible only after the actual RAM and IRQ dispatch. */
    assert_rx_state(&fdcan, 1, 0, 0);
    g_assert_cmpmem(ram, sizeof(ram), expected_rx_element,
                    sizeof(expected_rx_element));
    g_assert_cmphex(fdcan.iomem.ops->read(&fdcan, FDCAN_RXF0S, 4) & 0x7f,
                    ==, 1);
    g_assert_cmphex(fdcan.iomem.ops->read(&fdcan, FDCAN_IR, 4), ==, 1);
    g_assert_cmpint(irq_capture.level, ==, 1);
    g_assert_cmpuint(capture->calls, ==, 0);
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 1000);
    cleanup_tx_fixture(&fdcan, capture);
    qemu_free_irq(irq);
}

static void test_rx_completed_rejections_saturation(void)
{
    uint8_t ram[16], malformed[84];
    DmMc02Fdcan fdcan;
    TxCaptureChardev *capture = init_rx_fixture(&fdcan, ram);

    qemu_chr_be_write(CHARDEV(capture), expected_wire, 84);
    qemu_chr_be_write(CHARDEV(capture), expected_wire, 84); /* FIFO full */
    assert_rx_state(&fdcan, 2, 0, 1);
    memcpy(malformed, expected_wire, sizeof(malformed));
    malformed[9] = 1; /* Reserved host-wire byte, not a controller frame. */
    qemu_chr_be_write(CHARDEV(capture), malformed, sizeof(malformed));
    assert_rx_state(&fdcan, 3, 0, 2);
    fdcan.iomem.ops->write(&fdcan, FDCAN_CCCR, 1, 4); /* INIT rejects. */
    qemu_chr_be_write(CHARDEV(capture), expected_wire, 84);
    assert_rx_state(&fdcan, 4, 0, 3);
    g_assert_cmpmem(ram, sizeof(ram), expected_rx_element,
                    sizeof(expected_rx_element));

    fdcan.rx_wire_completed = UINT64_MAX - 1;
    qemu_chr_be_write(CHARDEV(capture), malformed, sizeof(malformed));
    assert_rx_state(&fdcan, UINT64_MAX, 0, 4);
    qemu_chr_be_write(CHARDEV(capture), expected_wire, 84);
    assert_rx_state(&fdcan, UINT64_MAX, 0, 5);
    cleanup_tx_fixture(&fdcan, capture);
}

static void test_rx_state_observation(void)
{
    const int results[] = { 17 };
    uint8_t ram[16], ram_before[16];
    DmMc02Fdcan fdcan, before;
    DmMc02Fdcan unbound;
    TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results, 1);
    TxCaptureChardev capture_before;
    IrqCapture irq_capture = { 0 }, irq_before;
    qemu_irq irq = qemu_allocate_irq(capture_irq, &irq_capture, 0);
    int64_t timer_expire;

    g_assert_null(dm_mc02_fdcan_get_chardev(NULL));
    g_assert_true(dm_mc02_fdcan_init(&unbound, NULL, "test-fdcan-unbound",
                                     DM_MC02_FDCAN_REGION_SIZE, ram,
                                     sizeof(ram), NULL, &error_abort));
    g_assert_null(dm_mc02_fdcan_get_chardev(&unbound));
    dm_mc02_fdcan_cleanup(&unbound);
    g_assert_null(dm_mc02_fdcan_get_chardev(&unbound));
    dm_mc02_fdcan_set_irq(&fdcan, 0, irq);
    fdcan.iomem.ops->write(&fdcan, FDCAN_IE, 1u << 9, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_ILE, 1, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_TXBAR, 1, 4);
    qemu_chr_be_write(CHARDEV(capture), expected_wire, 17);
    g_assert_cmpuint(capture->calls, ==, 1);
    g_assert_true(timer_pending(fdcan.tx_timer));
    g_assert_cmpint(irq_capture.level, ==, 1);
    memcpy(&before, &fdcan, sizeof(before));
    memcpy(&capture_before, capture, sizeof(capture_before));
    memcpy(ram_before, ram, sizeof(ram_before));
    irq_before = irq_capture;
    timer_expire = timer_expire_time_ns(fdcan.tx_timer);

    for (unsigned query = 0; query < 3; ++query) {
        /* Borrowed identity must not ref the backend or service either IO. */
        g_assert_true(dm_mc02_fdcan_get_chardev(&fdcan) == CHARDEV(capture));
        assert_rx_state(&fdcan, 0, 17, 0);
    }
    g_assert_false(dm_mc02_fdcan_get_rx_state(&fdcan, NULL));
    g_assert_cmpmem(&fdcan, sizeof(fdcan), &before, sizeof(before));
    g_assert_cmpmem(capture, sizeof(*capture),
                    &capture_before, sizeof(capture_before));
    g_assert_cmpmem(ram, sizeof(ram), ram_before, sizeof(ram_before));
    g_assert_cmpuint(irq_capture.calls, ==, irq_before.calls);
    g_assert_cmpint(irq_capture.level, ==, irq_before.level);
    g_assert_true(timer_pending(fdcan.tx_timer));
    g_assert_cmpint(timer_expire_time_ns(fdcan.tx_timer), ==, timer_expire);
    g_assert_cmpint(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), ==, 1000);
    cleanup_tx_fixture(&fdcan, capture);
    g_assert_null(dm_mc02_fdcan_get_chardev(&fdcan));
    qemu_free_irq(irq);
}

static void test_rx_state_geometry(void)
{
    DmMc02Fdcan fdcan = {
        .rx_wire_completed = UINT64_C(0x8000000000000001),
        .rx_wire_len = 83,
        .rx_dropped = UINT64_MAX,
        /* TX geometry is outside this observation contract. */
        .tx_queue_head = UINT8_MAX,
    };
    DmMc02FdcanRxState state, untouched;
    const uint16_t invalid[] = { 84, UINT16_MAX };

    assert_rx_state(&fdcan, UINT64_C(0x8000000000000001), 83, UINT64_MAX);
    memset(&state, 0xa5, sizeof(state));
    memcpy(&untouched, &state, sizeof(untouched));
    g_assert_false(dm_mc02_fdcan_get_rx_state(NULL, &state));
    g_assert_cmpmem(&state, sizeof(state), &untouched, sizeof(untouched));
    for (unsigned i = 0; i < G_N_ELEMENTS(invalid); ++i) {
        fdcan.rx_wire_len = invalid[i];
        g_assert_false(dm_mc02_fdcan_get_rx_state(&fdcan, &state));
        g_assert_cmpmem(&state, sizeof(state), &untouched, sizeof(untouched));
    }
}

static void test_rx_completed_lifecycle(void)
{
    for (unsigned mode = 0; mode < 2; ++mode) {
        const int results[] = { 0 };
        uint8_t ram[16];
        DmMc02Fdcan fdcan;
        TxCaptureChardev *capture = init_tx_fixture(&fdcan, ram, results, 0);

        /* No RX storage: receipt counts dispatch, independently of storage. */
        qemu_chr_be_write(CHARDEV(capture), expected_wire, 84);
        qemu_chr_be_write(CHARDEV(capture), expected_wire, 13);
        assert_rx_state(&fdcan, 1, 13, 1);
        if (mode == 0) {
            qemu_chr_be_event(CHARDEV(capture), CHR_EVENT_CLOSED);
        } else {
            dm_mc02_fdcan_set_powered(&fdcan, false);
        }
        assert_rx_state(&fdcan, 1, 0, 1);
        g_assert_cmpint(qemu_chr_be_can_write(CHARDEV(capture)), ==, 84);
        if (mode == 0) {
            qemu_chr_be_event(CHARDEV(capture), CHR_EVENT_OPENED);
        } else {
            dm_mc02_fdcan_set_powered(&fdcan, true);
        }
        assert_rx_state(&fdcan, 1, 0, 1);
        qemu_chr_be_write(CHARDEV(capture), expected_wire, 84);
        assert_rx_state(&fdcan, 2, 0, 2);
        qemu_chr_be_write(CHARDEV(capture), expected_wire, 13);
        dm_mc02_fdcan_reset(&fdcan);
        assert_rx_state(&fdcan, 0, 0, 0);
        qemu_chr_be_write(CHARDEV(capture), expected_wire, 84);
        assert_rx_state(&fdcan, 1, 0, 1); /* Reset leaves INIT asserted. */
        cleanup_tx_fixture(&fdcan, capture);
    }
}

static void test_rx_native_bus_not_wire(void)
{
    uint8_t ram[16], sender_ram[16];
    DmMc02Fdcan fdcan, sender;
    TxCaptureChardev *capture = init_rx_fixture(&fdcan, ram);
    CanBusState *bus = CAN_BUS(object_new(TYPE_CAN_BUS));

    memcpy(sender_ram, tx_element, sizeof(sender_ram));
    g_assert_true(dm_mc02_fdcan_init(&sender, NULL, "test-fdcan-sender",
                                     DM_MC02_FDCAN_REGION_SIZE, sender_ram,
                                     sizeof(sender_ram), NULL, &error_abort));
    sender.iomem.ops->write(&sender, FDCAN_TXBC, 0x01000000, 4);
    sender.iomem.ops->write(&sender, FDCAN_CCCR, 0, 4);
    dm_mc02_fdcan_set_canbus(&fdcan, bus);
    dm_mc02_fdcan_set_canbus(&sender, bus);
    sender.iomem.ops->write(&sender, FDCAN_TXBAR, 1, 4);
    g_assert_cmpmem(ram, sizeof(ram), expected_rx_element,
                    sizeof(expected_rx_element));
    g_assert_cmphex(fdcan.iomem.ops->read(&fdcan, FDCAN_IR, 4), ==, 1);
    assert_rx_state(&fdcan, 0, 0, 0);
    assert_rx_state(&sender, 0, 0, 0);
    dm_mc02_fdcan_cleanup(&sender);
    cleanup_tx_fixture(&fdcan, capture);
    object_unref(OBJECT(bus));
}

static void prepare_source(DmMc02Fdcan *state)
{
    memset(state, 0, sizeof(*state));
    put_le32(state->regs + FDCAN_RXF0C, 4u << 16);
    put_le32(state->regs + FDCAN_RXF1C, 4u << 16);
    put_le32(state->regs + FDCAN_TXBC, 4u << 24);
    put_le32(state->regs + FDCAN_IR, 1);
    put_le32(state->regs + FDCAN_IE, 1);
    put_le32(state->regs + FDCAN_ILE, 1);
    state->rx_wire_len = 13;
    for (unsigned i = 0; i < state->rx_wire_len; ++i) {
        state->rx_wire[i] = 0x10 + i;
    }
    state->rx_fifo_fill = 2;
    state->rx_fifo_get = 1;
    state->rx_fifo_put = 3;
    state->rx_fifo1_fill = 1;
    state->rx_fifo1_get = 2;
    state->rx_fifo1_put = 3;
    state->tx_fifo_put = 2;
    state->tx_fifo_get = 1;
    state->rx_fifo_configured = true;
    state->rx_buffer_configured = true;
    state->tx_fifo_configured = true;
    state->tx_pending_mask = 3;
    state->rx_dropped = 17;
    state->tx_dropped = 19;
    state->tx_no_ack = 23;
    state->tx_queue_head = 14;
    state->tx_queue_count = 3;
    state->tx_queue_offset[14] = 5;
    state->tx_queue_offset[15] = 9;
    state->tx_queue_offset[0] = 2;
    state->tx_short_writes = 29;
    state->tx_next_ns = 123456789;
    for (unsigned i = 0; i < DM_MC02_FDCAN_TX_QUEUE_SIZE;
         ++i) {
        for (unsigned j = 0; j < DM_MC02_CAN_WIRE_SIZE; ++j) {
            state->tx_queue[i][j] = i ^ j;
        }
    }
}

static void test_round_trip_restores_state_and_irq_projection(void)
{
    DmMc02Fdcan source;
    DmMc02Fdcan restored;
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source);
    save_state(&source, dm_mc02_fdcan_vmstate());

    memset(&restored, 0, sizeof(restored));
    restored.irq_level[0] = false;
    restored.irq_level_valid[0] = true;
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    restored.irq[0] = irq;
    g_assert_cmpint(load_state(&restored, dm_mc02_fdcan_vmstate()), ==, 0);

    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpmem(restored.rx_wire, sizeof(restored.rx_wire), source.rx_wire,
                    sizeof(source.rx_wire));
    g_assert_cmpuint(restored.rx_wire_len, ==, source.rx_wire_len);
    g_assert_cmpuint(restored.rx_fifo_fill, ==, source.rx_fifo_fill);
    g_assert_cmpuint(restored.rx_fifo_get, ==, source.rx_fifo_get);
    g_assert_cmpuint(restored.rx_fifo_put, ==, source.rx_fifo_put);
    g_assert_cmpuint(restored.rx_fifo1_fill, ==, source.rx_fifo1_fill);
    g_assert_cmpuint(restored.tx_pending_mask, ==, source.tx_pending_mask);
    g_assert_cmpmem(restored.tx_queue, sizeof(restored.tx_queue),
                    source.tx_queue, sizeof(source.tx_queue));
    g_assert_cmpmem(restored.tx_queue_offset, sizeof(restored.tx_queue_offset),
                    source.tx_queue_offset, sizeof(source.tx_queue_offset));
    g_assert_cmpuint(restored.tx_queue_head, ==, source.tx_queue_head);
    g_assert_cmpuint(restored.tx_queue_count, ==, source.tx_queue_count);
    g_assert_cmpuint(restored.tx_next_ns, ==, source.tx_next_ns);
    g_assert_cmpuint(restored.rx_fifo_size, ==, 4);
    g_assert_true(restored.irq[0] == irq);
    g_assert_true(restored.irq_level_valid[0]);
    g_assert_true(restored.irq_level[0]);
    g_assert_cmpuint(capture.calls, ==, 1);
    g_assert_cmpint(capture.level, ==, 1);
    qemu_free_irq(irq);
}

static void test_rejects_invalid_fifo_state_before_irq_sync(void)
{
    DmMc02Fdcan source;
    DmMc02Fdcan restored;
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source);
    source.tx_pending_mask = 1u << 4;
    save_state(&source, dm_mc02_fdcan_vmstate());

    memset(&restored, 0, sizeof(restored));
    restored.irq_level_valid[0] = true;
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    restored.irq[0] = irq;
    g_assert_cmpint(load_state(&restored, dm_mc02_fdcan_vmstate()), !=, 0);
    g_assert_true(restored.irq_level_valid[0]);
    g_assert_cmpuint(capture.calls, ==, 0);

    const struct { unsigned reg; uint32_t value; } invalid_configs[] = {
        { FDCAN_RXF0C, 65u << 16 }, /* seven-bit count, exceeds 64 */
        { FDCAN_TXBC, 33u << 24 }, /* exceeds 32 TX slots */
        { FDCAN_TXBC, 1u << 16 },  /* dedicated TX unsupported */
        { FDCAN_TXBC, 1u << 16 | 4u << 24 }, /* mixed layout unsupported */
        { FDCAN_TXBC, 1u << 30 | 4u << 24 }, /* queue unsupported */
    };

    for (unsigned i = 0; i < G_N_ELEMENTS(invalid_configs); ++i) {
        prepare_source(&source);
        put_le32(source.regs + invalid_configs[i].reg,
                  invalid_configs[i].value);
        save_state(&source, dm_mc02_fdcan_vmstate_raw());
        g_assert_cmpint(load_state(&restored, dm_mc02_fdcan_vmstate_raw()),
                        !=, 0);
        g_assert_true(restored.irq_level_valid[0]);
        g_assert_cmpuint(capture.calls, ==, 0);
    }
    qemu_free_irq(irq);
}

static void test_rejects_truncated_state_before_irq_sync(void)
{
    DmMc02Fdcan source;
    DmMc02Fdcan restored;
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source);
    save_state(&source, dm_mc02_fdcan_vmstate());
    g_assert_cmpint(ftruncate(temp_fd, 7), ==, 0);

    memset(&restored, 0, sizeof(restored));
    restored.irq_level_valid[0] = true;
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    restored.irq[0] = irq;
    g_assert_cmpint(load_state(&restored, dm_mc02_fdcan_vmstate()), !=, 0);
    g_assert_true(restored.irq_level_valid[0]);
    g_assert_cmpuint(capture.calls, ==, 0);
    qemu_free_irq(irq);
}

static void test_bus_off_round_trip_and_old_layout_rejection(void)
{
    DmMc02Fdcan source;
    DmMc02Fdcan restored;

    prepare_source(&source);
    put_le32(source.regs + FDCAN_PSR, 1u << 7);
    source.bus_off = true;
    save_state(&source, dm_mc02_fdcan_vmstate());

    memset(&restored, 0, sizeof(restored));
    g_assert_cmpint(load_state(&restored, dm_mc02_fdcan_vmstate()), ==, 0);
    g_assert_true(restored.bus_off);
    g_assert_cmpuint(restored.regs[FDCAN_PSR] & (1u << 7), !=, 0);

    /* Both old normal/raw layouts must fail before reading producer state
     * or projecting IRQs; retaining their RAM bytes would misdecode them. */
    for (int version = 1; version <= 2; ++version) {
        const VMStateDescription *descriptions[] = {
            dm_mc02_fdcan_vmstate(), dm_mc02_fdcan_vmstate_raw(),
        };

        for (unsigned i = 0; i < G_N_ELEMENTS(descriptions); ++i) {
            memset(&restored, 0, sizeof(restored));
            restored.irq_level_valid[0] = true;
            g_assert_cmpint(load_state_v(&restored, descriptions[i], version),
                            !=, 0);
            g_assert_cmpuint(restored.regs[FDCAN_PSR], ==, 0);
            g_assert_true(restored.irq_level_valid[0]);
        }
    }
}

static void test_raw_description_has_no_runtime_projection(void)
{
    DmMc02Fdcan source;
    DmMc02Fdcan restored;
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source);
    save_state(&source, dm_mc02_fdcan_vmstate_raw());
    memset(&restored, 0, sizeof(restored));
    restored.irq_level_valid[0] = true;
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    restored.irq[0] = irq;
    g_assert_cmpint(load_state(&restored, dm_mc02_fdcan_vmstate_raw()), ==, 0);
    g_assert_true(restored.irq_level_valid[0]);
    g_assert_cmpuint(capture.calls, ==, 0);
    qemu_free_irq(irq);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file =
        g_strdup_printf("%s/dm-fdcan-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    type_register_static(&capture_type);
    g_assert_cmpint(qemu_init_main_loop(&error_abort), ==, 0);
    g_assert_cmpint(socket_init(), ==, 0);
    qemu_add_opts(&qemu_chardev_opts);
    qemu_clock_enable(QEMU_CLOCK_VIRTUAL, true);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-fdcan-vmstate/round-trip-irq",
                    test_round_trip_restores_state_and_irq_projection);
    g_test_add_func("/dm-fdcan-vmstate/reject-invalid-fifo",
                    test_rejects_invalid_fifo_state_before_irq_sync);
    g_test_add_func("/dm-fdcan-vmstate/reject-truncated",
                    test_rejects_truncated_state_before_irq_sync);
    g_test_add_func("/dm-fdcan-vmstate/bus-off-old-layout-reject",
                    test_bus_off_round_trip_and_old_layout_rejection);
    g_test_add_func("/dm-fdcan-vmstate/raw-no-projection",
                    test_raw_description_has_no_runtime_projection);
    g_test_add_func("/dm-fdcan-tx/shortwrite-retry", test_tx_shortwrite_retry);
    g_test_add_func("/dm-fdcan-tx/state-observation",
                    test_tx_state_observation);
    g_test_add_func("/dm-fdcan-tx/state-geometry", test_tx_state_geometry);
    g_test_add_func("/dm-fdcan-rx/completed-fragments",
                    test_rx_completed_fragments);
    g_test_add_func("/dm-fdcan-rx/completed-rejections-saturation",
                    test_rx_completed_rejections_saturation);
    g_test_add_func("/dm-fdcan-rx/state-observation",
                    test_rx_state_observation);
    g_test_add_func("/dm-fdcan-rx/state-geometry", test_rx_state_geometry);
    g_test_add_func("/dm-fdcan-rx/completed-lifecycle",
                    test_rx_completed_lifecycle);
    g_test_add_func("/dm-fdcan-rx/native-bus-not-wire",
                    test_rx_native_bus_not_wire);
    g_test_add_func("/dm-fdcan-tx/writable-source-retry",
                    test_tx_writable_source_retry);
    g_test_add_func("/dm-fdcan-tx/writable-source-single-owner",
                    test_tx_writable_source_single_owner);
    g_test_add_func("/dm-fdcan-tx/writable-source-errors",
                    test_tx_writable_source_errors);
    g_test_add_func("/dm-fdcan-tx/writable-source-terminal-cancel",
                    test_tx_writable_source_terminal_cancel);
    g_test_add_func("/dm-fdcan-tx/writable-source-restore",
                    test_tx_writable_source_restore);
    g_test_add_func("/dm-fdcan-tx/writable-source-restore-reject",
                    test_tx_writable_source_restore_reject);
    g_test_add_func("/dm-fdcan-tx/bounded-queue-newest-drop",
                    test_tx_bounded_queue_newest_drop);
    g_test_add_func("/dm-fdcan-tx/disconnect-drops-old-session",
                    test_tx_disconnect_drops_old_session);
    g_test_add_func("/dm-fdcan-tx/disconnect-during-write",
                    test_tx_disconnect_during_write);
#ifdef CONFIG_LINUX
    g_test_add_func("/dm-fdcan-tx/native-socket-reconnect",
                    test_tx_native_socket_reconnect);
    g_test_add_func("/dm-fdcan-tx/native-socket-writable-drain",
                    test_tx_native_socket_writable_drain);
#endif
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    for (QEMUClockType type = 0; type < QEMU_CLOCK_MAX; ++type) {
        g_assert_false(qemu_clock_has_timers(type));
    }
    timerlistgroup_deinit(&main_loop_tlg);
    return ret;
}
