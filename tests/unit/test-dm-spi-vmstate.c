/* Focused VMState contract test for the board-independent SPI data path. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_spi.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_spi_sync_calls;

static int temp_fd;

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

static void save_state(const DmMc02Spi *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_spi_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Spi *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_spi_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void prepare_source(DmMc02Spi *state)
{
    memset(state, 0, sizeof(*state));
    state->cr1 = UINT32_C(0x00000001);
    state->cr2 = UINT32_C(0x00001234);
    state->cfg1 = UINT32_C(0x11223344);
    state->cfg2 = UINT32_C(0x55667788);
    state->transfer_remaining = UINT32_C(0x0234);
    state->rx = UINT8_C(0xa5);
    state->rx_valid = true;
    state->eot = false;
    state->dma_tx_next_ns = UINT64_C(5000);
    /* These fields are deliberately runtime-only and must not be serialized.
     * The restore test seeds the destination with different values below. */
    state->selected_mask = UINT32_C(0x2);
    state->dma_request_active = true;
    state->dma_endpoint_enabled = false;
    state->dma_batch_limit = 7;
}

static void test_round_trip_restores_spi_state(void)
{
    DmMc02Spi source;
    DmMc02Spi restored;

    prepare_source(&source);
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    restored.selected_mask = UINT32_C(0x1);
    restored.dma_request_active = true;
    restored.dma_endpoint_enabled = true;
    restored.dma_batch_limit = 3;
    dm_mc02_spi_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), ==, 0);

    g_assert_cmpuint(restored.cr1, ==, source.cr1);
    g_assert_cmpuint(restored.cr2, ==, source.cr2);
    g_assert_cmpuint(restored.cfg1, ==, source.cfg1);
    g_assert_cmpuint(restored.cfg2, ==, source.cfg2);
    g_assert_cmpuint(restored.transfer_remaining, ==,
                     source.transfer_remaining);
    g_assert_cmpuint(restored.rx, ==, source.rx);
    g_assert_cmpuint(restored.rx_valid, ==, source.rx_valid);
    g_assert_cmpuint(restored.eot, ==, source.eot);
    g_assert_cmpuint(restored.dma_tx_next_ns, ==, source.dma_tx_next_ns);
    g_assert_cmpuint(restored.selected_mask, ==, UINT32_C(0x1));
    g_assert_cmpuint(restored.dma_endpoint_enabled, ==, true);
    g_assert_cmpuint(restored.dma_batch_limit, ==, 3);
    g_assert_false(restored.dma_request_active);
    g_assert_cmpuint(dm_mc02_spi_sync_calls, ==, 1);
}

static void test_rejects_unrepresentable_state_without_sync(void)
{
    DmMc02Spi source;
    DmMc02Spi restored;

    prepare_source(&source);
    source.transfer_remaining = UINT32_MAX;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    restored.dma_request_active = true;
    dm_mc02_spi_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_true(restored.dma_request_active);
    g_assert_cmpuint(dm_mc02_spi_sync_calls, ==, 0);
}

static void test_rejects_out_of_range_deadline_without_sync(void)
{
    DmMc02Spi source;
    DmMc02Spi restored;

    prepare_source(&source);
    source.dma_tx_next_ns = UINT64_MAX;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    restored.dma_request_active = true;
    dm_mc02_spi_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_true(restored.dma_request_active);
    g_assert_cmpuint(dm_mc02_spi_sync_calls, ==, 0);
}

static void test_rejects_truncated_state_without_sync(void)
{
    DmMc02Spi state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    state.dma_request_active = true;
    dm_mc02_spi_sync_calls = 0;
    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_true(state.dma_request_active);
    g_assert_cmpuint(dm_mc02_spi_sync_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-spi-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-spi-vmstate/round-trip",
                    test_round_trip_restores_spi_state);
    g_test_add_func("/dm-spi-vmstate/reject-transfer-count",
                    test_rejects_unrepresentable_state_without_sync);
    g_test_add_func("/dm-spi-vmstate/reject-deadline",
                    test_rejects_out_of_range_deadline_without_sync);
    g_test_add_func("/dm-spi-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_sync);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
