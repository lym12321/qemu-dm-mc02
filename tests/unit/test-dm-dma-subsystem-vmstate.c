/* Focused VMState contract test for the composite DMA/DMAMUX subsystem. */
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

#define DMA_LISR 0x000
#define DMA_HISR 0x004
#define DMA_STREAM0 0x010
#define DMA_STREAM_STRIDE 0x018
#define DMA_SxCR 0x000
#define DMA_SxNDTR 0x004
#define DMA_SxPAR 0x008
#define DMA_SxM0AR 0x00c
#define DMA_SxFCR 0x014
#define DMA_CR_TCIE (1u << 4)
#define DMA_FLAG_TCIF (1u << 5)

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

static void save_state_with_description(
    const DmMc02DmaSubsystem *state, const VMStateDescription *description)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, description, (void *)state,
                                       NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static void save_state(const DmMc02DmaSubsystem *state)
{
    save_state_with_description(state, dm_mc02_dma_subsystem_vmstate());
}

/* The pre-marker stream is kept in the test as a compatibility fixture. */
static const VMStateDescription vmstate_dm_mc02_dma_subsystem_v1 = {
    .name = "dm-mc02-dma-subsystem-v1-fixture",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_STRUCT_ARRAY(dmamux, DmMc02DmaSubsystem,
                             DM_MC02_DMAMUX_COUNT, 0,
                             vmstate_dm_mc02_dmamux, DmMc02Dmamux),
        VMSTATE_STRUCT_ARRAY(dma, DmMc02DmaSubsystem,
                             DM_MC02_DMA_CONTROLLER_COUNT, 0,
                             vmstate_dm_mc02_dma_raw, DmMc02Dma),
        VMSTATE_END_OF_LIST()
    },
};

static int load_state_version(DmMc02DmaSubsystem *state, int version);

static int load_state(DmMc02DmaSubsystem *state)
{
    return load_state_version(state, 2);
}

static int load_state_version(DmMc02DmaSubsystem *state, int version)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_dma_subsystem_vmstate(),
                                 state, version);

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

static void prepare_dma(DmMc02Dma *state, unsigned stream, uint8_t seed)
{
    hwaddr base = DMA_STREAM0 + stream * DMA_STREAM_STRIDE;
    unsigned flag_shift = stream < 4 ? 0 : 6;

    memset(state, 0, sizeof(*state));
    put_le32(state->regs + (stream < 4 ? DMA_LISR : DMA_HISR),
             DMA_FLAG_TCIF << flag_shift);
    put_le32(state->regs + base + DMA_SxCR, DMA_CR_TCIE);
    put_le32(state->regs + base + DMA_SxNDTR, 3 + stream);
    put_le32(state->regs + base + DMA_SxPAR,
             UINT32_C(0x40001000) + stream * 0x100);
    put_le32(state->regs + base + DMA_SxM0AR,
             UINT32_C(0x24000040) + stream * 0x100);
    put_le32(state->regs + base + DMA_SxFCR, UINT32_C(0x85));
    state->reload_ndtr[stream] = 3 + stream;
    state->reload_par[stream] = UINT64_C(0x40001000) + stream * 0x100;
    state->reload_m0ar[stream] = UINT64_C(0x24000040) + stream * 0x100;
    state->cursor_m0ar[stream] = state->reload_m0ar[stream] + 4;
    state->fifo_head[stream] = 2;
    state->fifo_length[stream] = 5;
    for (unsigned i = 0; i < DM_MC02_DMA_FIFO_BYTES; ++i) {
        state->fifo[stream][i] = seed + i;
    }
}

