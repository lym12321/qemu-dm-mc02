/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_spi.h"

void dm_test_clock(int64_t target, bool late);
void dm_test_clock_reset(void);
void dm_test_clock_set(int64_t target);
unsigned dm_test_clock_dispatches(void);
extern void (*dm_test_irq)(qemu_irq irq, int level);

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

static struct { uint64_t ns; bool driven, level; } samples[256];
static unsigned count;
static void output(void *opaque, uint64_t ns, bool driven, bool level)
{
    g_assert_cmpuint(count, <, G_N_ELEMENTS(samples));
    if (count) {
        g_assert_cmpuint(ns, >=, samples[count - 1].ns);
    }
    samples[count].ns = ns;
    samples[count].driven = driven;
    samples[count++].level = level;
}

/* The producer publishes transitions, not samples for every bit. Reconstruct
 * the held level so waveform checks remain independent of callback density.
 * Each recording begins with the reset MOSI level (low). */
static bool level_at(uint64_t ns)
{
    bool level = false;

    for (unsigned i = 0; i < count && samples[i].ns <= ns; ++i) {
        g_assert_true(samples[i].driven);
        level = samples[i].level;
    }
    return level;
}

static void assert_bit_interval(uint64_t ns, uint64_t width,
                                bool previous, bool level)
{
    if (ns) {
        g_assert_cmpint(level_at(ns - 1), ==, previous);
    }
    g_assert_cmpint(level_at(ns), ==, level);
    g_assert_cmpint(level_at(ns + width - 1), ==, level);
}
static void wr(DmMc02Spi *s, unsigned reg, uint32_t value, unsigned size)
{
    s->iomem.ops->write(s, reg, value, size);
}
static uint32_t sr(DmMc02Spi *s)
{
    return s->iomem.ops->read(s, 0x14, 4);
}
static void init_variant(DmMc02Spi *s, bool lowend)
{
    dm_test_clock_reset();
    dm_mc02_spi_state_init(s);
    dm_mc02_spi_init(s, NULL, "spi", false);
    if (lowend) {
        dm_mc02_spi_set_capabilities(s, 8, 16);
    }
    dm_mc02_spi_enable_timing(s);
    dm_mc02_spi_set_clock_hz(s, 8000000);
    count = 0;
    dm_mc02_spi_set_mosi(s, output, NULL);
}

static void init(DmMc02Spi *s)
{
    init_variant(s, false);
}

static void test_packed_fifo_late_clock(gconstpointer data)
{
    const uint8_t bytes[] = { 0x80, 0x01, 0xf0, 0x00 };
    const uint64_t edges[] = { 0, 250, 3750, 5000 };
    bool previous = false;
    DmMc02Spi s;
    init(&s);
    wr(&s, 8, 7, 4); /* DSIZE8, MBR /2: 4MHz */
    wr(&s, 12, (1u << 22) | (1u << 17), 4);
    wr(&s, 4, 4, 4);
    wr(&s, 0, 1, 4);
    wr(&s, 0x20, 0x00f00180, 4); /* packed byte order */
    wr(&s, 0, 1 | (1u << 9), 4);
    count = 0;
    g_assert_false(sr(&s) & 8); /* EOT follows serialization, not TXDR. */
    if (GPOINTER_TO_INT(data)) {
        dm_test_clock_set(8000);
        dm_mc02_spi_sync_mosi(&s, 8000);
    } else {
        dm_test_clock(8000, true);
    }
    g_assert_cmpuint(count, ==, G_N_ELEMENTS(edges));
    for (unsigned edge = 0; edge < count; ++edge) {
        g_assert_cmpuint(samples[edge].ns, ==, edges[edge]);
    }
    for (unsigned bit = 0; bit < 32; ++bit) {
        bool level = (bytes[bit / 8] >> (7 - bit % 8)) & 1;

        assert_bit_interval(bit * 250, 250, previous, level);
        previous = level;
    }
    /* The SR read dispatched the t=0 edge synchronously. The remaining
     * three transitions and four frame completions need seven callbacks. */
    g_assert_cmpuint(dm_test_clock_dispatches(), ==,
                     GPOINTER_TO_INT(data) ? 0 : 7);
    g_assert_true(sr(&s) & 8);
    g_assert_false(s.cr1 & (1u << 9));
    dm_mc02_spi_set_mosi(&s, output, NULL); /* Unchanged board mux. */
    g_assert_cmpuint(count, ==, G_N_ELEMENTS(edges));
    wr(&s, 0, 0, 4);
    g_assert_false(samples[count - 1].driven);
    dm_mc02_spi_cleanup(&s);
}

