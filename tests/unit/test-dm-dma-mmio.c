/* Exercise the DMA model's real MMIO callbacks and IRQ projections. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_dma.h"
#include "qemu/bswap.h"
#include "qemu/module.h"

#define TEST_LISR       0x000
#define TEST_HISR       0x004
#define TEST_LIFCR      0x008
#define TEST_HIFCR      0x00c
#define TEST_STREAM0    0x010
#define TEST_STRIDE     0x018
#define TEST_CR         0x000
#define TEST_FCR        0x014
#define TEST_FCR_FS     0x038
#define TEST_FCR_WRITE  0x087
#define TEST_FLAG_MASK  0x03d

typedef struct IrqCapture {
    unsigned calls;
    int level;
} IrqCapture;

typedef struct DmaFixture {
    DmMc02Dma dma;
    IrqCapture capture[DM_MC02_DMA_STREAM_COUNT];
    qemu_irq irq[DM_MC02_DMA_STREAM_COUNT];
} DmaFixture;

static const unsigned flag_shifts[] = { 0, 6, 16, 22, 0, 6, 16, 22 };

static void put_le32(uint8_t *where, uint32_t value)
{
    stl_le_p(where, value);
}

static uint32_t read_reg(DmMc02Dma *dma, hwaddr offset, unsigned size)
{
    return dma->iomem.ops->read(dma->iomem.opaque, offset, size);
}

static void write_reg(DmMc02Dma *dma, hwaddr offset, uint32_t value,
                      unsigned size)
{
    dma->iomem.ops->write(dma->iomem.opaque, offset, value, size);
}

static void capture_irq(void *opaque, int n, int level)
{
    IrqCapture *capture = opaque;

    capture->calls++;
    capture->level = level;
}

static void fixture_init(DmaFixture *f)
{
    memset(f, 0, sizeof(*f));
    dm_mc02_dma_init(&f->dma, NULL, "test-dma");
    for (unsigned stream = 0; stream < DM_MC02_DMA_STREAM_COUNT; stream++) {
        f->irq[stream] = qemu_allocate_irq(capture_irq, &f->capture[stream], 0);
        dm_mc02_dma_set_stream_irq(&f->dma, stream, f->irq[stream]);
        g_assert_cmpuint(f->capture[stream].calls, ==, 1);
        g_assert_cmpint(f->capture[stream].level, ==, 0);
        f->capture[stream].calls = 0;
    }
}

static void fixture_clear(DmaFixture *f)
{
    for (unsigned stream = 0; stream < DM_MC02_DMA_STREAM_COUNT; stream++) {
        qemu_free_irq(f->irq[stream]);
    }
}

static void test_register_reads(void)
{
    DmMc02Dma dma;
    static const unsigned sizes[] = { 1, 2, 4 };

    dm_mc02_dma_init(&dma, NULL, "test-dma");
    for (unsigned i = 0; i < sizeof(dma.regs); i++) {
        dma.regs[i] = (i * 19 + 0x63) & 0xff;
    }

    /* Include unaligned, sub-word and reserved-window reads.  Only a full
     * word starting exactly at a stream's FCR projects the live FIFO state. */
    for (unsigned i = 0; i < G_N_ELEMENTS(sizes); i++) {
        unsigned size = sizes[i];

        for (unsigned offset = 0; offset + size <= sizeof(dma.regs); offset++) {
            uint32_t expected = 0;

            for (unsigned byte = 0; byte < size; byte++) {
                expected |= (uint32_t)dma.regs[offset + byte] << (byte * 8);
            }
            for (unsigned stream = 0; stream < DM_MC02_DMA_STREAM_COUNT;
                 stream++) {
                if (size == 4 && offset == TEST_STREAM0 +
                    stream * TEST_STRIDE + TEST_FCR) {
                    expected &= ~TEST_FCR_FS;
                }
            }
            g_assert_cmphex(read_reg(&dma, offset, size), ==, expected);
        }
        g_assert_cmphex(read_reg(&dma, sizeof(dma.regs) - size + 1, size),
                        ==, 0);
    }
    g_assert_cmphex(read_reg(&dma, 0, 8), ==, 0);
}