static void assert_dma_persistent_equal(const DmMc02Dma *actual,
                                        const DmMc02Dma *expected)
{
    g_assert_cmpmem(actual->regs, sizeof(actual->regs), expected->regs,
                    sizeof(expected->regs));
    g_assert_cmpmem(actual->reload_ndtr, sizeof(actual->reload_ndtr),
                    expected->reload_ndtr, sizeof(expected->reload_ndtr));
    g_assert_cmpmem(actual->reload_par, sizeof(actual->reload_par),
                    expected->reload_par, sizeof(expected->reload_par));
    g_assert_cmpmem(actual->reload_m0ar, sizeof(actual->reload_m0ar),
                    expected->reload_m0ar, sizeof(expected->reload_m0ar));
    g_assert_cmpmem(actual->reload_m1ar, sizeof(actual->reload_m1ar),
                    expected->reload_m1ar, sizeof(expected->reload_m1ar));
    g_assert_cmpmem(actual->cursor_m0ar, sizeof(actual->cursor_m0ar),
                    expected->cursor_m0ar, sizeof(expected->cursor_m0ar));
    g_assert_cmpmem(actual->cursor_m1ar, sizeof(actual->cursor_m1ar),
                    expected->cursor_m1ar, sizeof(expected->cursor_m1ar));
    g_assert_cmpmem(actual->fifo, sizeof(actual->fifo), expected->fifo,
                    sizeof(expected->fifo));
    g_assert_cmpmem(actual->fifo_head, sizeof(actual->fifo_head),
                    expected->fifo_head, sizeof(expected->fifo_head));
    g_assert_cmpmem(actual->fifo_length, sizeof(actual->fifo_length),
                    expected->fifo_length, sizeof(expected->fifo_length));
}

static void prepare_source(DmMc02DmaSubsystem *source)
{
    memset(source, 0, sizeof(*source));
    dm_mc02_dma_subsystem_set_identity(source);
    prepare_dma(&source->dma[0], 0, 0x20);
    prepare_dma(&source->dma[1], 5, 0x80);
    source->dmamux[0].regs[0] = 17;
    source->dmamux[0].regs[4] = 23;
    source->dmamux[1].regs[0] = 31;
    source->dmamux[1].regs[4] = 47;
    source->dmamux[0].generation = UINT64_C(0x1020304050607080);
    source->dmamux[1].generation = UINT64_C(0xfedcba9876543210);
}

static void prepare_runtime(DmMc02DmaSubsystem *state, qemu_irq irq0,
                            qemu_irq irq1)
{
    dm_mc02_dma_subsystem_set_identity(state);
    /* DMA1 and DMA2 share DMAMUX1's 16 channels; the second controller
     * starts at channel 8.  These fields are destination-owned wiring. */
    state->dma[0].dmamux_channel_offset = 0;
    state->dma[1].dmamux_channel_offset = 8;
    state->dma[0].stream_irq[0] = irq0;
    state->dma[1].stream_irq[5] = irq1;
    state->dma[0].request_stream_mask[17] = 1;
    state->dma[1].request_stream_mask[31] = 1;
    state->dma[0].request_cache_seen[17] = true;
    state->dma[1].request_cache_seen[31] = true;
    state->dma[0].request_cache_generation = 91;
    state->dma[1].request_cache_generation = 92;
    state->dma[0].request_cache_valid = true;
    state->dma[1].request_cache_valid = true;
    state->dma[0].stream_irq_level[0] = false;
    state->dma[1].stream_irq_level[5] = false;
    state->dma[0].stream_irq_level_valid[0] = true;
    state->dma[1].stream_irq_level_valid[5] = true;
}

