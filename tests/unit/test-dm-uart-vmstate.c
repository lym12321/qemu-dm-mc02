/* Focused VMState contract test for the board-independent UART component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_uart.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_uart_sync_calls;

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

static void save_state(const DmMc02Uart *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_uart_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Uart *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_uart_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static bool endpoint_read(void *opaque, uint8_t *data, unsigned size,
                          uint64_t timestamp_ns)
{
    (void)opaque;
    (void)data;
    (void)size;
    (void)timestamp_ns;
    return false;
}

static bool endpoint_write(void *opaque, const uint8_t *data, unsigned size,
                           uint64_t timestamp_ns)
{
    (void)opaque;
    (void)data;
    (void)size;
    (void)timestamp_ns;
    return false;
}

static void prepare_source(DmMc02Uart *state)
{
    memset(state, 0, sizeof(*state));
    for (unsigned i = 0; i < ARRAY_SIZE(state->regs); ++i) {
        state->regs[i] = (uint8_t)(0x21u + i * 5u);
    }
    for (unsigned i = 0; i < ARRAY_SIZE(state->rx_fifo); ++i) {
        state->rx_fifo[i] = (uint8_t)(0x40u + i);
        state->rx_wire_fifo[i] = (uint8_t)(0x80u + i);
    }
    for (unsigned i = 0; i < ARRAY_SIZE(state->tx_fifo); ++i) {
        state->tx_fifo[i] = (uint8_t)(0xa0u + i);
    }

    state->rx_fifo_head = 17;
    state->rx_fifo_len = 23;
    state->rx_wire_head = 31;
    state->rx_wire_len = 29;
    state->tx_fifo_head = 37;
    state->tx_fifo_len = 41;
    state->rx_dropped = UINT64_C(0x0102030405060708);
    state->tx_dropped = UINT64_C(0x1112131415161718);
    state->tx_short_writes = UINT64_C(0x2122232425262728);
    state->rx_next_ns = UINT64_C(100000);
    state->idle_next_ns = 0;
    state->tx_next_ns = UINT64_C(300000);
    state->dma_tx_next_ns = UINT64_C(400000);
}

static void seed_runtime_fields(DmMc02Uart *state)
{
    memset(&state->chr, 0x5a, sizeof(state->chr));
    state->kernel_clock = (Clock *)(uintptr_t)0x1001;
    state->rx_timer = (QEMUTimer *)(uintptr_t)0x1002;
    state->idle_timer = (QEMUTimer *)(uintptr_t)0x1003;
    state->tx_timer = (QEMUTimer *)(uintptr_t)0x1004;
    state->dma_tx_timer = (QEMUTimer *)(uintptr_t)0x1005;
    state->dma_rx = (DmMc02Dma *)(uintptr_t)0x1006;
    state->dmamux_rx = (DmMc02Dmamux *)(uintptr_t)0x1007;
    state->dma_tx = (DmMc02Dma *)(uintptr_t)0x1008;
    state->dmamux_tx = (DmMc02Dmamux *)(uintptr_t)0x1009;
    state->irq = (qemu_irq)(uintptr_t)0x100a;
    state->kernel_clock_hz = UINT64_C(123456789);
    state->chr_enabled = true;
    state->rs485_enabled = true;
    state->de_mode = DM_MC02_UART_DE_MANUAL_GPIO;
    state->de_level = true;
    state->de_active_high = false;
    state->transceiver_powered = false;
    state->dma_rx_request_id = 11;
    state->dma_rx_peripheral_addr = 0x4000;
    state->dma_tx_request_id = 12;
    state->dma_tx_peripheral_addr = 0x4004;
    state->dma_rx_endpoint.read = endpoint_read;
    state->dma_rx_endpoint.write = endpoint_write;
    state->dma_rx_endpoint.opaque = (void *)(uintptr_t)0x1010;
    state->dma_tx_endpoint.read = endpoint_read;
    state->dma_tx_endpoint.write = endpoint_write;
    state->dma_tx_endpoint.opaque = (void *)(uintptr_t)0x1011;
    state->dma_endpoint_enabled = true;
    state->dma_tx_started = true;
    state->irq_level = true;
    state->irq_level_valid = true;
    memset(&state->rx_timing, 0x33, sizeof(state->rx_timing));
    memset(&state->tx_timing, 0x44, sizeof(state->tx_timing));
}

static void test_round_trip_restores_uart_state(void)
{
    DmMc02Uart source;
    DmMc02Uart restored;
    DmMc02Uart runtime;

    prepare_source(&source);
    memset(&runtime, 0, sizeof(runtime));
    seed_runtime_fields(&runtime);
    restored = runtime;
    save_state(&source);
    dm_mc02_uart_sync_calls = 0;

    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpmem(restored.rx_fifo, sizeof(restored.rx_fifo),
                    source.rx_fifo, sizeof(source.rx_fifo));
    g_assert_cmpmem(restored.rx_wire_fifo, sizeof(restored.rx_wire_fifo),
                    source.rx_wire_fifo, sizeof(source.rx_wire_fifo));
    g_assert_cmpmem(restored.tx_fifo, sizeof(restored.tx_fifo),
                    source.tx_fifo, sizeof(source.tx_fifo));
    g_assert_cmpuint(restored.rx_fifo_head, ==, source.rx_fifo_head);
    g_assert_cmpuint(restored.rx_fifo_len, ==, source.rx_fifo_len);
    g_assert_cmpuint(restored.rx_wire_head, ==, source.rx_wire_head);
    g_assert_cmpuint(restored.rx_wire_len, ==, source.rx_wire_len);
    g_assert_cmpuint(restored.tx_fifo_head, ==, source.tx_fifo_head);
    g_assert_cmpuint(restored.tx_fifo_len, ==, source.tx_fifo_len);
    g_assert_cmpuint(restored.rx_dropped, ==, source.rx_dropped);
    g_assert_cmpuint(restored.tx_dropped, ==, source.tx_dropped);
    g_assert_cmpuint(restored.tx_short_writes, ==, source.tx_short_writes);
    g_assert_cmpuint(restored.rx_next_ns, ==, source.rx_next_ns);
    g_assert_cmpuint(restored.idle_next_ns, ==, source.idle_next_ns);
    g_assert_cmpuint(restored.tx_next_ns, ==, source.tx_next_ns);
    g_assert_cmpuint(restored.dma_tx_next_ns, ==, source.dma_tx_next_ns);

    g_assert_cmpmem(&restored.chr, sizeof(restored.chr), &runtime.chr,
                    sizeof(runtime.chr));
    g_assert_true(restored.kernel_clock == runtime.kernel_clock);
    g_assert_cmpuint(restored.kernel_clock_hz, ==, runtime.kernel_clock_hz);
    g_assert_true(restored.rx_timer == runtime.rx_timer);
    g_assert_true(restored.idle_timer == runtime.idle_timer);
    g_assert_true(restored.tx_timer == runtime.tx_timer);
    g_assert_true(restored.dma_tx_timer == runtime.dma_tx_timer);
    g_assert_true(restored.dma_rx == runtime.dma_rx);
    g_assert_true(restored.dmamux_rx == runtime.dmamux_rx);
    g_assert_true(restored.dma_tx == runtime.dma_tx);
    g_assert_true(restored.dmamux_tx == runtime.dmamux_tx);
    g_assert_true(restored.irq == runtime.irq);
    g_assert_true(restored.chr_enabled);
    g_assert_true(restored.rs485_enabled);
    g_assert_cmpint(restored.de_mode, ==, runtime.de_mode);
    g_assert_true(restored.de_level);
    g_assert_false(restored.de_active_high);
    g_assert_false(restored.transceiver_powered);
    g_assert_cmpuint(restored.dma_rx_request_id, ==,
                     runtime.dma_rx_request_id);
    g_assert_cmpuint(restored.dma_tx_request_id, ==,
                     runtime.dma_tx_request_id);
    g_assert_cmpuint(restored.dma_rx_peripheral_addr, ==,
                     runtime.dma_rx_peripheral_addr);
    g_assert_cmpuint(restored.dma_tx_peripheral_addr, ==,
                     runtime.dma_tx_peripheral_addr);
    g_assert_cmpmem(&restored.dma_rx_endpoint, sizeof(restored.dma_rx_endpoint),
                    &runtime.dma_rx_endpoint, sizeof(runtime.dma_rx_endpoint));
    g_assert_cmpmem(&restored.dma_tx_endpoint, sizeof(restored.dma_tx_endpoint),
                    &runtime.dma_tx_endpoint, sizeof(runtime.dma_tx_endpoint));
    g_assert_true(restored.dma_endpoint_enabled);
    g_assert_true(restored.dma_tx_started);
    g_assert_true(restored.irq_level);
    g_assert_true(restored.irq_level_valid);
    g_assert_cmpmem(&restored.rx_timing, sizeof(restored.rx_timing),
                    &runtime.rx_timing, sizeof(runtime.rx_timing));
    g_assert_cmpmem(&restored.tx_timing, sizeof(restored.tx_timing),
                    &runtime.tx_timing, sizeof(runtime.tx_timing));
    g_assert_cmpuint(dm_mc02_uart_sync_calls, ==, 1);
}

static void test_round_trip_restores_idle_deadline(void)
{
    DmMc02Uart source;
    DmMc02Uart restored;

    prepare_source(&source);
    source.rx_wire_len = 0;
    source.rx_next_ns = 0;
    source.idle_next_ns = UINT64_C(200000);
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_uart_sync_calls = 0;

    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpuint(restored.idle_next_ns, ==, source.idle_next_ns);
    g_assert_cmpuint(restored.rx_wire_len, ==, 0);
    g_assert_cmpuint(restored.rx_next_ns, ==, 0);
    g_assert_cmpuint(dm_mc02_uart_sync_calls, ==, 1);
}

static void assert_rejected_without_sync(const DmMc02Uart *source)
{
    DmMc02Uart restored;

    save_state(source);
    memset(&restored, 0, sizeof(restored));
    restored.dma_tx_started = true;
    dm_mc02_uart_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_true(restored.dma_tx_started);
    g_assert_cmpuint(dm_mc02_uart_sync_calls, ==, 0);
}

static void test_rejects_invalid_fifo_head_or_length(void)
{
    DmMc02Uart source;

    prepare_source(&source);
    source.rx_fifo_head = DM_MC02_UART_RX_FIFO_SIZE;
    assert_rejected_without_sync(&source);

    prepare_source(&source);
    source.rx_fifo_len = DM_MC02_UART_RX_FIFO_SIZE + 1;
    assert_rejected_without_sync(&source);

    prepare_source(&source);
    source.rx_wire_head = DM_MC02_UART_RX_WIRE_FIFO_SIZE;
    assert_rejected_without_sync(&source);

    prepare_source(&source);
    source.rx_wire_len = DM_MC02_UART_RX_WIRE_FIFO_SIZE + 1;
    assert_rejected_without_sync(&source);

    prepare_source(&source);
    source.tx_fifo_head = DM_MC02_UART_TX_FIFO_SIZE;
    assert_rejected_without_sync(&source);

    prepare_source(&source);
    source.tx_fifo_len = DM_MC02_UART_TX_FIFO_SIZE + 1;
    assert_rejected_without_sync(&source);
}

static void test_rejects_deadline_above_int64_max(void)
{
    DmMc02Uart source;

    prepare_source(&source);
    source.rx_next_ns = UINT64_MAX;
    assert_rejected_without_sync(&source);

    prepare_source(&source);
    source.idle_next_ns = UINT64_MAX;
    source.rx_wire_len = 0;
    source.rx_next_ns = 0;
    assert_rejected_without_sync(&source);

    prepare_source(&source);
    source.tx_next_ns = UINT64_MAX;
    assert_rejected_without_sync(&source);

    prepare_source(&source);
    source.dma_tx_next_ns = UINT64_MAX;
    assert_rejected_without_sync(&source);
}

static void test_rejects_truncated_state_without_sync(void)
{
    DmMc02Uart state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    state.dma_tx_started = true;
    dm_mc02_uart_sync_calls = 0;
    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_true(state.dma_tx_started);
    g_assert_cmpuint(dm_mc02_uart_sync_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-uart-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-uart-vmstate/round-trip",
                    test_round_trip_restores_uart_state);
    g_test_add_func("/dm-uart-vmstate/round-trip-idle-deadline",
                    test_round_trip_restores_idle_deadline);
    g_test_add_func("/dm-uart-vmstate/reject-fifo",
                    test_rejects_invalid_fifo_head_or_length);
    g_test_add_func("/dm-uart-vmstate/reject-deadline",
                    test_rejects_deadline_above_int64_max);
    g_test_add_func("/dm-uart-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_sync);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
