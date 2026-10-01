/* Focused VMState contract test for the FDCAN/shared Message RAM boundary. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_fdcan_msg_ram_link.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

#define FDCAN_IR    0x050
#define FDCAN_IE    0x054
#define FDCAN_ILE   0x05c
#define FDCAN_SIDFC 0x084
#define FDCAN_XIDFC 0x088
#define FDCAN_RXESC 0x0bc
#define FDCAN_RXF0C 0x0a0
#define FDCAN_RXF1C 0x0b0
#define FDCAN_RXBC  0x0ac
#define FDCAN_TXBC  0x0c0

#define TEST_RAM_SIZE 0x1000

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

static int save_link(DmMc02FdcanMsgRamLink *link)
{
    QEMUFile *file = open_test_file(true);
    int ret = vmstate_save_state(file, dm_mc02_fdcan_msg_ram_link_vmstate(),
                                 link, NULL);

    if (!ret) {
        qemu_put_byte(file, QEMU_VM_EOF);
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static int load_link(DmMc02FdcanMsgRamLink *link)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_fdcan_msg_ram_link_vmstate(),
                                 link, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void capture_irq(void *opaque, int n, int level)
{
    IrqCapture *capture = opaque;

    (void)n;
    capture->calls++;
    capture->level = level;
}

static void prepare_source(DmMc02FdcanMsgRamLink *link,
                            DmMessageRam *msg_ram, uint8_t *ram_data)
{
    memset(link, 0, sizeof(*link));
    memset(ram_data, 0x5a, TEST_RAM_SIZE);
    msg_ram->data = ram_data;
    msg_ram->size = TEST_RAM_SIZE;
    g_assert_true(dm_mc02_fdcan_msg_ram_link_bind(link, msg_ram));

    /* Keep every section separate so this fixture exercises the address
     * calculation rather than relying on overlap being accepted. */
    put_le32(link->fdcan.regs + FDCAN_SIDFC, 0x100 / 4 | (2u << 16));
    put_le32(link->fdcan.regs + FDCAN_XIDFC, 0x110 / 4 | (1u << 16));
    put_le32(link->fdcan.regs + FDCAN_RXF0C, 0x200 / 4 | (4u << 16));
    put_le32(link->fdcan.regs + FDCAN_RXF1C, 0x400 / 4 | (4u << 16));
    put_le32(link->fdcan.regs + FDCAN_RXBC, 0x600 / 4);
    put_le32(link->fdcan.regs + FDCAN_TXBC, 0xa00 / 4 | (4u << 16));
    put_le32(link->fdcan.regs + FDCAN_RXESC, 0);
    put_le32(link->fdcan.regs + FDCAN_IR, 1);
    put_le32(link->fdcan.regs + FDCAN_IE, 1);
    put_le32(link->fdcan.regs + FDCAN_ILE, 1);
    link->fdcan.rx_fifo_configured = true;
    link->fdcan.rx_buffer_configured = true;
    link->fdcan.tx_fifo_configured = true;
    link->fdcan.rx_fifo_fill = 1;
    link->fdcan.rx_fifo_get = 0;
    link->fdcan.rx_fifo_put = 1;
    link->fdcan.rx_fifo1_fill = 1;
    link->fdcan.rx_fifo1_get = 0;
    link->fdcan.rx_fifo1_put = 1;
    link->fdcan.tx_fifo_get = 0;
    link->fdcan.tx_fifo_put = 1;
}

static void test_round_trip_preserves_shared_ram_identity_and_bytes(void)
{
    DmMc02FdcanMsgRamLink source;
    DmMc02FdcanMsgRamLink restored;
    DmMessageRam source_ram;
    DmMessageRam restored_ram;
    uint8_t source_data[TEST_RAM_SIZE];
    uint8_t restored_data[TEST_RAM_SIZE];
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source, &source_ram, source_data);
    source_data[0x6c0] = 0xa1;
    source_data[0x6c1] = 0xb2;
    g_assert_cmpint(save_link(&source), ==, 0);

    memset(&restored, 0, sizeof(restored));
    memset(restored_data, 0xc3, sizeof(restored_data));
    restored_ram.data = restored_data;
    restored_ram.size = TEST_RAM_SIZE;
    g_assert_true(dm_mc02_fdcan_msg_ram_link_bind(&restored, &restored_ram));
    restored.fdcan.irq_level_valid[0] = true;
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    restored.fdcan.irq[0] = irq;
    g_assert_cmpint(load_link(&restored), ==, 0);

    g_assert_true(restored.msg_ram == &restored_ram);
    g_assert_true(restored.fdcan.msg_ram == restored_data);
    g_assert_cmpuint(restored.msg_ram_size, ==, TEST_RAM_SIZE);
    g_assert_cmpuint(restored.fdcan.msg_ram_size, ==, TEST_RAM_SIZE);
    g_assert_cmpuint(restored.fdcan.regs[FDCAN_RXF0C], ==,
                     source.fdcan.regs[FDCAN_RXF0C]);
    g_assert_cmpuint(restored.fdcan.rx_fifo_fill, ==, source.fdcan.rx_fifo_fill);
    g_assert_cmpuint(restored_data[0x6c0], ==, 0xc3);
    g_assert_cmpuint(restored_data[0x6c1], ==, 0xc3);
    g_assert_cmpuint(capture.calls, ==, 1);
    g_assert_cmpint(capture.level, ==, 1);
    qemu_free_irq(irq);
}

