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
#define TEST_REQUEST_ADDR 0x40010434u
#define TEST_MEMORY_BASE  0x24000000u
#define TEST_MMIO_ADDR    0x58020018u

static uint32_t read_reg(DmMc02Dma *dma, hwaddr offset, unsigned size);
static void write_reg(DmMc02Dma *dma, hwaddr offset, uint32_t value,
                      unsigned size);

extern MemTxResult (*dm_test_dma_memory_rw)(AddressSpace *as, hwaddr addr,
                                            MemTxAttrs attrs, void *buf,
                                            hwaddr len, bool is_write);

static uint8_t transfer_memory[32];
static struct { hwaddr addr; uint32_t value; uint64_t ns; } transfer_writes[8];
static unsigned transfer_write_count;
static bool reject_memory_write;
static DmMc02EventTime *transfer_event_time;
static DmMc02Dma *nested_dma;
static DmMc02Dmamux *nested_mux;

static MemTxResult transfer_rw(AddressSpace *as, hwaddr addr, MemTxAttrs attrs,
                                void *buf, hwaddr len, bool is_write)
{
    if (addr >= TEST_MEMORY_BASE &&
        addr - TEST_MEMORY_BASE + len <= sizeof(transfer_memory)) {
        uint8_t *memory = transfer_memory + addr - TEST_MEMORY_BASE;

        if (is_write) {
            if (reject_memory_write) {
                return MEMTX_ERROR;
            }
            memcpy(memory, buf, len);
        } else {
            memcpy(buf, memory, len);
        }
        return MEMTX_OK;
    }
    if (is_write && len == 4 &&
        (addr == TEST_MMIO_ADDR || addr == TEST_REQUEST_ADDR)) {
        g_assert_cmpuint(transfer_write_count, <,
                         G_N_ELEMENTS(transfer_writes));
        transfer_writes[transfer_write_count].addr = addr;
        transfer_writes[transfer_write_count].value = ldl_le_p(buf);
        transfer_writes[transfer_write_count++].ns =
            dm_mc02_event_time_get(transfer_event_time, 9000);
        if (nested_dma) {
            DmMc02Dma *dma = nested_dma;
            uint64_t outer_ns = dm_mc02_event_time_get(transfer_event_time, 9000);

            nested_dma = NULL;
            g_assert_true(dm_mc02_dma_request_batch_at(
                dma, nested_mux, 47, TEST_REQUEST_ADDR, 222, 1));
            g_assert_cmpuint(dm_mc02_event_time_get(transfer_event_time, 9000),
                             ==, outer_ns);
        }
        return MEMTX_OK;
    }
    return MEMTX_ERROR;
}

static void transfer_init(DmMc02Dma *dma, DmMc02Dmamux *mux,
                           hwaddr par, unsigned count, bool m2p)
{
    memset(transfer_memory, 0, sizeof(transfer_memory));
    transfer_write_count = 0;
    reject_memory_write = false;
    transfer_event_time = NULL;
    nested_dma = NULL;
    dm_test_dma_memory_rw = transfer_rw;
    dm_mc02_dma_init(dma, NULL, "test-dma");
    dm_mc02_dmamux_init(mux, NULL, "test-mux");
    mux->iomem.ops->write(mux, 0, 47, 4);
    dma->iomem.ops->write(dma, TEST_STREAM0 + 8, par, 4);
    dma->iomem.ops->write(dma, TEST_STREAM0 + 12, TEST_MEMORY_BASE, 4);
    dma->iomem.ops->write(dma, TEST_STREAM0 + 4, count, 4);
    dma->iomem.ops->write(dma, TEST_STREAM0,
        1 | (m2p ? 1u << 6 : 0) | (1u << 10) |
        (2u << 11) | (2u << 13), 4);
}

