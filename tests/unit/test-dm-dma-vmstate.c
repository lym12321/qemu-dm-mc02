/* Focused VMState contract test for the board-independent DMA component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_dma.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

#define TEST_DMA_LISR       0x000
#define TEST_DMA_STREAM0    0x010
#define TEST_DMA_SxCR       0x000
#define TEST_DMA_SxNDTR     0x004
#define TEST_DMA_SxPAR      0x008
#define TEST_DMA_SxM0AR     0x00c
#define TEST_DMA_SxM1AR     0x010
#define TEST_DMA_SxFCR      0x014
#define TEST_DMA_CR_TCIE    (1u << 4)
#define TEST_DMA_FLAG_TCIF  (1u << 5)

typedef struct IrqCapture {
    unsigned calls;
    int level;
} IrqCapture;

static int temp_fd;

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

static void save_state(const DmMc02Dma *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_dma_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Dma *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_dma_vmstate(), state, 1);

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

static void prepare_source(DmMc02Dma *source)
{
    memset(source, 0, sizeof(*source));
    put_le32(source->regs + TEST_DMA_LISR, TEST_DMA_FLAG_TCIF);
    put_le32(source->regs + TEST_DMA_STREAM0 + TEST_DMA_SxCR,
             TEST_DMA_CR_TCIE | (2u << 11) | (2u << 13));
    put_le32(source->regs + TEST_DMA_STREAM0 + TEST_DMA_SxNDTR, 5);
    put_le32(source->regs + TEST_DMA_STREAM0 + TEST_DMA_SxPAR,
             UINT32_C(0x40001000));
    put_le32(source->regs + TEST_DMA_STREAM0 + TEST_DMA_SxM0AR,
             UINT32_C(0x24000040));
    put_le32(source->regs + TEST_DMA_STREAM0 + TEST_DMA_SxM1AR,
             UINT32_C(0x24000080));
    put_le32(source->regs + TEST_DMA_STREAM0 + TEST_DMA_SxFCR,
             UINT32_C(0x85));

    source->reload_ndtr[0] = 5;
    source->reload_par[0] = UINT64_C(0x40001000);
    source->reload_m0ar[0] = UINT64_C(0x24000040);
    source->reload_m1ar[0] = UINT64_C(0x24000080);
    source->cursor_m0ar[0] = UINT64_C(0x24000054);
    source->cursor_m1ar[0] = UINT64_C(0x2400009c);
    for (unsigned i = 0; i < DM_MC02_DMA_FIFO_BYTES; ++i) {
        source->fifo[0][i] = 0xa0 + i;
    }
    source->fifo_head[0] = 3;
    source->fifo_length[0] = 7;
}

static void prepare_runtime(DmMc02Dma *state, qemu_irq irq)
{
    state->dmamux_channel_offset = 8;
    state->stream_irq[0] = irq;
    state->request_stream_mask[7] = 1;
    state->request_cache_seen[7] = true;
    state->request_cache_generation = 22;
    state->request_cache_valid = true;
    state->stream_irq_level[0] = false;
    state->stream_irq_level_valid[0] = true;
}

static void test_round_trip_restores_live_state_and_reprojects_runtime(void)
{
    DmMc02Dma source;
    DmMc02Dma restored;
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source);
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    prepare_runtime(&restored, irq);
    g_assert_cmpint(load_state(&restored), ==, 0);

    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpmem(restored.reload_ndtr, sizeof(restored.reload_ndtr),
                    source.reload_ndtr, sizeof(source.reload_ndtr));
    g_assert_cmpmem(restored.reload_par, sizeof(restored.reload_par),
                    source.reload_par, sizeof(source.reload_par));
    g_assert_cmpmem(restored.reload_m0ar, sizeof(restored.reload_m0ar),
                    source.reload_m0ar, sizeof(source.reload_m0ar));
    g_assert_cmpmem(restored.reload_m1ar, sizeof(restored.reload_m1ar),
                    source.reload_m1ar, sizeof(source.reload_m1ar));
    g_assert_cmpmem(restored.cursor_m0ar, sizeof(restored.cursor_m0ar),
                    source.cursor_m0ar, sizeof(source.cursor_m0ar));
    g_assert_cmpmem(restored.cursor_m1ar, sizeof(restored.cursor_m1ar),
                    source.cursor_m1ar, sizeof(source.cursor_m1ar));
    g_assert_cmpmem(restored.fifo, sizeof(restored.fifo), source.fifo,
                    sizeof(source.fifo));
    g_assert_cmpmem(restored.fifo_head, sizeof(restored.fifo_head),
                    source.fifo_head, sizeof(source.fifo_head));
    g_assert_cmpmem(restored.fifo_length, sizeof(restored.fifo_length),
                    source.fifo_length, sizeof(source.fifo_length));

    g_assert_cmpuint(restored.dmamux_channel_offset, ==, 8);
    g_assert_true(restored.stream_irq[0] == irq);
    g_assert_false(restored.request_cache_valid);
    g_assert_cmpuint(restored.request_cache_generation, ==, 0);
    g_assert_cmpuint(restored.request_stream_mask[7], ==, 0);
    g_assert_false(restored.request_cache_seen[7]);
    g_assert_cmpuint(restored.stream_irq_level_valid[0], ==, true);
    g_assert_cmpuint(restored.stream_irq_level[0], ==, true);
    g_assert_cmpuint(capture.calls, ==, 1);
    g_assert_cmpint(capture.level, ==, 1);
    qemu_free_irq(irq);
}

static void test_truncated_state_does_not_reproject_runtime(void)
{
    DmMc02Dma source;
    DmMc02Dma restored;
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source);
    save_state(&source);
    g_assert_cmpint(ftruncate(temp_fd, TEST_DMA_STREAM0), ==, 0);

    memset(&restored, 0, sizeof(restored));
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    prepare_runtime(&restored, irq);
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_true(restored.request_cache_valid);
    g_assert_cmpuint(restored.request_cache_generation, ==, 22);
    g_assert_cmpuint(restored.stream_irq_level_valid[0], ==, true);
    g_assert_cmpuint(capture.calls, ==, 0);
    qemu_free_irq(irq);
}

static void test_invalid_fifo_state_is_rejected_before_runtime_sync(void)
{
    DmMc02Dma source;
    DmMc02Dma restored;
    IrqCapture capture = { 0 };
    qemu_irq irq;

    prepare_source(&source);
    source.fifo_length[0] = DM_MC02_DMA_FIFO_BYTES + 1;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    irq = qemu_allocate_irq(capture_irq, &capture, 0);
    prepare_runtime(&restored, irq);
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_true(restored.request_cache_valid);
    g_assert_cmpuint(restored.request_cache_generation, ==, 22);
    g_assert_cmpuint(capture.calls, ==, 0);
    qemu_free_irq(irq);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-dma-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-dma-vmstate/round-trip",
                    test_round_trip_restores_live_state_and_reprojects_runtime);
    g_test_add_func("/dm-dma-vmstate/reject-truncated",
                    test_truncated_state_does_not_reproject_runtime);
    g_test_add_func("/dm-dma-vmstate/reject-invalid-fifo",
                    test_invalid_fifo_state_is_rejected_before_runtime_sync);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