static void test_fifo_status_and_write_mask(void)
{
    static const struct {
        unsigned bytes;
        unsigned fs;
    } cases[] = {
        { 0, 0 }, { 1, 1 }, { 4, 1 }, { 5, 2 }, { 8, 2 },
        { 9, 3 }, { 12, 3 }, { 13, 4 }, { 16, 4 },
    };
    DmMc02Dma dma;

    dm_mc02_dma_init(&dma, NULL, "test-dma");
    for (unsigned stream = 0; stream < DM_MC02_DMA_STREAM_COUNT; stream++) {
        unsigned fcr = TEST_STREAM0 + stream * TEST_STRIDE + TEST_FCR;

        write_reg(&dma, fcr, UINT32_MAX, 4);
        g_assert_cmphex(ldl_le_p(dma.regs + fcr), ==, TEST_FCR_WRITE);
        for (unsigned i = 0; i < G_N_ELEMENTS(cases); i++) {
            dma.fifo_length[stream] = cases[i].bytes;
            g_assert_cmphex(read_reg(&dma, fcr, 4), ==,
                            TEST_FCR_WRITE | (cases[i].fs << 3));
        }
        /* Existing byte-backed sub-word accesses retain their raw meaning. */
        write_reg(&dma, fcr, 0x5a, 1);
        write_reg(&dma, fcr + 2, 0xabcd, 2);
        g_assert_cmphex(read_reg(&dma, fcr, 1), ==, 0x5a);
        g_assert_cmphex(read_reg(&dma, fcr + 2, 2), ==, 0xabcd);
        g_assert_cmphex(read_reg(&dma, fcr, 4), ==, 0xabcd0062);
    }
}

static void test_w1c_flags_and_irq_gates(void)
{
    static const struct {
        uint32_t flag;
        uint32_t cr;
        uint32_t fcr;
    } cases[] = {
        { 1u << 0, 0, 1u << 7 }, /* FIFO error */
        { 1u << 2, 1u << 1, 0 }, /* Direct mode error */
        { 1u << 3, 1u << 2, 0 }, /* Transfer error */
        { 1u << 4, 1u << 3, 0 }, /* Half transfer */
        { 1u << 5, 1u << 4, 0 }, /* Transfer complete */
    };

    for (unsigned i = 0; i < G_N_ELEMENTS(cases); i++) {
        for (unsigned target = 0; target < DM_MC02_DMA_STREAM_COUNT; target++) {
            DmaFixture f;
            uint32_t flags = 0;
            unsigned status = target < 4 ? TEST_LISR : TEST_HISR;
            unsigned ifcr = target < 4 ? TEST_LIFCR : TEST_HIFCR;
            uint32_t clear = cases[i].flag << flag_shifts[target];

            fixture_init(&f);
            for (unsigned stream = 0; stream < DM_MC02_DMA_STREAM_COUNT;
                 stream++) {
                unsigned base = TEST_STREAM0 + stream * TEST_STRIDE;

                flags |= cases[i].flag << flag_shifts[stream];
                write_reg(&f.dma, base + TEST_CR, cases[i].cr, 4);
                write_reg(&f.dma, base + TEST_FCR, cases[i].fcr, 4);
            }
            put_le32(f.dma.regs + TEST_LISR, flags);
            put_le32(f.dma.regs + TEST_HISR, flags);
            put_le32(f.dma.regs + ifcr, 0x12345678);
            dm_mc02_dma_sync_runtime(&f.dma);
            for (unsigned stream = 0; stream < DM_MC02_DMA_STREAM_COUNT;
                 stream++) {
                g_assert_cmpint(f.capture[stream].level, ==, 1);
                f.capture[stream].calls = 0;
            }
            f.dma.request_cache_valid = true;
            write_reg(&f.dma, ifcr, clear, 4);
            g_assert_cmphex(read_reg(&f.dma, status, 4), ==, flags & ~clear);
            g_assert_cmphex(read_reg(&f.dma, status ^ 4, 4), ==, flags);
            g_assert_cmphex(read_reg(&f.dma, ifcr, 4), ==, 0x12345678);
            g_assert_true(f.dma.request_cache_valid);
            for (unsigned stream = 0; stream < DM_MC02_DMA_STREAM_COUNT;
                 stream++) {
                g_assert_cmpuint(f.capture[stream].calls, ==, stream == target);
                g_assert_cmpint(f.capture[stream].level, ==, stream != target);
                f.capture[stream].calls = 0;
            }
            /* Re-clearing an absent flag and a zero W1C leave levels alone. */
            write_reg(&f.dma, ifcr, clear, 4);
            write_reg(&f.dma, ifcr, 0, 4);
            for (unsigned stream = 0; stream < DM_MC02_DMA_STREAM_COUNT;
                 stream++) {
                g_assert_cmpuint(f.capture[stream].calls, ==, 0);
            }
            fixture_clear(&f);
        }
    }
}