static void test_round_trip_composite_state_and_runtime_projection(void)
{
    DmMc02DmaSubsystem source, restored;
    IrqCapture capture0 = { 0 }, capture1 = { 0 };
    qemu_irq irq0, irq1;

    prepare_source(&source);
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    irq0 = qemu_allocate_irq(capture_irq, &capture0, 0);
    irq1 = qemu_allocate_irq(capture_irq, &capture1, 0);
    prepare_runtime(&restored, irq0, irq1);

    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpmem(restored.dmamux[0].regs, sizeof(restored.dmamux[0].regs),
                    source.dmamux[0].regs, sizeof(source.dmamux[0].regs));
    g_assert_cmpmem(restored.dmamux[1].regs, sizeof(restored.dmamux[1].regs),
                    source.dmamux[1].regs, sizeof(source.dmamux[1].regs));
    g_assert_cmpuint(restored.dmamux[0].generation, ==,
                     source.dmamux[0].generation);
    g_assert_cmpuint(restored.dmamux[1].generation, ==,
                     source.dmamux[1].generation);
    assert_dma_persistent_equal(&restored.dma[0], &source.dma[0]);
    assert_dma_persistent_equal(&restored.dma[1], &source.dma[1]);
    g_assert_cmpuint(restored.dma[0].dmamux_channel_offset, ==, 0);
    g_assert_cmpuint(restored.dma[1].dmamux_channel_offset, ==, 8);
    g_assert_true(restored.dma[0].stream_irq[0] == irq0);
    g_assert_true(restored.dma[1].stream_irq[5] == irq1);
    g_assert_false(restored.dma[0].request_cache_valid);
    g_assert_false(restored.dma[1].request_cache_valid);
    g_assert_cmpuint(restored.dma[0].request_cache_generation, ==, 0);
    g_assert_cmpuint(restored.dma[1].request_cache_generation, ==, 0);
    g_assert_false(restored.dma[0].request_cache_seen[17]);
    g_assert_false(restored.dma[1].request_cache_seen[31]);
    g_assert_true(restored.dma[0].stream_irq_level[0]);
    g_assert_true(restored.dma[1].stream_irq_level[5]);
    g_assert_cmpuint(capture0.calls, ==, 1);
    g_assert_cmpuint(capture1.calls, ==, 1);
    g_assert_cmpint(capture0.level, ==, 1);
    g_assert_cmpint(capture1.level, ==, 1);
    qemu_free_irq(irq0);
    qemu_free_irq(irq1);
}

static void test_invalid_fifo_rejected_before_composite_runtime_sync(void)
{
    DmMc02DmaSubsystem source, restored;
    IrqCapture capture0 = { 0 }, capture1 = { 0 };
    qemu_irq irq0, irq1;

    prepare_source(&source);
    source.dma[1].fifo_length[5] = DM_MC02_DMA_FIFO_BYTES + 1;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    irq0 = qemu_allocate_irq(capture_irq, &capture0, 0);
    irq1 = qemu_allocate_irq(capture_irq, &capture1, 0);
    prepare_runtime(&restored, irq0, irq1);
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_true(restored.dma[0].request_cache_valid);
    g_assert_true(restored.dma[1].request_cache_valid);
    g_assert_cmpuint(restored.dma[0].request_cache_generation, ==, 91);
    g_assert_cmpuint(restored.dma[1].request_cache_generation, ==, 92);
    g_assert_cmpuint(capture0.calls, ==, 0);
    g_assert_cmpuint(capture1.calls, ==, 0);
    qemu_free_irq(irq0);
    qemu_free_irq(irq1);
}

static void test_truncated_composite_rejected_before_runtime_sync(void)
{
    DmMc02DmaSubsystem source, restored;
    IrqCapture capture0 = { 0 }, capture1 = { 0 };
    qemu_irq irq0, irq1;

    prepare_source(&source);
    save_state(&source);
    g_assert_cmpint(ftruncate(temp_fd, DM_MC02_DMAMUX_REGION_SIZE + 8), ==, 0);
    memset(&restored, 0, sizeof(restored));
    irq0 = qemu_allocate_irq(capture_irq, &capture0, 0);
    irq1 = qemu_allocate_irq(capture_irq, &capture1, 0);
    prepare_runtime(&restored, irq0, irq1);
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_true(restored.dma[0].request_cache_valid);
    g_assert_true(restored.dma[1].request_cache_valid);
    g_assert_cmpuint(restored.dma[0].request_cache_generation, ==, 91);
    g_assert_cmpuint(restored.dma[1].request_cache_generation, ==, 92);
    g_assert_cmpuint(capture0.calls, ==, 0);
    g_assert_cmpuint(capture1.calls, ==, 0);
    qemu_free_irq(irq0);
    qemu_free_irq(irq1);
}

