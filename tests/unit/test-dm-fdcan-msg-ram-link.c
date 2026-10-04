/* Focused VMState contract test for the FDCAN/shared Message RAM boundary. */
#include "qemu/osdep.h"
#include "qemu/bswap.h"
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
#define FDCAN_TXFQS 0x0c4
#define FDCAN_TXESC 0x0c8
#define FDCAN_TXBAR 0x0d0
#define FDCAN_TXBTO 0x0d8
#define FDCAN_GFC   0x080
#define FDCAN_RXF0S 0x0a4
#define FDCAN_RXF1S 0x0b4
#define FDCAN_NDAT1 0x098

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
                                 link, 2);

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
    put_le32(link->fdcan.regs + FDCAN_SIDFC, 0x100 | (2u << 16));
    put_le32(link->fdcan.regs + FDCAN_XIDFC, 0x110 | (1u << 16));
    put_le32(link->fdcan.regs + FDCAN_RXF0C, 0x200 | (4u << 16));
    put_le32(link->fdcan.regs + FDCAN_RXF1C, 0x400 | (4u << 16));
    put_le32(link->fdcan.regs + FDCAN_RXBC, 0x600);
    put_le32(link->fdcan.regs + FDCAN_TXBC, 0xa00 | (4u << 24));
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

    /* v1 used the wrong address/count layout. Reject before reading the
     * geometry marker, let alone projecting the restored child. */
    restored.msg_ram_size = 0x1234;
    QEMUFile *file = open_test_file(false);
    g_assert_cmpint(vmstate_load_state(
        file, dm_mc02_fdcan_msg_ram_link_vmstate(), &restored, 1), !=, 0);
    qemu_fclose(file);
    g_assert_cmpuint(restored.msg_ram_size, ==, 0x1234);
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
    put_le32(link.fdcan.regs + FDCAN_RXF0C, 0xff0 | (4u << 16));
    g_assert_false(dm_mc02_fdcan_msg_ram_link_state_valid(&link));

    prepare_source(&link, &msg_ram, ram_data);
    /* All 64 dedicated buffers must be addressable by the model's accepted
     * Rx-buffer index range. */
    put_le32(link.fdcan.regs + FDCAN_RXBC, 0xc10);
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

typedef struct CanPeer {
    CanBusClientState client;
    unsigned received;
    qemu_can_frame last;
} CanPeer;

static bool peer_can_receive(CanBusClientState *client)
{
    (void)client;
    return true;
}

static ssize_t peer_receive(CanBusClientState *client,
                            const qemu_can_frame *frames, size_t count)
{
    CanPeer *peer = container_of(client, CanPeer, client);

    g_assert_cmpuint(count, ==, 1);
    peer->last = frames[0];
    peer->received++;
    return count;
}