static void test_w1c_reserved_and_subword(void)
{
    DmaFixture f;
    static const unsigned ifcrs[] = { TEST_LIFCR, TEST_HIFCR };

    fixture_init(&f);
    put_le32(f.dma.regs + TEST_LISR, UINT32_MAX);
    put_le32(f.dma.regs + TEST_HISR, UINT32_MAX);
    for (unsigned i = 0; i < G_N_ELEMENTS(ifcrs); i++) {
        unsigned ifcr = ifcrs[i];
        unsigned status = i ? TEST_HISR : TEST_LISR;

        write_reg(&f.dma, ifcr, 0xff, 1);
        write_reg(&f.dma, ifcr + 1, 0x5a3c, 2);
        g_assert_cmphex(read_reg(&f.dma, ifcr, 4), ==, 0x005a3cff);
        g_assert_cmphex(read_reg(&f.dma, status, 4), ==, UINT32_MAX);
    }
    write_reg(&f.dma, TEST_LIFCR + 3, 0x89abcdef, 4);
    g_assert_cmphex(read_reg(&f.dma, TEST_LISR, 4), ==, UINT32_MAX);
    g_assert_cmphex(read_reg(&f.dma, TEST_HISR, 4), ==, UINT32_MAX);
    for (unsigned i = 0; i < G_N_ELEMENTS(ifcrs); i++) {
        unsigned status = i ? TEST_HISR : TEST_LISR;

        /* Preserve the existing byte-backed reserved-bit clearing behavior. */
        write_reg(&f.dma, ifcrs[i], 1u << 1, 4);
        g_assert_cmphex(read_reg(&f.dma, status, 4), ==, UINT32_MAX & ~(1u << 1));
        write_reg(&f.dma, ifcrs[i], UINT32_MAX, 4);
        g_assert_cmphex(read_reg(&f.dma, status, 4), ==, 0);
    }
    fixture_clear(&f);
}

static void test_forced_irq_projection(void)
{
    DmaFixture f;
    IrqCapture replacement = { 0 };
    qemu_irq irq;

    fixture_init(&f);
    put_le32(f.dma.regs + TEST_LISR, TEST_FLAG_MASK);
    write_reg(&f.dma, TEST_STREAM0 + TEST_CR, 1u << 4, 4);
    g_assert_cmpint(f.capture[0].level, ==, 1);
    irq = qemu_allocate_irq(capture_irq, &replacement, 0);
    dm_mc02_dma_set_stream_irq(&f.dma, 0, irq);
    g_assert_cmpuint(replacement.calls, ==, 1);
    g_assert_cmpint(replacement.level, ==, 1);
    for (unsigned stream = 1; stream < DM_MC02_DMA_STREAM_COUNT; stream++) {
        f.capture[stream].calls = 0;
    }
    replacement.calls = 0;
    dm_mc02_dma_sync_runtime(&f.dma);
    g_assert_cmpuint(replacement.calls, ==, 1);
    g_assert_cmpint(replacement.level, ==, 1);
    for (unsigned stream = 1; stream < DM_MC02_DMA_STREAM_COUNT; stream++) {
        g_assert_cmpuint(f.capture[stream].calls, ==, 1);
        f.capture[stream].calls = 0;
    }
    replacement.calls = 0;
    dm_mc02_dma_reset(&f.dma);
    g_assert_cmpuint(replacement.calls, ==, 1);
    g_assert_cmpint(replacement.level, ==, 0);
    for (unsigned stream = 1; stream < DM_MC02_DMA_STREAM_COUNT; stream++) {
        g_assert_cmpuint(f.capture[stream].calls, ==, 1);
        g_assert_cmpint(f.capture[stream].level, ==, 0);
    }
    qemu_free_irq(irq);
    fixture_clear(&f);
}

int main(int argc, char **argv)
{
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-dma-mmio/register-reads", test_register_reads);
    g_test_add_func("/dm-dma-mmio/fifo-status-write-mask",
                    test_fifo_status_and_write_mask);
    g_test_add_func("/dm-dma-mmio/w1c-flags-irq", test_w1c_flags_and_irq_gates);
    g_test_add_func("/dm-dma-mmio/w1c-reserved-subword",
                    test_w1c_reserved_and_subword);
    g_test_add_func("/dm-dma-mmio/forced-irq-projection",
                    test_forced_irq_projection);
    return g_test_run();
}