static void test_pad_sync_stopped_clock(void)
{
    DmMc02Spi s;
    unsigned stopped;
    init(&s);
    wr(&s, 8, 7, 4);
    wr(&s, 12, (1u << 22) | (1u << 17), 4);
    wr(&s, 4, 1, 4);
    wr(&s, 0x20, 0xff, 1);
    wr(&s, 0, 1 | (1u << 9), 4);
    count = 0;
    dm_test_clock(500, false);
    stopped = count;
    dm_mc02_spi_set_clock_hz(&s, 0);
    dm_test_clock_set(20000);
    dm_mc02_spi_sync_mosi(&s, 20000);
    g_assert_cmpuint(count, ==, stopped);
    g_assert_true(s.shifting);
    g_assert_false(sr(&s) & 8);
    dm_mc02_spi_set_clock_hz(&s, 8000000);
    dm_test_clock(22000, false);
    /* An all-high frame resumes without another MOSI transition. */
    g_assert_cmpuint(count, ==, stopped);
    g_assert_true(level_at(22000));
    g_assert_true(sr(&s) & 8);
    dm_mc02_spi_cleanup(&s);
}

static void test_frame_width_order_phase_and_stop(void)
{
    DmMc02Spi s;
    bool previous = false;
    init(&s);
    wr(&s, 8, 31 | (1u << 28), 4); /* DSIZE32, MBR /4: 2MHz */
    wr(&s, 12, (1u << 22) | (1u << 23) | (1u << 24), 4);
    wr(&s, 4, 1, 4);
    wr(&s, 0x20, 0x80000001, 4);
    wr(&s, 0, 1 | (1u << 9), 4);
    count = 0;
    dm_test_clock(15999, true);
    g_assert_cmpuint(count, ==, 3);
    for (unsigned bit = 0; bit < 32; ++bit) {
        bool level = bit == 0 || bit == 31;

        assert_bit_interval(250 + bit * 500, 500, previous, level);
        previous = level;
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

static int irq_level;

static void irq_output(qemu_irq irq, int level)
{
    g_assert_true(irq == (qemu_irq)&irq_level);
    irq_level = level;
}

static void test_lowend_fifo_packed_irq_reset(gconstpointer data)
{
    const uint32_t words[] = { 0x44332211, 0x88776655 };
    unsigned bits = GPOINTER_TO_UINT(data);
    unsigned slot = bits / 8;
    DmMc02Spi s;

    init_variant(&s, true);
    dm_test_irq = irq_output;
    dm_mc02_spi_set_irq(&s, (qemu_irq)&irq_level);
    /* Run twice: a reset must preserve the smaller instance's capabilities. */
    for (unsigned pass = 0; pass < 2; ++pass) {
        uint64_t start = pass * UINT64_C(20000);
        unsigned transitions = 0;
        bool previous = false;

        wr(&s, 8, bits - 1, 4);
        wr(&s, 12, (1u << 22) | (1u << 17), 4);
        wr(&s, 4, 8 / slot, 4);
        wr(&s, 0x10, 8, 4); /* EOTIE */
        wr(&s, 0, 1, 4);
        g_assert_true(sr(&s) & 2);
        wr(&s, 0x20, words[0], 4);
        g_assert_true(sr(&s) & 2);
        wr(&s, 0x20, words[1], 4);
        g_assert_false(sr(&s) & 2); /* Eight bytes, not SPI1's sixteen. */
        wr(&s, 0x20, 0xffffffff, slot); /* Overflow must not replace data. */
        g_assert_cmpuint(s.tx_count, ==, 8 / slot);
        g_assert_cmpint(irq_level, ==, 0);
        wr(&s, 0, 1 | (1u << 9), 4);
        count = 0;
        dm_test_clock(start + 15999, true);
        for (unsigned bit = 0; bit < 64; ++bit) {
            unsigned frame = bit / bits;
            uint32_t value = words[frame / (4 / slot)] >>
                             ((frame % (4 / slot)) * slot * 8);
            bool level = (value >> (bits - 1 - bit % bits)) & 1;

            assert_bit_interval(start + bit * 250, 250, previous, level);
            transitions += level != previous;
            previous = level;
        }
        g_assert_cmpuint(count, ==, transitions);
        g_assert_false(sr(&s) & 8);
        g_assert_cmpint(irq_level, ==, 0);
        dm_test_clock(start + 16000, false);
        g_assert_true(sr(&s) & 8);
        g_assert_cmpint(irq_level, ==, 1);
        wr(&s, 0x18, 8, 4);
        g_assert_false(sr(&s) & 8);
        g_assert_cmpint(irq_level, ==, 0);
        dm_test_clock(start + 20000, false);
        dm_mc02_spi_reset(&s);
    }
    dm_mc02_spi_cleanup(&s);
    dm_test_irq = NULL;
}

static void test_lowend_frame_limit(void)
{
    const uint8_t word[4] = { 0x11, 0x22, 0x33, 0x44 };
    DmMc02Spi s;

    init_variant(&s, true);
    wr(&s, 8, 16, 4); /* DSIZE17 is unsupported on SPI4/5/6. */
    g_assert_cmpuint(s.iomem.ops->read(&s, 8, 4), ==, 16);
    wr(&s, 12, (1u << 22) | (1u << 17), 4);
    wr(&s, 4, 1, 4);
    wr(&s, 0, 1 | (1u << 9), 4);
    g_assert_false(sr(&s) & 2);
    wr(&s, 0x20, 0xffffffff, 4);
    g_assert_cmpint(s.tx_endpoint.write_ex(&s, word, sizeof(word), 0), ==,
                    DM_MC02_DMA_ENDPOINT_ERROR);
    count = 0;
    dm_test_clock(10000, true);
    g_assert_cmpuint(count, ==, 0);
    g_assert_cmpuint(s.tx_count, ==, 0);
    g_assert_false(sr(&s) & 8);
    dm_mc02_spi_cleanup(&s);
}

static void test_polling_overdue_refill(gconstpointer data)
{
    bool observed = GPOINTER_TO_INT(data);
    DmMc02Spi s;

    init_variant(&s, true);
    if (!observed) {
        dm_mc02_spi_set_mosi(&s, NULL, NULL);
    }
    wr(&s, 8, 7, 4); /* 8-bit, 4 MHz. */
    wr(&s, 12, (1u << 22) | (1u << 17) | (1u << 24), 4);
    wr(&s, 4, 10, 4);
    wr(&s, 0, 1, 4);
    wr(&s, 0x20, 0x60606060, 4);
    wr(&s, 0x20, 0x60606060, 4);
    wr(&s, 0, 1 | (1u << 9), 4);
    wr(&s, 0x20, 0x60, 1); /* Shift register plus full eight-byte FIFO. */
    g_assert_false(sr(&s) & 2);
    count = 0;

    /* CPU polling can run while the IO thread has not dispatched a due
     * serializer callback. TXP must reflect elapsed peripheral time. */
    dm_test_clock_set(2100);
    g_assert_true(sr(&s) & 2);
    g_assert_cmpuint(s.tx_count, ==, 7);
    wr(&s, 0x20, 0x78, 1);
    g_assert_false(sr(&s) & 2);
    dm_test_clock_set(20000);
    g_assert_true(sr(&s) & 8);
    g_assert_cmpuint(count, ==, observed ? 20 : 0);
    if (observed) {
        bool previous = false;

        for (unsigned bit = 0; bit < 80; ++bit) {
            uint8_t value = bit < 72 ? 0x60 : 0x78;
            bool level = (value >> (7 - bit % 8)) & 1;

            assert_bit_interval(125 + bit * 250, 250, previous, level);
            previous = level;
        }
    }
    dm_mc02_spi_cleanup(&s);
}

static void test_write_after_overdue_empty(void)
{
    DmMc02Spi s;

    init(&s);
    wr(&s, 8, 7, 4);
    wr(&s, 12, (1u << 22) | (1u << 17) | (1u << 24), 4);
    wr(&s, 4, 2, 4);
    wr(&s, 0x20, 0x80, 1);
    wr(&s, 0, 1 | (1u << 9), 4);
    count = 0;
    /* A late TXDR write must not retroactively fill a FIFO that became
     * empty before this write. The second frame begins at the write time. */
    dm_test_clock_set(100000);
    wr(&s, 0x20, 0x01, 1);
    g_assert_cmpuint(count, ==, 2);
    dm_test_clock(102000, true);
    g_assert_true(sr(&s) & 8);
    g_assert_cmpuint(count, ==, 3);
    bool previous = false;
    for (unsigned bit = 0; bit < 16; ++bit) {
        uint64_t start = bit < 8 ? 0 : 100000;
        uint8_t value = bit < 8 ? 0x80 : 0x01;
        bool level = (value >> (7 - bit % 8)) & 1;

        assert_bit_interval(start + 125 + (bit % 8) * 250, 250, previous, level);
        previous = level;
    }
    g_assert_false(level_at(100000)); /* Empty interval is still low. */
    dm_mc02_spi_cleanup(&s);
}

static void test_constant_run_clock_change(void)
{
    for (unsigned phase = 0; phase < 2; ++phase) {
        for (unsigned gate = 0; gate < 2; ++gate) {
            DmMc02Spi s;
            uint64_t edge = gate ? (phase ? 8000 : 7500) :
                                  (phase ? 1350 : 1225);
            uint64_t end = gate ? (phase ? 8250 : 8000) :
                                 (phase ? 1413 : 1350);

            init(&s);
            wr(&s, 8, 7, 4); /* DSIZE8, 4MHz. */
            wr(&s, 12, (1u << 22) | (phase << 24), 4);
            wr(&s, 4, 1, 4);
            wr(&s, 0x20, 1, 1); /* Seven low bits followed by one high. */
            wr(&s, 0, 1 | (1u << 9), 4);
            count = 0;
            dm_test_clock(600, false); /* Mid-run, before its first edge. */
            if (gate) {
                dm_mc02_spi_set_clock_hz(&s, 0);
                dm_test_clock(5000, true);
                g_assert_cmpuint(count, ==, 0);
                g_assert_false(sr(&s) & 8);
            }
            dm_mc02_spi_set_clock_hz(&s, gate ? 4000000 : 16000000);
            dm_test_clock(edge - 1, false);
            g_assert_cmpuint(count, ==, 0);
            dm_test_clock(edge, false);
            g_assert_cmpuint(count, ==, 1);
            g_assert_cmpuint(samples[0].ns, ==, edge);
            g_assert_true(samples[0].driven && samples[0].level);
            dm_test_clock(end - 1, false);
            g_assert_false(sr(&s) & 8);
            dm_test_clock(end, false);
            g_assert_true(sr(&s) & 8);
            dm_mc02_spi_cleanup(&s);
        }
    }
}

static void test_constant_and_alternating_frames(void)
{
    const uint32_t words[] = { 0, 0xaaaaaaaa };

    for (unsigned i = 0; i < G_N_ELEMENTS(words); ++i) {
        DmMc02Spi s;
        bool previous = false;

        init(&s);
        wr(&s, 8, 31, 4); /* DSIZE32, 4MHz. */
        wr(&s, 12, 1u << 22, 4);
        wr(&s, 4, 1, 4);
        wr(&s, 0x20, words[i], 4);
        wr(&s, 0, 1 | (1u << 9), 4);
        count = 0;
        dm_test_clock(7999, true);
        g_assert_false(sr(&s) & 8);
        g_assert_cmpuint(count, ==, i ? 32 : 0);
        for (unsigned bit = 0; bit < 32; ++bit) {
            bool level = (words[i] >> (31 - bit)) & 1;

            assert_bit_interval(bit * 250, 250, previous, level);
            previous = level;
        }
        dm_test_clock(8000, false);
        g_assert_true(sr(&s) & 8);
        /* Constant data need only the frame end; alternating data retain
         * every edge. Neither case may finish TSIZE early. */
        g_assert_cmpuint(dm_test_clock_dispatches(), ==, i ? 33 : 1);
        dm_mc02_spi_cleanup(&s);
    }
}

static void test_lowend_symbol_runs(void)
{
    const uint8_t symbols[] = { 0x60, 0x78 };
    const uint64_t falling[] = { 584, 917 };

    for (unsigned i = 0; i < G_N_ELEMENTS(symbols); ++i) {
        DmMc02Spi s;

        init_variant(&s, true);
        dm_mc02_spi_set_clock_hz(&s, 24000000);
        wr(&s, 8, 7 | (1u << 28), 4); /* DSIZE8, MBR /4: 6MHz. */
        wr(&s, 12, (1u << 22) | (1u << 17) | (1u << 24), 4);
        wr(&s, 4, 1, 4);
        wr(&s, 0x20, symbols[i], 1);
        wr(&s, 0, 1 | (1u << 9), 4);
        count = 0;
        dm_test_clock(1333, true);
        g_assert_false(sr(&s) & 8);
        g_assert_cmpuint(count, ==, 2);
        g_assert_cmpuint(samples[0].ns, ==, 250);
        g_assert_cmpuint(samples[1].ns, ==, falling[i]);
        g_assert_false(level_at(249));
        g_assert_true(level_at(250));
        g_assert_true(level_at(falling[i] - 1));
        g_assert_false(level_at(falling[i]));
        dm_test_clock(1334, false);
        g_assert_true(sr(&s) & 8);
        /* Two edges plus one frame end, versus eight bits plus frame end. */
        g_assert_cmpuint(dm_test_clock_dispatches(), ==, 3);
        dm_mc02_spi_cleanup(&s);
    }
}

static void test_overdue_clock_change(void)
{
    DmMc02Spi s;

    init(&s);
    wr(&s, 8, 7, 4);
    wr(&s, 12, (1u << 22) | (1u << 17), 4);
    wr(&s, 4, 1, 4);
    wr(&s, 0x20, 0x80, 1);
    wr(&s, 0, 1 | (1u << 9), 4);
    count = 0;
    /* Clock notification may arrive before overdue callbacks are dispatched.
     * Finish the old-clock frame before applying the new frequency. */
    dm_test_clock_set(100000);
    dm_mc02_spi_set_clock_hz(&s, 16000000);
    g_assert_true(sr(&s) & 8);
    g_assert_cmpuint(count, ==, 2);
    g_assert_cmpuint(samples[0].ns, ==, 0);
    g_assert_cmpuint(samples[1].ns, ==, 250);
    assert_bit_interval(0, 250, false, true);
    assert_bit_interval(250, 999750, true, false);
    dm_test_clock(1000000, true);
    g_assert_cmpuint(count, ==, 2);
    dm_mc02_spi_cleanup(&s);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_data_func("/spi/output/packed-late", GINT_TO_POINTER(false), test_packed_fifo_late_clock);
    g_test_add_data_func("/spi/output/pad-sync", GINT_TO_POINTER(true), test_packed_fifo_late_clock);
    g_test_add_func("/spi/output/pad-sync-stopped-clock", test_pad_sync_stopped_clock);
    g_test_add_func("/spi/output/width-phase-stop", test_frame_width_order_phase_and_stop);
    g_test_add_data_func("/spi/output/lowend-packed8-irq-reset",
                         GUINT_TO_POINTER(8), test_lowend_fifo_packed_irq_reset);
    g_test_add_data_func("/spi/output/lowend-packed16-irq-reset",
                         GUINT_TO_POINTER(16), test_lowend_fifo_packed_irq_reset);
    g_test_add_func("/spi/output/lowend-frame-limit", test_lowend_frame_limit);
    g_test_add_data_func("/spi/output/polling-overdue-refill",
                         GINT_TO_POINTER(true), test_polling_overdue_refill);
    g_test_add_data_func("/spi/output/polling-overdue-unobserved",
                         GINT_TO_POINTER(false), test_polling_overdue_refill);
    g_test_add_func("/spi/output/write-after-overdue-empty", test_write_after_overdue_empty);
    g_test_add_func("/spi/output/constant-run-clock", test_constant_run_clock_change);
    g_test_add_func("/spi/output/constant-alternating", test_constant_and_alternating_frames);
    g_test_add_func("/spi/output/lowend-symbol-runs", test_lowend_symbol_runs);
    g_test_add_func("/spi/output/overdue-clock-change", test_overdue_clock_change);
    return g_test_run();
}
