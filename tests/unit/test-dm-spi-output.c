/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_spi.h"

void dm_test_clock(int64_t target, bool late);
void dm_test_clock_reset(void);

/* No DMA is connected in this serializer isolation fixture. */
bool dm_mc02_dma_request(DmMc02Dma *d, const DmMc02Dmamux *m,
                         uint32_t request, hwaddr addr)
{
    g_assert_not_reached();
}
bool dm_mc02_dma_request_endpoint(DmMc02Dma *d, const DmMc02Dmamux *m,
                                  uint32_t request, hwaddr addr,
                                  const DmMc02DmaEndpoint *endpoint, uint64_t ns)
{
    g_assert_not_reached();
}

static struct { uint64_t ns; bool driven, level; } samples[80];
static unsigned count;
static void output(void *opaque, uint64_t ns, bool driven, bool level)
{
    g_assert_cmpuint(count, <, G_N_ELEMENTS(samples));
    samples[count].ns = ns;
    samples[count].driven = driven;
    samples[count++].level = level;
}
static void wr(DmMc02Spi *s, unsigned reg, uint32_t value, unsigned size)
{
    s->iomem.ops->write(s, reg, value, size);
}
static uint32_t sr(DmMc02Spi *s)
{
    return s->iomem.ops->read(s, 0x14, 4);
}
static void init(DmMc02Spi *s)
{
    dm_test_clock_reset();
    dm_mc02_spi_state_init(s);
    dm_mc02_spi_init(s, NULL, "spi", false);
    dm_mc02_spi_enable_timing(s);
    dm_mc02_spi_set_clock_hz(s, 8000000);
    count = 0;
    dm_mc02_spi_set_mosi(s, output, NULL);
}

static void test_packed_fifo_late_clock(void)
{
    const uint8_t bytes[] = { 0x80, 0x01, 0xf0, 0x00 };
    DmMc02Spi s;
    init(&s);
    wr(&s, 8, 7, 4); /* DSIZE8, MBR /2: 4MHz */
    wr(&s, 12, (1u << 22) | (1u << 17), 4);
    wr(&s, 4, 4, 4);
    wr(&s, 0, 1, 4);
    wr(&s, 0x20, 0x00f00180, 4); /* packed byte order */
    wr(&s, 0, 1 | (1u << 9), 4);
    g_assert_false(sr(&s) & 8); /* EOT follows serialization, not TXDR. */
    count = 0;
    dm_test_clock(8000, true);
    g_assert_cmpuint(count, ==, 32);
    for (unsigned bit = 0; bit < 32; ++bit) {
        g_assert_true(samples[bit].driven);
        g_assert_cmpuint(samples[bit].ns, ==, bit * 250);
        g_assert_cmpint(samples[bit].level, ==,
                        (bytes[bit / 8] >> (7 - bit % 8)) & 1);
    }
    g_assert_true(sr(&s) & 8);
    g_assert_false(s.cr1 & (1u << 9));
    wr(&s, 0, 0, 4);
    g_assert_false(samples[count - 1].driven);
    dm_mc02_spi_cleanup(&s);
}

static void test_frame_width_order_phase_and_stop(void)
{
    DmMc02Spi s;
    init(&s);
    wr(&s, 8, 31 | (1u << 28), 4); /* DSIZE32, MBR /4: 2MHz */
    wr(&s, 12, (1u << 22) | (1u << 23) | (1u << 24), 4);
    wr(&s, 4, 1, 4);
    wr(&s, 0x20, 0x80000001, 4);
    wr(&s, 0, 1 | (1u << 9), 4);
    count = 0;
    dm_test_clock(15999, true);
    g_assert_cmpuint(count, ==, 32);
    for (unsigned bit = 0; bit < 32; ++bit) {
        g_assert_cmpuint(samples[bit].ns, ==, 250 + bit * 500);
        g_assert_cmpint(samples[bit].level, ==, bit == 0 || bit == 31);
    }
    g_assert_false(sr(&s) & 8);
    dm_test_clock(16000, false);
    g_assert_true(sr(&s) & 8);
    wr(&s, 0x18, 8, 4);
    g_assert_false(sr(&s) & 8);
    /* Stopping SPE cancels a queued transfer, including its future edges. */
    wr(&s, 0x20, 0xffffffff, 4);
    wr(&s, 0, 1 | (1u << 9), 4);
    wr(&s, 0, 0, 4);
    unsigned stopped = count;
    dm_test_clock(100000, true);
    g_assert_cmpuint(count, ==, stopped);
    dm_mc02_spi_cleanup(&s);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/spi/output/packed-late", test_packed_fifo_late_clock);
    g_test_add_func("/spi/output/width-phase-stop", test_frame_width_order_phase_and_stop);
    return g_test_run();
}