static DmMc02DmaEndpointResult endpoint_write(void *opaque,
    const uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    unsigned *calls = opaque;

    g_assert_cmpuint(size, ==, 4);
    (*calls)++;
    return *calls == 1 ? DM_MC02_DMA_ENDPOINT_RETRY :
                        DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void test_m2p_configured_par(gconstpointer data)
{
    DmMc02Dma dma;
    DmMc02Dmamux mux;
    unsigned mode = GPOINTER_TO_UINT(data);
    unsigned callback_calls = 0;
    DmMc02DmaEndpoint endpoint = {
        .write_ex = endpoint_write, .opaque = &callback_calls,
    };
    const uint32_t words[] = { 0x80, 0x800000, 0x80 };

    transfer_init(&dma, &mux, TEST_MMIO_ADDR, 3, true);
    for (unsigned i = 0; i < G_N_ELEMENTS(words); ++i) {
        stl_le_p(transfer_memory + i * 4, words[i]);
    }
    g_assert_false(dm_mc02_dma_request(&dma, &mux, 48, TEST_REQUEST_ADDR));
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 3);
    g_assert_true(dm_mc02_dma_request(&dma, &mux, 47, TEST_REQUEST_ADDR));
    /* An ordinary-request cache must not allow a different fixed callback
     * to consume this configured PAR. Negative endpoint caching must also
     * not suppress the following ordinary MMIO request. */
    g_assert_false(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR, &endpoint, 123));
    g_assert_cmpuint(callback_calls, ==, 0);
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 2);
    if (mode == 0) {
        g_assert_true(dm_mc02_dma_request(&dma, &mux, 47, TEST_REQUEST_ADDR));
        g_assert_true(dm_mc02_dma_request(&dma, &mux, 47, TEST_REQUEST_ADDR));
    } else if (mode == 1) {
        g_assert_true(dm_mc02_dma_request_batch(
            &dma, &mux, 47, TEST_REQUEST_ADDR, 2));
    } else {
        /* A caller's coalescing permission names its fixed endpoint. Other
         * configured MMIO addresses must retain every beat's side effect. */
        g_assert_true(dm_mc02_dma_request_batch_coalesced(
            &dma, &mux, 47, TEST_REQUEST_ADDR, 2));
    }
    g_assert_cmpuint(transfer_write_count, ==, G_N_ELEMENTS(words));
    for (unsigned i = 0; i < G_N_ELEMENTS(words); ++i) {
        g_assert_cmphex(transfer_writes[i].addr, ==, TEST_MMIO_ADDR);
        g_assert_cmphex(transfer_writes[i].value, ==, words[i]);
    }
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 0);
    g_assert_false(read_reg(&dma, TEST_STREAM0, 4) & 1);
    g_assert_true(read_reg(&dma, TEST_LISR, 4) & (1u << 5));
    dm_test_dma_memory_rw = NULL;
}

static void test_endpoint_retry_and_plain_request_cache(void)
{
    DmMc02Dma dma;
    DmMc02Dmamux mux;
    unsigned calls = 0;
    DmMc02DmaEndpoint endpoint = {
        .write_ex = endpoint_write, .opaque = &calls,
    };

    transfer_init(&dma, &mux, TEST_REQUEST_ADDR, 2, true);
    stl_le_p(transfer_memory, 0x12345678);
    stl_le_p(transfer_memory + 4, 0xaabbccdd);
    g_assert_false(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR, &endpoint, 123));
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 2);
    g_assert_cmphex(dma.cursor_m0ar[0], ==, TEST_MEMORY_BASE);
    g_assert_cmphex(read_reg(&dma, TEST_LISR, 4), ==, 0);
    g_assert_true(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR, &endpoint, 124));
    g_assert_cmpuint(calls, ==, 2);
    g_assert_true(dm_mc02_dma_request(&dma, &mux, 47, TEST_REQUEST_ADDR));
    g_assert_cmpuint(calls, ==, 2);
    g_assert_cmpuint(transfer_write_count, ==, 1);
    g_assert_cmphex(transfer_writes[0].addr, ==, TEST_REQUEST_ADDR);
    g_assert_cmphex(transfer_writes[0].value, ==, 0xaabbccdd);
    dm_test_dma_memory_rw = NULL;
}