static void test_unsupported_version_rejected_before_runtime_sync(void)
{
    DmMc02DmaSubsystem source, restored;
    IrqCapture capture0 = { 0 }, capture1 = { 0 };
    qemu_irq irq0, irq1;

    prepare_source(&source);
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    irq0 = qemu_allocate_irq(capture_irq, &capture0, 0);
    irq1 = qemu_allocate_irq(capture_irq, &capture1, 0);
    prepare_runtime(&restored, irq0, irq1);
    g_assert_cmpint(load_state_version(&restored, 3), !=, 0);
    g_assert_true(restored.dma[0].request_cache_valid);
    g_assert_true(restored.dma[1].request_cache_valid);
    g_assert_cmpuint(capture0.calls, ==, 0);
    g_assert_cmpuint(capture1.calls, ==, 0);
    qemu_free_irq(irq0);
    qemu_free_irq(irq1);
}

static void test_rejects_mismatched_child_identity_before_runtime_sync(void)
{
    DmMc02DmaSubsystem source, restored;
    IrqCapture capture0 = { 0 }, capture1 = { 0 };
    qemu_irq irq0, irq1;

    prepare_source(&source);
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    irq0 = qemu_allocate_irq(capture_irq, &capture0, 0);
    irq1 = qemu_allocate_irq(capture_irq, &capture1, 0);
    prepare_runtime(&restored, irq0, irq1);
    restored.dma2_marker = DM_MC02_DMA_SUBSYSTEM_DMA1_MARKER;

    g_assert_cmpint(load_state_version(&restored, 2), !=, 0);
    g_assert_true(restored.dma[0].request_cache_valid);
    g_assert_true(restored.dma[1].request_cache_valid);
    g_assert_cmpuint(capture0.calls, ==, 0);
    g_assert_cmpuint(capture1.calls, ==, 0);
    qemu_free_irq(irq0);
    qemu_free_irq(irq1);
}

static void test_loads_pre_marker_v1_stream(void)
{
    DmMc02DmaSubsystem source, restored;
    IrqCapture capture0 = { 0 }, capture1 = { 0 };
    qemu_irq irq0, irq1;

    prepare_source(&source);
    save_state_with_description(&source,
                                &vmstate_dm_mc02_dma_subsystem_v1);
    memset(&restored, 0, sizeof(restored));
    irq0 = qemu_allocate_irq(capture_irq, &capture0, 0);
    irq1 = qemu_allocate_irq(capture_irq, &capture1, 0);
    prepare_runtime(&restored, irq0, irq1);

    g_assert_cmpint(load_state_version(&restored, 1), ==, 0);
    assert_dma_persistent_equal(&restored.dma[0], &source.dma[0]);
    assert_dma_persistent_equal(&restored.dma[1], &source.dma[1]);
    g_assert_cmpuint(restored.dma1_marker, ==,
                     DM_MC02_DMA_SUBSYSTEM_DMA1_MARKER);
    g_assert_cmpuint(restored.dma2_marker, ==,
                     DM_MC02_DMA_SUBSYSTEM_DMA2_MARKER);
    g_assert_cmpuint(capture0.calls, ==, 1);
    g_assert_cmpuint(capture1.calls, ==, 1);
    qemu_free_irq(irq0);
    qemu_free_irq(irq1);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-dma-subsystem-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-dma-subsystem-vmstate/round-trip",
                    test_round_trip_composite_state_and_runtime_projection);
    g_test_add_func("/dm-dma-subsystem-vmstate/reject-invalid-fifo",
                    test_invalid_fifo_rejected_before_composite_runtime_sync);
    g_test_add_func("/dm-dma-subsystem-vmstate/reject-truncated",
                    test_truncated_composite_rejected_before_runtime_sync);
    g_test_add_func("/dm-dma-subsystem-vmstate/reject-version",
                    test_unsupported_version_rejected_before_runtime_sync);
    g_test_add_func("/dm-dma-subsystem-vmstate/reject-identity",
                    test_rejects_mismatched_child_identity_before_runtime_sync);
    g_test_add_func("/dm-dma-subsystem-vmstate/load-v1",
                    test_loads_pre_marker_v1_stream);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
