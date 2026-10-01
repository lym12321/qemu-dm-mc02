/* Focused VMState contract test for the reusable FDCAN component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_fdcan.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

#define FDCAN_IR   0x050
#define FDCAN_IE   0x054
#define FDCAN_ILS  0x058
#define FDCAN_ILE  0x05c
#define FDCAN_RXF0C 0x0a0
#define FDCAN_RXF1C 0x0b0
#define FDCAN_TXBC  0x0c0
#define FDCAN_PSR   0x044

static int temp_fd;

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

static void capture_irq(void *opaque, int n, int level)
{
    IrqCapture *capture = opaque;

    (void)n;
    capture->calls++;
    capture->level = level;
}

static void prepare_source(DmMc02Fdcan *state)
{
    memset(state, 0, sizeof(*state));
    put_le32(state->regs + FDCAN_RXF0C, 4u << 16);
    put_le32(state->regs + FDCAN_RXF1C, 4u << 16);
    put_le32(state->regs + FDCAN_TXBC, 4u << 16);
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

static void test_bus_off_round_trip_and_v1_compatibility(void)
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

    /* Serialize with the pre-bus_off v1 field set.  The v1 loader derives the
     * internal participation gate from the already serialized PSR.BO bit. */
    save_state_v(&source, dm_mc02_fdcan_vmstate(), 1);
    memset(&restored, 0, sizeof(restored));
    g_assert_cmpint(load_state_v(&restored, dm_mc02_fdcan_vmstate(), 1), ==, 0);
    g_assert_true(restored.bus_off);
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
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-fdcan-vmstate/round-trip-irq",
                    test_round_trip_restores_state_and_irq_projection);
    g_test_add_func("/dm-fdcan-vmstate/reject-invalid-fifo",
                    test_rejects_invalid_fifo_state_before_irq_sync);
    g_test_add_func("/dm-fdcan-vmstate/reject-truncated",
                    test_rejects_truncated_state_before_irq_sync);
    g_test_add_func("/dm-fdcan-vmstate/bus-off-v1-compat",
                    test_bus_off_round_trip_and_v1_compatibility);
    g_test_add_func("/dm-fdcan-vmstate/raw-no-projection",
                    test_raw_description_has_no_runtime_projection);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