static DmMc02DmaEndpointResult endpoint_accept(void *opaque,
    const uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    unsigned *calls = opaque;

    g_assert_cmpuint(size, ==, 4);
    (*calls)++;
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void test_endpoint_cache_ndtr_recapture(void)
{
    DmMc02Dma dma;
    DmMc02Dmamux mux;
    unsigned calls = 0;
    DmMc02DmaEndpoint endpoint = {
        .write_ex = endpoint_accept, .opaque = &calls,
    };

    transfer_init(&dma, &mux, TEST_REQUEST_ADDR, 3, true);
    write_reg(&dma, TEST_STREAM0,
              read_reg(&dma, TEST_STREAM0, 4) | (1u << 9), 4);
    g_assert_true(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR, &endpoint, 123));
    g_assert_cmphex(read_reg(&dma, TEST_STREAM0 + 8, 4), ==,
                    TEST_REQUEST_ADDR + 4);
    /* PINC still matches the captured starting address until NDTR captures
     * the live PAR again. A negative lookup must not survive that change. */
    g_assert_false(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR + 4, &endpoint, 124));
    write_reg(&dma, TEST_STREAM0 + 4, 2, 4);
    g_assert_true(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR + 4, &endpoint, 125));
    g_assert_cmpuint(calls, ==, 2);
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 1);
    g_assert_cmphex(read_reg(&dma, TEST_STREAM0 + 8, 4), ==,
                    TEST_REQUEST_ADDR + 8);
    dm_test_dma_memory_rw = NULL;
}

static void test_endpoint_cache_subword_pinc(gconstpointer data)
{
    DmMc02Dma dma;
    DmMc02Dmamux mux;
    unsigned mode = GPOINTER_TO_UINT(data);
    unsigned calls = 0;
    uint32_t cr;
    DmMc02DmaEndpoint endpoint = {
        .write_ex = endpoint_accept, .opaque = &calls,
    };

    transfer_init(&dma, &mux, TEST_REQUEST_ADDR, 0, true);
    cr = read_reg(&dma, TEST_STREAM0, 4) | (1u << 9);
    write_reg(&dma, TEST_STREAM0, cr & 0xffff, 2);
    write_reg(&dma, TEST_STREAM0 + 2, cr >> 16, 2);
    write_reg(&dma, TEST_STREAM0 + 4, 3, 2);
    if (mode & 1) {
        write_reg(&dma, TEST_STREAM0 + TEST_FCR, 1u << 2, 4);
    }
    /* Sub-word configuration leaves no captured reload count, so PINC
     * matches the live PAR. A separate request entry point advances it. */
    g_assert_false(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR + 4, &endpoint, 123));
    if (mode & 2) {
        g_assert_true(dm_mc02_dma_advance_stream(&dma, 0, 1));
    } else {
        g_assert_true(dm_mc02_dma_request_endpoint_batch(
            &dma, &mux, 47, TEST_REQUEST_ADDR, &endpoint, 124, 1));
    }
    g_assert_cmphex(read_reg(&dma, TEST_STREAM0 + 8, 4), ==,
                    TEST_REQUEST_ADDR + 4);
    g_assert_true(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR + 4, &endpoint, 125));
    g_assert_cmpuint(calls, ==, (mode & 2) ? 1 : 2);
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 1);
    g_assert_cmphex(read_reg(&dma, TEST_STREAM0 + 8, 4), ==,
                    TEST_REQUEST_ADDR + 8);
    dm_test_dma_memory_rw = NULL;
}