static void test_official_config_through_mmio_and_can(void)
{
    uint8_t ram[0x2000] = { 0 };
    DmMc02Fdcan fdcan;
    CanBusState *bus = CAN_BUS(object_new(TYPE_CAN_BUS));
    CanBusClientInfo peer_info = {
        .can_receive = peer_can_receive,
        .receive = peer_receive,
    };
    CanPeer peer = { .client.info = &peer_info };
    qemu_can_frame incoming = { .can_id = 0x321, .can_dlc = 8 };
    uint8_t classic[8] = { 0, 0x7f, 0x80, 0xff, 0x55, 0xaa, 6, 7 };
    uint8_t fd[64];

    /* ST H723 CMSIS start fields have Pos=2; HAL's word offset << 2
     * therefore yields these byte offsets, not another word count.
     * TXBC.TFQS is bits 29:24, NDTB is zero, and TFQM selects FIFO. */
    g_assert_true(dm_mc02_fdcan_init(&fdcan, NULL, "test-fdcan",
                                     DM_MC02_FDCAN_REGION_SIZE,
                                     ram, sizeof(ram), NULL, &error_abort));
    dm_mc02_fdcan_set_canbus(&fdcan, bus);
    g_assert_cmpint(can_bus_insert_client(bus, &peer.client), ==, 0);
    fdcan.iomem.ops->write(&fdcan, FDCAN_SIDFC, 0x00020080, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_XIDFC, 0x00010090, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_RXF0C, 0x00401000, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_RXF1C, 0x00020200, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_RXBC, 0x00001800, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_TXBC, 0x20000400, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_TXESC, 7, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_RXESC, 0x70, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_GFC, 0x28, 4);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_TXFQS, 4) & 0x3f,
                     ==, 32);
    g_assert_cmpuint(fdcan.rx_fifo_size, ==, 64);
    g_assert_true(dm_mc02_fdcan_message_ram_state_valid(&fdcan));

    stl_le_p(ram + 0x80, 0x4b210321); /* standard 0x321 -> FIFO0 */
    stl_le_p(ram + 0x84, 0x3b230005); /* standard 0x323 -> Rx buffer 5 */
    stl_le_p(ram + 0x90, 0x41234567); /* extended 0x1234567 -> FIFO1 */
    stl_le_p(ram + 0x94, 0x41234567);
    for (unsigned i = 0; i < sizeof(fd); ++i) {
        fd[i] = i ^ 0xa5;
    }

    stl_le_p(ram + 0xcb8, 0x04800000); /* TX slot31, standard ID 0x120 */
    stl_le_p(ram + 0xcbc, 0x00080000);
    memcpy(ram + 0xcc0, classic, sizeof(classic));
    fdcan.iomem.ops->write(&fdcan, FDCAN_TXBAR, 0x80000000, 4);
    g_assert_cmpuint(peer.received, ==, 1);
    g_assert_cmphex(peer.last.can_id, ==, 0x120);
    g_assert_cmpuint(peer.last.can_dlc, ==, 8);
    g_assert_cmpuint(peer.last.flags, ==, 0);
    g_assert_cmpmem(peer.last.data, 8, classic, 8);
    g_assert_cmphex(fdcan.iomem.ops->read(&fdcan, FDCAN_TXBTO, 4),
                    ==, 0x80000000);
    g_assert_cmpuint(fdcan.tx_fifo_get, ==, 0);

    stl_le_p(ram + 0x400, 0x401abcde); /* wrapped slot0, extended FD64+BRS */
    stl_le_p(ram + 0x404, 0x003f0000);
    memcpy(ram + 0x408, fd, sizeof(fd));
    fdcan.iomem.ops->write(&fdcan, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(peer.received, ==, 2);
    g_assert_cmphex(peer.last.can_id, ==, 0x801abcde);
    g_assert_cmpuint(peer.last.can_dlc, ==, 64);
    g_assert_cmphex(peer.last.flags, ==, QEMU_CAN_FRMF_TYPE_FD |
                                         QEMU_CAN_FRMF_BRS);
    g_assert_cmpmem(peer.last.data, 64, fd, 64);

    memcpy(incoming.data, classic, sizeof(classic));
    g_assert_cmpint(can_bus_client_send(&peer.client, &incoming, 1), ==, 1);
    g_assert_cmphex(ldl_le_p(ram + 0x1000), ==, 0x0c840000);
    g_assert_cmphex(ldl_le_p(ram + 0x1004), ==, 0x00080000);
    g_assert_cmpmem(ram + 0x1008, 8, classic, 8);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_RXF0S, 4) & 0x7f,
                     ==, 1);

    incoming.can_id = 0x81234567;
    incoming.can_dlc = 64;
    incoming.flags = QEMU_CAN_FRMF_TYPE_FD | QEMU_CAN_FRMF_BRS;
    memcpy(incoming.data, fd, sizeof(fd));
    g_assert_cmpint(can_bus_client_send(&peer.client, &incoming, 1), ==, 1);
    g_assert_cmphex(ldl_le_p(ram + 0x200), ==, 0x41234567);
    g_assert_cmphex(ldl_le_p(ram + 0x204), ==, 0x003f0000);
    g_assert_cmpmem(ram + 0x208, 64, fd, 64);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_RXF1S, 4) & 0x7f,
                     ==, 1);

    incoming.can_id = 0x323;
    incoming.can_dlc = 8;
    incoming.flags = 0;
    memcpy(incoming.data, classic, sizeof(classic));
    g_assert_cmpint(can_bus_client_send(&peer.client, &incoming, 1), ==, 1);
    g_assert_cmphex(ldl_le_p(ram + 0x1850), ==, 0x0c8c0000);
    g_assert_cmphex(ldl_le_p(ram + 0x1854), ==, 0x01080000);
    g_assert_cmpmem(ram + 0x1858, 8, classic, 8);
    g_assert_cmphex(fdcan.iomem.ops->read(&fdcan, FDCAN_NDAT1, 4), ==, 0x20);

    /* Zero capacities disable storage; no hidden three-element default,
     * full event or modulo-zero acknowledge survives reconfiguration. */
    fdcan.iomem.ops->write(&fdcan, FDCAN_RXF0C, 0, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_RXF1C, 0, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_TXBC, 0, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_IR, UINT32_MAX, 4);
    incoming.can_id = 0x321;
    g_assert_cmpint(can_bus_client_send(&peer.client, &incoming, 1), ==, 1);
    fdcan.iomem.ops->write(&fdcan, 0xa8, 0, 4);
    fdcan.iomem.ops->write(&fdcan, FDCAN_TXBAR, 1, 4);
    g_assert_cmpuint(peer.received, ==, 2);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_RXF0S, 4), ==, 0);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_RXF1S, 4), ==, 0);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_TXFQS, 4), ==, 0);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_IR, 4), ==, 0);
    g_assert_true(dm_mc02_fdcan_message_ram_state_valid(&fdcan));
    fdcan.iomem.ops->write(&fdcan, FDCAN_RXF0C + 2, 65, 1);
    fdcan.iomem.ops->write(&fdcan, FDCAN_TXBC + 2, 1, 1);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_RXF0C, 4), ==, 0);
    g_assert_cmpuint(fdcan.iomem.ops->read(&fdcan, FDCAN_TXBC, 4), ==, 0);

    can_bus_remove_client(&peer.client);
    dm_mc02_fdcan_cleanup(&fdcan);
    object_unref(OBJECT(bus));
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf(
        "%s/dm-fdcan-msg-ram-link.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    init_clocks(NULL);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-fdcan-msg-ram-link/round-trip",
                    test_round_trip_preserves_shared_ram_identity_and_bytes);
    g_test_add_func("/dm-fdcan-msg-ram-link/reject-geometry",
                    test_rejects_geometry_mismatch_before_runtime_projection);
    g_test_add_func("/dm-fdcan-msg-ram-link/reject-bounds",
                    test_rejects_fifo_and_buffer_bounds);
    g_test_add_func("/dm-fdcan-msg-ram-link/shared-owner",
                    test_two_links_observe_one_message_ram_owner);
    g_test_add_func("/dm-fdcan-msg-ram-link/official-mmio-can",
                    test_official_config_through_mmio_and_can);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