static void test_rejects_geometry_mismatch_before_runtime_projection(void)
{
    DmMc02FdcanMsgRamLink source;
    DmMc02FdcanMsgRamLink restored;
    DmMessageRam source_ram;
    DmMessageRam restored_ram;
    uint8_t source_data[TEST_RAM_SIZE];
    uint8_t restored_data[TEST_RAM_SIZE / 2];
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source, &source_ram, source_data);
    g_assert_cmpint(save_link(&source), ==, 0);
    memset(&restored, 0, sizeof(restored));
    restored_ram.data = restored_data;
    restored_ram.size = sizeof(restored_data);
    g_assert_true(dm_mc02_fdcan_msg_ram_link_bind(&restored, &restored_ram));
    restored.fdcan.irq_level_valid[0] = true;
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    restored.fdcan.irq[0] = irq;

    g_assert_cmpint(load_link(&restored), !=, 0);
    g_assert_true(restored.fdcan.irq_level_valid[0]);
    g_assert_cmpuint(capture.calls, ==, 0);
    qemu_free_irq(irq);
}

static void test_rejects_fifo_and_buffer_bounds(void)
{
    DmMc02FdcanMsgRamLink link;
    DmMessageRam msg_ram;
    uint8_t ram_data[TEST_RAM_SIZE];

    prepare_source(&link, &msg_ram, ram_data);
    g_assert_true(dm_mc02_fdcan_msg_ram_link_state_valid(&link));

    /* Four 16-byte FIFO elements starting at 0xff0 do not fit. */
    put_le32(link.fdcan.regs + FDCAN_RXF0C, 0xff0 / 4 | (4u << 16));
    g_assert_false(dm_mc02_fdcan_msg_ram_link_state_valid(&link));

    prepare_source(&link, &msg_ram, ram_data);
    /* All 64 dedicated buffers must be addressable by the model's accepted
     * Rx-buffer index range. */
    put_le32(link.fdcan.regs + FDCAN_RXBC, 0xc10 / 4);
    g_assert_false(dm_mc02_fdcan_msg_ram_link_state_valid(&link));
}

static void test_two_links_observe_one_message_ram_owner(void)
{
    DmMc02FdcanMsgRamLink first;
    DmMc02FdcanMsgRamLink second;
    DmMessageRam msg_ram;
    uint8_t ram_data[TEST_RAM_SIZE];

    memset(ram_data, 0, sizeof(ram_data));
    msg_ram.data = ram_data;
    msg_ram.size = sizeof(ram_data);
    memset(&first, 0, sizeof(first));
    memset(&second, 0, sizeof(second));
    g_assert_true(dm_mc02_fdcan_msg_ram_link_bind(&first, &msg_ram));
    g_assert_true(dm_mc02_fdcan_msg_ram_link_bind(&second, &msg_ram));
    first.fdcan.msg_ram[0x2a] = 0x7e;
    g_assert_true(first.fdcan.msg_ram == second.fdcan.msg_ram);
    g_assert_cmpuint(second.fdcan.msg_ram[0x2a], ==, 0x7e);
    g_assert_true(dm_mc02_fdcan_msg_ram_link_state_valid(&first));
    g_assert_true(dm_mc02_fdcan_msg_ram_link_state_valid(&second));
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf(
        "%s/dm-fdcan-msg-ram-link.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-fdcan-msg-ram-link/round-trip",
                    test_round_trip_preserves_shared_ram_identity_and_bytes);
    g_test_add_func("/dm-fdcan-msg-ram-link/reject-geometry",
                    test_rejects_geometry_mismatch_before_runtime_projection);
    g_test_add_func("/dm-fdcan-msg-ram-link/reject-bounds",
                    test_rejects_fifo_and_buffer_bounds);
    g_test_add_func("/dm-fdcan-msg-ram-link/shared-owner",
                    test_two_links_observe_one_message_ram_owner);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