static void test_request_cache_mux_identity(void)
{
    DmMc02Dma dma;
    DmMc02Dmamux mux, other_mux;

    transfer_init(&dma, &mux, TEST_REQUEST_ADDR, 2, true);
    dm_mc02_dmamux_init(&other_mux, NULL, "test-other-mux");
    other_mux.iomem.ops->write(&other_mux, 0, 48, 4);
    /* Each mux has one configuration write, but only mux routes request 47.
     * Equal generation values do not make their route tables equivalent. */
    g_assert_false(dm_mc02_dma_request(
        &dma, &other_mux, 47, TEST_REQUEST_ADDR));
    g_assert_true(dm_mc02_dma_request(&dma, &mux, 47, TEST_REQUEST_ADDR));
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 1);
    g_assert_false(dm_mc02_dma_request(
        &dma, &other_mux, 47, TEST_REQUEST_ADDR));
    g_assert_true(dm_mc02_dma_request(&dma, &mux, 47, TEST_REQUEST_ADDR));
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 0);
    g_assert_cmpuint(transfer_write_count, ==, 2);
    dm_test_dma_memory_rw = NULL;
}

typedef struct Reservation { unsigned prepares, commits, aborts; } Reservation;

static DmMc02DmaEndpointResult endpoint_prepare(void *opaque,
    uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    Reservation *reservation = opaque;

    g_assert_cmpuint(size, ==, 4);
    reservation->prepares++;
    stl_le_p(data, 0x12345678);
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void endpoint_commit(void *opaque)
{
    ((Reservation *)opaque)->commits++;
}

static void endpoint_abort(void *opaque)
{
    ((Reservation *)opaque)->aborts++;
}

static void test_p2m_endpoint_reservation(void)
{
    DmMc02Dma dma;
    DmMc02Dmamux mux;
    Reservation reservation = { 0 };
    DmMc02DmaEndpoint endpoint = {
        .read_prepare = endpoint_prepare,
        .read_commit = endpoint_commit,
        .read_abort = endpoint_abort,
        .opaque = &reservation,
    };

    transfer_init(&dma, &mux, TEST_REQUEST_ADDR, 1, false);
    g_assert_false(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_MMIO_ADDR, &endpoint, 123));
    g_assert_cmpuint(reservation.prepares, ==, 0);
    reject_memory_write = true;
    g_assert_true(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR, &endpoint, 124));
    g_assert_cmpuint(reservation.prepares, ==, 1);
    g_assert_cmpuint(reservation.aborts, ==, 1);
    g_assert_cmpuint(reservation.commits, ==, 0);
    g_assert_cmpuint(read_reg(&dma, TEST_STREAM0 + 4, 4), ==, 1);
    g_assert_true(read_reg(&dma, TEST_LISR, 4) & (1u << 3));
    reject_memory_write = false;
    write_reg(&dma, TEST_STREAM0, read_reg(&dma, TEST_STREAM0, 4) | 1, 4);
    g_assert_true(dm_mc02_dma_request_endpoint(
        &dma, &mux, 47, TEST_REQUEST_ADDR, &endpoint, 125));
    g_assert_cmpuint(reservation.prepares, ==, 2);
    g_assert_cmpuint(reservation.aborts, ==, 1);
    g_assert_cmpuint(reservation.commits, ==, 1);
    g_assert_cmphex(ldl_le_p(transfer_memory), ==, 0x12345678);
    dm_test_dma_memory_rw = NULL;
}

static void test_request_event_time_scopes(void)
{
    DmMc02Dma dma, inner;
    DmMc02Dmamux mux, inner_mux;
    DmMc02EventTime context = { 0 }, saved;
    static const uint64_t expected[] = { 0, 9000, 111, 222, 333 };

    transfer_init(&dma, &mux, TEST_MMIO_ADDR, 4, true);
    transfer_init(&inner, &inner_mux, TEST_MMIO_ADDR, 1, true);
    dm_mc02_dma_set_event_time(&dma, &context);
    dm_mc02_dma_set_event_time(&inner, &context);
    transfer_event_time = &context;
    g_assert_true(dm_mc02_dma_request_at(
        &dma, &mux, 47, TEST_REQUEST_ADDR, 0)); /* Zero is a valid event time. */
    g_assert_false(context.active);
    g_assert_true(dm_mc02_dma_request(&dma, &mux, 47, TEST_REQUEST_ADDR));
    saved = dm_mc02_event_time_enter(&context, 333);
    nested_dma = &inner;
    nested_mux = &inner_mux;
    g_assert_true(dm_mc02_dma_request_at(
        &dma, &mux, 47, TEST_REQUEST_ADDR, 111));
    g_assert_true(context.active);
    g_assert_cmpuint(context.timestamp_ns, ==, 333);
    g_assert_false(dm_mc02_dma_request_batch_at(
        &dma, &mux, 47, TEST_REQUEST_ADDR, 444, 0));
    g_assert_cmpuint(context.timestamp_ns, ==, 333);
    /* A legacy request inherits a caller's scope without changing it. */
    g_assert_true(dm_mc02_dma_request(&dma, &mux, 47, TEST_REQUEST_ADDR));
    dm_mc02_event_time_leave(&context, saved);
    g_assert_false(context.active);
    g_assert_false(dm_mc02_dma_request_at(
        &dma, &mux, 48, TEST_REQUEST_ADDR, 444));
    g_assert_false(context.active);
    g_assert_cmpuint(transfer_write_count, ==, G_N_ELEMENTS(expected));
    for (unsigned i = 0; i < G_N_ELEMENTS(expected); ++i) {
        g_assert_cmpuint(transfer_writes[i].ns, ==, expected[i]);
    }
    dm_mc02_dma_reset(&dma);
    g_assert_true(dma.event_time == &context);
    dm_test_dma_memory_rw = NULL;
    transfer_event_time = NULL;
}

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

typedef struct MmioHookCapture {
    uint8_t *regs;
    hwaddr watched;
    unsigned before_calls, after_calls, enabled_calls;
    unsigned sequence, before_step, after_step, enabled_step;
    uint32_t before_value, after_value, synchronized_value;
    bool synchronize_value;
} MmioHookCapture;

static void mmio_before_access(void *opaque)
{
    MmioHookCapture *capture = opaque;

    capture->before_calls++;
    capture->before_step = ++capture->sequence;
    capture->before_value = ldl_le_p(capture->regs + capture->watched);
    if (capture->synchronize_value) {
        stl_le_p(capture->regs + capture->watched,
                  capture->synchronized_value);
    }
}

static void mmio_after_write(void *opaque)
{
    MmioHookCapture *capture = opaque;

    capture->after_calls++;
    capture->after_step = ++capture->sequence;
    capture->after_value = ldl_le_p(capture->regs + capture->watched);
}

static void mmio_stream_enabled(void *opaque, unsigned stream, uint32_t cr)
{
    MmioHookCapture *capture = opaque;

    capture->enabled_calls++;
    capture->enabled_step = ++capture->sequence;
    g_assert_cmpuint(stream, ==, 0);
    g_assert_cmphex(cr, ==, 1);
    g_assert_cmphex(ldl_le_p(capture->regs + TEST_STREAM0), ==, cr);
}

static void test_mmio_hooks(gconstpointer data)
{
    bool mux_mode = GPOINTER_TO_UINT(data);
    DmMc02Dma dma;
    DmMc02Dmamux mux;
    MemoryRegion *region;
    MmioHookCapture capture = { 0 };

    dm_mc02_dma_init(&dma, NULL, "hook-dma");
    dm_mc02_dmamux_init(&mux, NULL, "hook-mux");
    if (mux_mode) {
        region = &mux.iomem;
        capture.regs = mux.regs;
        dm_mc02_dmamux_set_mmio_hooks(&mux, mmio_before_access,
                                      mmio_after_write, &capture);
    } else {
        region = &dma.iomem;
        capture.regs = dma.regs;
        /* An inert byte-backed register isolates access hook ordering. */
        capture.watched = 0x300;
        dm_mc02_dma_set_mmio_hooks(&dma, mmio_before_access,
                                   mmio_after_write, &capture);
    }

    region->ops->write(region->opaque, capture.watched, 0x11223344, 4);
    g_assert_cmpuint(capture.before_calls, ==, 1);
    g_assert_cmpuint(capture.after_calls, ==, 1);
    g_assert_cmpuint(capture.before_step, <, capture.after_step);
    g_assert_cmphex(capture.before_value, ==, 0);
    g_assert_cmphex(capture.after_value, ==, 0x11223344);

    /* A read must observe state produced by the synchronization hook. */
    capture.synchronize_value = true;
    capture.synchronized_value = 0xaabbccdd;
    g_assert_cmphex(region->ops->read(region->opaque, capture.watched, 4),
                    ==, 0xaabbccdd);
    capture.synchronize_value = false;
    g_assert_cmpuint(capture.before_calls, ==, 2);
    g_assert_cmpuint(capture.after_calls, ==, 1);

    /* Ignored writes still complete the before/after transaction pair. */
    region->ops->write(region->opaque, DM_MC02_DMA_REGION_SIZE, 0, 4);
    g_assert_cmpuint(capture.before_calls, ==, 3);
    g_assert_cmpuint(capture.after_calls, ==, 2);
    g_assert_cmphex(capture.before_value, ==, 0xaabbccdd);
    g_assert_cmphex(capture.after_value, ==, 0xaabbccdd);
    g_assert_cmphex(region->ops->read(region->opaque,
                                     DM_MC02_DMA_REGION_SIZE, 4), ==, 0);
    g_assert_cmpuint(capture.before_calls, ==, 4);
    g_assert_cmpuint(capture.after_calls, ==, 2);

    if (mux_mode) {
        dm_mc02_dmamux_reset(&mux);
    } else {
        dm_mc02_dma_reset(&dma);
        dm_mc02_dma_sync_runtime(&dma);
    }
    g_assert_cmpuint(capture.before_calls, ==, 4);
    g_assert_cmpuint(capture.after_calls, ==, 2);
    g_assert_cmphex(region->ops->read(region->opaque, capture.watched, 4),
                    ==, 0);
    g_assert_cmpuint(capture.before_calls, ==, 5);

    if (!mux_mode) {
        capture.watched = TEST_LISR;
        stl_le_p(dma.regs + TEST_LISR, 1u << 5);
        /* Full-word W1C has its own early return in the register writer. */
        write_reg(&dma, TEST_LIFCR, 1u << 5, 4);
        g_assert_cmpuint(capture.before_calls, ==, 6);
        g_assert_cmpuint(capture.after_calls, ==, 3);
        g_assert_cmphex(capture.before_value, ==, 1u << 5);
        g_assert_cmphex(capture.after_value, ==, 0);

        capture.watched = TEST_STREAM0;
        dm_mc02_dma_set_stream_enabled_callback(
            &dma, mmio_stream_enabled, &capture);
        write_reg(&dma, TEST_STREAM0, 1, 4);
        g_assert_cmpuint(capture.enabled_calls, ==, 1);
        g_assert_cmpuint(capture.before_calls, ==, 7);
        g_assert_cmpuint(capture.after_calls, ==, 4);
        g_assert_cmpuint(capture.before_step, <, capture.enabled_step);
        g_assert_cmpuint(capture.enabled_step, <, capture.after_step);
        g_assert_cmphex(capture.after_value, ==, 1);
    }

    if (mux_mode) {
        dm_mc02_dmamux_set_mmio_hooks(&mux, NULL, NULL, NULL);
    } else {
        dm_mc02_dma_set_mmio_hooks(&dma, NULL, NULL, NULL);
    }
    region->ops->read(region->opaque, capture.watched, 4);
    g_assert_cmpuint(capture.before_calls, ==, mux_mode ? 5 : 7);
}

static void test_request_bypasses_mmio_hooks(void)
{
    DmMc02Dma dma;
    DmMc02Dmamux mux;
    MmioHookCapture dma_capture = { .watched = TEST_STREAM0 + 4 };
    MmioHookCapture mux_capture = { 0 };

    transfer_init(&dma, &mux, TEST_MMIO_ADDR, 1, true);
    dma_capture.regs = dma.regs;
    mux_capture.regs = mux.regs;
    dm_mc02_dma_set_mmio_hooks(&dma, mmio_before_access,
                               mmio_after_write, &dma_capture);
    dm_mc02_dmamux_set_mmio_hooks(&mux, mmio_before_access,
                                  mmio_after_write, &mux_capture);
    g_assert_true(dm_mc02_dma_request_at(
        &dma, &mux, 47, TEST_REQUEST_ADDR, 123));
    g_assert_cmphex(ldl_le_p(dma.regs + TEST_STREAM0 + 4), ==, 0);
    g_assert_true(ldl_le_p(dma.regs + TEST_LISR) & (1u << 5));
    g_assert_cmpuint(dma_capture.before_calls, ==, 0);
    g_assert_cmpuint(dma_capture.after_calls, ==, 0);
    g_assert_cmpuint(mux_capture.before_calls, ==, 0);
    g_assert_cmpuint(mux_capture.after_calls, ==, 0);
    dm_test_dma_memory_rw = NULL;
}

int main(int argc, char **argv)
{
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-dma-mmio/register-reads", test_register_reads);
    g_test_add_data_func("/dm-dma-mmio/hooks/dma", GUINT_TO_POINTER(0),
                        test_mmio_hooks);
    g_test_add_data_func("/dm-dma-mmio/hooks/dmamux", GUINT_TO_POINTER(1),
                        test_mmio_hooks);
    g_test_add_func("/dm-dma-mmio/hooks/request-bypass",
                    test_request_bypasses_mmio_hooks);
    g_test_add_func("/dm-dma-mmio/fifo-status-write-mask",
                    test_fifo_status_and_write_mask);
    g_test_add_func("/dm-dma-mmio/w1c-flags-irq", test_w1c_flags_and_irq_gates);
    g_test_add_func("/dm-dma-mmio/w1c-reserved-subword",
                    test_w1c_reserved_and_subword);
    g_test_add_func("/dm-dma-mmio/forced-irq-projection",
                    test_forced_irq_projection);
    g_test_add_data_func("/dm-dma-mmio/m2p-configured-par/single",
                        GUINT_TO_POINTER(0), test_m2p_configured_par);
    g_test_add_data_func("/dm-dma-mmio/m2p-configured-par/batch",
                        GUINT_TO_POINTER(1), test_m2p_configured_par);
    g_test_add_data_func("/dm-dma-mmio/m2p-configured-par/coalesced-fallback",
                        GUINT_TO_POINTER(2), test_m2p_configured_par);
    g_test_add_func("/dm-dma-mmio/endpoint-retry-cache",
                    test_endpoint_retry_and_plain_request_cache);
    g_test_add_func("/dm-dma-mmio/endpoint-cache/ndtr-recapture",
                    test_endpoint_cache_ndtr_recapture);
    g_test_add_data_func("/dm-dma-mmio/endpoint-cache/subword-pinc/direct",
                        GUINT_TO_POINTER(0), test_endpoint_cache_subword_pinc);
    g_test_add_data_func("/dm-dma-mmio/endpoint-cache/subword-pinc/fifo",
                        GUINT_TO_POINTER(1), test_endpoint_cache_subword_pinc);
    g_test_add_data_func("/dm-dma-mmio/endpoint-cache/subword-pinc/advance-direct",
                        GUINT_TO_POINTER(2), test_endpoint_cache_subword_pinc);
    g_test_add_data_func("/dm-dma-mmio/endpoint-cache/subword-pinc/advance-fifo",
                        GUINT_TO_POINTER(3), test_endpoint_cache_subword_pinc);
    g_test_add_func("/dm-dma-mmio/request-cache-mux-identity",
                    test_request_cache_mux_identity);
    g_test_add_func("/dm-dma-mmio/p2m-endpoint-reservation",
                    test_p2m_endpoint_reservation);
    g_test_add_func("/dm-dma-mmio/request-event-time-scopes",
                    test_request_event_time_scopes);
    return g_test_run();
}
