#include "qemu/osdep.h"

#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "hw/arm/dm_mc02_dma.h"
#include "sysemu/dma.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define RAM_BASE             0x00100000u
#define RAM_SIZE             0x00001000u
#define M2P_PERIPHERAL       0x50000000u
#define P2M_PERIPHERAL       0x50000004u

#define DMA_LISR             0x000u
#define DMA_STREAM_BASE      0x010u
#define DMA_STREAM_STRIDE    0x018u
#define DMA_SxCR             0x00u
#define DMA_SxNDTR           0x04u
#define DMA_SxPAR            0x08u
#define DMA_SxM0AR           0x0cu
#define DMA_SxM1AR           0x10u
#define DMA_SxFCR            0x14u

#define DMAMUX_CxCR(stream)  ((stream) * 4u)

#define DMA_CR_EN            (1u << 0)
#define DMA_CR_HTIE          (1u << 3)
#define DMA_CR_TCIE          (1u << 4)
#define DMA_CR_DIR_M2P       (1u << 6)
#define DMA_CR_MINC          (1u << 10)
#define DMA_CR_PSIZE_8       (0u << 11)
#define DMA_CR_MSIZE_16      (1u << 13)
#define DMA_CR_DBM           (1u << 18)
#define DMA_CR_PSIZE_16      (1u << 11)
#define DMA_CR_PBURST_SHIFT  21
#define DMA_CR_MBURST_SHIFT  23
#define DMA_CR_FTH_HALF      (1u << 0)
#define DMA_CR_FTH_FULL      (3u << 0)
#define DMA_FCR_DMDIS        (1u << 2)
#define DMA_FCR_FEIE         (1u << 7)
#define DMA_FLAG_HTIF(stream) (1u << (4u + ((stream) ? 6u : 0u)))
#define DMA_FLAG_TCIF(stream) (1u << (5u + ((stream) ? 6u : 0u)))
#define DMA_FLAG_TEIF(stream) (1u << (3u + ((stream) ? 6u : 0u)))
#define DMA_STREAM2_TCIF     (1u << 21)
#define DMA_STREAM2_TEIF     (1u << 19)
#define DMA_STREAM2_FEIF     (1u << 16)

typedef struct EndpointProbe {
    uint8_t input[16];
    uint8_t observed[16];
    uint64_t timestamps[16];
    unsigned calls;
    unsigned size_errors;
    unsigned retry_reads;
    unsigned retry_writes;
} EndpointProbe;

typedef struct ReservedReadProbe {
    uint8_t input[2];
    uint8_t reserved_data;
    uint64_t reserved_timestamp_ns;
    unsigned prepare_calls;
    unsigned commit_calls;
    unsigned abort_calls;
    bool reserved;
} ReservedReadProbe;

static uint8_t ram_bytes[RAM_SIZE];

/* The chip-layer smoke supplies only the address-space transport seam.  The
 * DMA implementation still performs all register, FIFO, DBM, and endpoint
 * work; this stub keeps the test independent of the full QEMU system link. */
AddressSpace address_space_memory;

void qemu_set_irq(qemu_irq irq, int level)
{
    (void)irq;
    (void)level;
}

void memory_region_init_io(MemoryRegion *mr, Object *owner,
                           const MemoryRegionOps *ops, void *opaque,
                           const char *name, uint64_t size)
{
    (void)owner;
    (void)name;
    (void)size;
    mr->ops = ops;
    mr->opaque = opaque;
}

MemTxResult address_space_rw(AddressSpace *as, hwaddr address,
                             MemTxAttrs attrs, void *data, hwaddr size,
                             bool is_write)
{
    (void)as;
    (void)attrs;
    if (address < RAM_BASE || size > RAM_SIZE ||
        address - RAM_BASE > RAM_SIZE - size) {
        return MEMTX_ERROR;
    }
    if (is_write) {
        memcpy(ram_bytes + address - RAM_BASE, data, size);
    } else {
        memcpy(data, ram_bytes + address - RAM_BASE, size);
    }
    return MEMTX_OK;
}

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "DMA FIFO/DBM endpoint smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static hwaddr stream_register(unsigned stream, hwaddr offset)
{
    return DMA_STREAM_BASE + stream * DMA_STREAM_STRIDE + offset;
}

static void dma_write(DmMc02Dma *dma, hwaddr offset, uint32_t value)
{
    dma->iomem.ops->write(dma->iomem.opaque, offset, value, sizeof(value));
}

static uint32_t dma_read(const DmMc02Dma *dma, hwaddr offset)
{
    return (uint32_t)dma->iomem.ops->read(dma->iomem.opaque, offset,
                                          sizeof(uint32_t));
}

static void dmamux_write(DmMc02Dmamux *dmamux, unsigned stream,
                         uint32_t request)
{
    dmamux->iomem.ops->write(dmamux->iomem.opaque,
                              DMAMUX_CxCR(stream), request,
                              sizeof(request));
}

static void memory_read(hwaddr address, void *data, unsigned size)
{
    CHECK(dma_memory_read(&address_space_memory, address, data, size,
                          MEMTXATTRS_UNSPECIFIED) == MEMTX_OK);
}

static bool endpoint_read(void *opaque, uint8_t *data, unsigned size,
                          uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    if (size != 1 || probe->calls >= ARRAY_SIZE(probe->input)) {
        probe->size_errors++;
        return false;
    }
    data[0] = probe->input[probe->calls];
    probe->observed[probe->calls] = data[0];
    probe->timestamps[probe->calls] = timestamp_ns;
    probe->calls++;
    return true;
}

static DmMc02DmaEndpointResult endpoint_read_retry_ex(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    if (probe->retry_reads) {
        probe->retry_reads--;
        return DM_MC02_DMA_ENDPOINT_RETRY;
    }
    return endpoint_read(opaque, data, size, timestamp_ns) ?
           DM_MC02_DMA_ENDPOINT_ACCEPTED : DM_MC02_DMA_ENDPOINT_ERROR;
}

static DmMc02DmaEndpointResult endpoint_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    ReservedReadProbe *probe = opaque;

    if (!data || size != 1 || probe->reserved ||
        probe->commit_calls >= ARRAY_SIZE(probe->input)) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    probe->reserved_data = probe->input[probe->commit_calls];
    probe->reserved_timestamp_ns = timestamp_ns;
    data[0] = probe->reserved_data;
    probe->reserved = true;
    probe->prepare_calls++;
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void endpoint_read_commit(void *opaque)
{
    ReservedReadProbe *probe = opaque;

    CHECK(probe->reserved);
    probe->reserved = false;
    probe->commit_calls++;
}

static void endpoint_read_abort(void *opaque)
{
    ReservedReadProbe *probe = opaque;

    CHECK(probe->reserved);
    probe->reserved = false;
    probe->abort_calls++;
}

static bool endpoint_write(void *opaque, const uint8_t *data, unsigned size,
                           uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    if (size != 1 || probe->calls >= ARRAY_SIZE(probe->observed)) {
        probe->size_errors++;
        return false;
    }
    probe->observed[probe->calls] = data[0];
    probe->timestamps[probe->calls] = timestamp_ns;
    probe->calls++;
    return true;
}

static DmMc02DmaEndpointResult endpoint_write_retry_ex(
    void *opaque, const uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    EndpointProbe *probe = opaque;

    if (probe->retry_writes) {
        probe->retry_writes--;
        return DM_MC02_DMA_ENDPOINT_RETRY;
    }
    return endpoint_write(opaque, data, size, timestamp_ns) ?
           DM_MC02_DMA_ENDPOINT_ACCEPTED : DM_MC02_DMA_ENDPOINT_ERROR;
}

static void configure_dbm_fifo(DmMc02Dma *dma, DmMc02Dmamux *dmamux,
                               unsigned stream, uint32_t request,
                               hwaddr peripheral, hwaddr m0ar, hwaddr m1ar,
                               bool memory_to_peripheral)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_MSIZE_16 |
                       DMA_CR_PSIZE_8 | DMA_CR_DBM |
                       DMA_CR_HTIE | DMA_CR_TCIE |
                       (DM_MC02_DMA_BURST_INCR4 << DMA_CR_MBURST_SHIFT) |
                       (DM_MC02_DMA_BURST_SINGLE << DMA_CR_PBURST_SHIFT);

    if (memory_to_peripheral) {
        control |= DMA_CR_DIR_M2P;
    }

    dmamux_write(dmamux, stream, request);
    dma_write(dma, stream_register(stream, DMA_SxPAR), peripheral);
    dma_write(dma, stream_register(stream, DMA_SxM0AR), m0ar);
    dma_write(dma, stream_register(stream, DMA_SxM1AR), m1ar);
    dma_write(dma, stream_register(stream, DMA_SxNDTR), 8);
    dma_write(dma, stream_register(stream, DMA_SxFCR),
              DMA_FCR_DMDIS | DMA_CR_FTH_HALF);
    dma_write(dma, stream_register(stream, DMA_SxCR), control);
}

static void configure_m2p_fifo(DmMc02Dma *dma, DmMc02Dmamux *dmamux,
                               unsigned memory_burst,
                               unsigned peripheral_burst)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_MSIZE_16 |
                       DMA_CR_PSIZE_8 | DMA_CR_DIR_M2P |
                       (memory_burst << DMA_CR_MBURST_SHIFT) |
                       (peripheral_burst << DMA_CR_PBURST_SHIFT);

    dmamux_write(dmamux, 0, 19);
    dma_write(dma, stream_register(0, DMA_SxPAR), M2P_PERIPHERAL);
    dma_write(dma, stream_register(0, DMA_SxM0AR), RAM_BASE + 0x300);
    dma_write(dma, stream_register(0, DMA_SxNDTR), 8);
    dma_write(dma, stream_register(0, DMA_SxFCR), DMA_FCR_DMDIS | DMA_CR_FTH_FULL);
    dma_write(dma, stream_register(0, DMA_SxCR), control);
}

static void configure_m2p_direct_retry(DmMc02Dma *dma,
                                       DmMc02Dmamux *dmamux)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_DIR_M2P |
                       DMA_CR_TCIE;

    dmamux_write(dmamux, 2, 23);
    dma_write(dma, stream_register(2, DMA_SxPAR), M2P_PERIPHERAL);
    dma_write(dma, stream_register(2, DMA_SxM0AR), RAM_BASE + 0x380);
    dma_write(dma, stream_register(2, DMA_SxNDTR), 1);
    dma_write(dma, stream_register(2, DMA_SxFCR), 0);
    dma_write(dma, stream_register(2, DMA_SxCR), control);
}

static void configure_m2p_fifo_retry(DmMc02Dma *dma,
                                     DmMc02Dmamux *dmamux)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_DIR_M2P |
                       DMA_CR_MSIZE_16 | DMA_CR_TCIE;

    dmamux_write(dmamux, 2, 24);
    dma_write(dma, stream_register(2, DMA_SxPAR), M2P_PERIPHERAL);
    dma_write(dma, stream_register(2, DMA_SxM0AR), RAM_BASE + 0x388);
    dma_write(dma, stream_register(2, DMA_SxNDTR), 1);
    /* Quarter-full threshold (FTH=0) leaves a committed partial FIFO after
     * the first accepted byte, so a later RETRY exercises speculative fill. */
    dma_write(dma, stream_register(2, DMA_SxFCR), DMA_FCR_DMDIS);
    dma_write(dma, stream_register(2, DMA_SxCR), control);
}

static void configure_m2p_fifo_retry_sequence(DmMc02Dma *dma,
                                               DmMc02Dmamux *dmamux)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_DIR_M2P |
                       DMA_CR_MSIZE_16 | DMA_CR_TCIE;

    dmamux_write(dmamux, 2, 27);
    dma_write(dma, stream_register(2, DMA_SxPAR), M2P_PERIPHERAL);
    dma_write(dma, stream_register(2, DMA_SxM0AR), RAM_BASE + 0x390);
    dma_write(dma, stream_register(2, DMA_SxNDTR), 8);
    dma_write(dma, stream_register(2, DMA_SxFCR), DMA_FCR_DMDIS);
    dma_write(dma, stream_register(2, DMA_SxCR), control);
}

static void configure_p2m_direct_retry(DmMc02Dma *dma,
                                       DmMc02Dmamux *dmamux, hwaddr m0ar)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_TCIE;

    dmamux_write(dmamux, 2, 25);
    dma_write(dma, stream_register(2, DMA_SxPAR), P2M_PERIPHERAL);
    dma_write(dma, stream_register(2, DMA_SxM0AR), m0ar);
    dma_write(dma, stream_register(2, DMA_SxNDTR), 1);
    dma_write(dma, stream_register(2, DMA_SxFCR), 0);
    dma_write(dma, stream_register(2, DMA_SxCR), control);
}

static void configure_p2m_fifo_retry(DmMc02Dma *dma,
                                     DmMc02Dmamux *dmamux)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_MSIZE_16 |
                       DMA_CR_TCIE;

    dmamux_write(dmamux, 2, 26);
    dma_write(dma, stream_register(2, DMA_SxPAR), P2M_PERIPHERAL);
    dma_write(dma, stream_register(2, DMA_SxM0AR), RAM_BASE + 0x3c0);
    dma_write(dma, stream_register(2, DMA_SxNDTR), 2);
    dma_write(dma, stream_register(2, DMA_SxFCR),
              DMA_FCR_DMDIS | DMA_CR_FTH_FULL);
    dma_write(dma, stream_register(2, DMA_SxCR), control);
}

static void configure_p2m_fifo_overflow(DmMc02Dma *dma,
                                        DmMc02Dmamux *dmamux,
                                        bool fifo_error_irq)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_PSIZE_16 |
                       DMA_CR_MSIZE_16 | DMA_CR_TCIE;
    uint32_t fcr = DMA_FCR_DMDIS | DMA_CR_FTH_FULL;

    if (fifo_error_irq) {
        fcr |= DMA_FCR_FEIE;
    }
    dmamux_write(dmamux, 2, 28);
    dma_write(dma, stream_register(2, DMA_SxPAR), P2M_PERIPHERAL);
    dma_write(dma, stream_register(2, DMA_SxM0AR), RAM_BASE + 0x3e0);
    dma_write(dma, stream_register(2, DMA_SxNDTR), 2);
    dma_write(dma, stream_register(2, DMA_SxFCR), fcr);
    dma_write(dma, stream_register(2, DMA_SxCR), control);
}

static void test_p2m_fifo_overflow_is_transactional(
    DmMc02Dma *dma, DmMc02Dmamux *dmamux)
{
    for (unsigned fifo_error_irq = 0; fifo_error_irq <= 1;
         ++fifo_error_irq) {
        EndpointProbe probe = { 0 };
        DmMc02DmaEndpoint endpoint = {
            .read_ex = endpoint_read_retry_ex,
            .opaque = &probe,
        };
        hwaddr m0ar = RAM_BASE + 0x3e0;

        dm_mc02_dma_reset(dma);
        dm_mc02_dmamux_reset(dmamux);
        configure_p2m_fifo_overflow(dma, dmamux, fifo_error_irq);
        memset(ram_bytes + 0x3e0, 0xa9, 4);
        for (unsigned i = 0; i < DM_MC02_DMA_FIFO_BYTES; ++i) {
            dma->fifo[2][i] = (uint8_t)(0xc0 + i);
        }
        dma->fifo_head[2] = 0;
        dma->fifo_length[2] = DM_MC02_DMA_FIFO_BYTES;

        /* The FIFO is already full, so the two-byte peripheral beat cannot
         * be accepted.  The endpoint must not be read merely to discover
         * that boundary. */
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, 28, P2M_PERIPHERAL, &endpoint, 7050));
        CHECK(probe.calls == 0);
        CHECK(probe.size_errors == 0);
        CHECK(ram_bytes[0x3e0] == 0xa9);
        CHECK(ram_bytes[0x3e1] == 0xa9);
        CHECK(dma_read(dma, stream_register(2, DMA_SxPAR)) ==
              P2M_PERIPHERAL);
        CHECK(dma_read(dma, stream_register(2, DMA_SxM0AR)) == m0ar);
        CHECK(dma->cursor_m0ar[2] == m0ar);
        CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 2);
        CHECK(!(dma_read(dma, stream_register(2, DMA_SxCR)) & DMA_CR_EN));
        CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_FEIF) != 0);
        CHECK(dma->fifo_head[2] == 0);
        CHECK(dma->fifo_length[2] == 0);
        CHECK(((dma_read(dma, stream_register(2, DMA_SxFCR)) >> 3) & 7) ==
              0);
        CHECK(dma->stream_irq_level_valid[2]);
        CHECK(dma->stream_irq_level[2] == fifo_error_irq);
    }
}

static void test_endpoint_retry_preserves_dma_state(
    DmMc02Dma *dma, DmMc02Dmamux *dmamux)
{
    EndpointProbe probe = { .retry_writes = 1 };
    DmMc02DmaEndpoint endpoint = {
        .write_ex = endpoint_write_retry_ex,
        .opaque = &probe,
    };
    hwaddr m0ar = RAM_BASE + 0x380;

    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    ram_bytes[0x380] = 0x5a;
    configure_m2p_direct_retry(dma, dmamux);

    CHECK(!dm_mc02_dma_request_endpoint(
        dma, dmamux, 23, M2P_PERIPHERAL, &endpoint, 7000));
    CHECK(probe.calls == 0);
    CHECK(dma_read(dma, stream_register(2, DMA_SxCR)) & DMA_CR_EN);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 1);
    CHECK(dma_read(dma, stream_register(2, DMA_SxM0AR)) == m0ar);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);

    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, 23, M2P_PERIPHERAL, &endpoint, 7001));
    CHECK(probe.calls == 1);
    CHECK(probe.observed[0] == 0x5a);
    CHECK(probe.timestamps[0] == 7001);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 0);
    CHECK(dma_read(dma, stream_register(2, DMA_SxM0AR)) == m0ar);
    CHECK(dma->cursor_m0ar[2] == m0ar + 1);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TCIF) != 0);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);

    /* A bounded batch must stop at the first backpressured beat instead of
     * spinning on the same stream.  The next caller-owned batch retries it. */
    memset(&probe, 0, sizeof(probe));
    probe.retry_writes = 1;
    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    ram_bytes[0x380] = 0x6b;
    configure_m2p_direct_retry(dma, dmamux);
    CHECK(!dm_mc02_dma_request_endpoint_batch(
        dma, dmamux, 23, M2P_PERIPHERAL, &endpoint, 7010, 2));
    CHECK(probe.calls == 0);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 1);
    CHECK(dma_read(dma, stream_register(2, DMA_SxCR)) & DMA_CR_EN);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);
    CHECK(dm_mc02_dma_request_endpoint_batch(
        dma, dmamux, 23, M2P_PERIPHERAL, &endpoint, 7011, 2));
    CHECK(probe.calls == 1);
    CHECK(probe.observed[0] == 0x6b);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 0);

    /* A failed first beat must not commit speculative FIFO prefetch. */
    memset(&probe, 0, sizeof(probe));
    probe.retry_writes = 1;
    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    ram_bytes[0x388] = 0x7c;
    ram_bytes[0x389] = 0x7d;
    configure_m2p_fifo_retry(dma, dmamux);
    CHECK(!dm_mc02_dma_request_endpoint(
        dma, dmamux, 24, M2P_PERIPHERAL, &endpoint, 7020));
    CHECK(probe.calls == 0);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 1);
    CHECK(dma->fifo_length[2] == 0);
    CHECK((dma_read(dma, stream_register(2, DMA_SxFCR)) >> 3 & 7) == 0);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);
    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, 24, M2P_PERIPHERAL, &endpoint, 7021));
    CHECK(probe.calls == 1);
    CHECK(probe.observed[0] == 0x7c);
    CHECK(probe.timestamps[0] == 7021);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 0);
    CHECK((dma_read(dma, stream_register(2, DMA_SxFCR)) >> 3 & 7) == 0);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TCIF) != 0);

    /* A retry after an already accepted beat can happen while FIFO fill
     * speculatively reads the next memory word.  The rejected fill must not
     * be retained without its matching memory cursor, or later requests
     * would eventually repeat the prefetched bytes. */
    memset(&probe, 0, sizeof(probe));
    for (unsigned i = 0; i < 8; ++i) {
        ram_bytes[0x390 + i] = (uint8_t)(0xa1 + i);
    }
    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    configure_m2p_fifo_retry_sequence(dma, dmamux);
    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, 27, M2P_PERIPHERAL, &endpoint, 7022));
    CHECK(probe.calls == 1);
    CHECK(probe.observed[0] == 0xa1);
    CHECK(dma->fifo_length[2] == 3);
    CHECK(dma->cursor_m0ar[2] == RAM_BASE + 0x394);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 7);

    probe.retry_writes = 1;
    CHECK(!dm_mc02_dma_request_endpoint(
        dma, dmamux, 27, M2P_PERIPHERAL, &endpoint, 7023));
    CHECK(probe.calls == 1);
    CHECK(dma->fifo_length[2] == 3);
    CHECK(dma->cursor_m0ar[2] == RAM_BASE + 0x394);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 7);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);

    for (unsigned i = 1; i < 8; ++i) {
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, 27, M2P_PERIPHERAL, &endpoint, 7024 + i));
        CHECK(probe.observed[i] == (uint8_t)(0xa1 + i));
    }
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 0);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TCIF) != 0);

    /* P2M direct retry must not publish a speculative byte to guest RAM or
     * advance the stream.  The same endpoint object is reused after the
     * caller-owned retry, so the accepted read also checks the original
     * timestamp and source byte. */
    memset(&probe, 0, sizeof(probe));
    probe.input[0] = 0x8e;
    probe.retry_reads = 1;
    endpoint.write_ex = NULL;
    endpoint.read_ex = endpoint_read_retry_ex;
    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    ram_bytes[0x3a0] = 0xa5;
    configure_p2m_direct_retry(dma, dmamux, RAM_BASE + 0x3a0);
    CHECK(!dm_mc02_dma_request_endpoint(
        dma, dmamux, 25, P2M_PERIPHERAL, &endpoint, 7030));
    CHECK(probe.calls == 0);
    CHECK(ram_bytes[0x3a0] == 0xa5);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 1);
    CHECK(dma_read(dma, stream_register(2, DMA_SxM0AR)) == RAM_BASE + 0x3a0);
    CHECK(dma->cursor_m0ar[2] == RAM_BASE + 0x3a0);
    CHECK(dma_read(dma, stream_register(2, DMA_SxCR)) & DMA_CR_EN);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);

    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, 25, P2M_PERIPHERAL, &endpoint, 7031));
    CHECK(probe.calls == 1);
    CHECK(probe.observed[0] == 0x8e);
    CHECK(probe.timestamps[0] == 7031);
    CHECK(ram_bytes[0x3a0] == 0x8e);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 0);
    CHECK(dma->cursor_m0ar[2] == RAM_BASE + 0x3a1);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TCIF) != 0);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);

    /* P2M FIFO retry must preserve bytes accepted by an earlier peripheral
     * request.  The second byte is retried before FIFO push; after it is
     * accepted, the final two-byte memory beat must contain both bytes. */
    memset(&probe, 0, sizeof(probe));
    probe.input[0] = 0x91;
    probe.input[1] = 0x92;
    endpoint.write_ex = NULL;
    endpoint.read_ex = endpoint_read_retry_ex;
    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    memset(ram_bytes + 0x3c0, 0xa6, 2);
    configure_p2m_fifo_retry(dma, dmamux);
    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, 26, P2M_PERIPHERAL, &endpoint, 7040));
    CHECK(probe.calls == 1);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 1);
    CHECK((dma_read(dma, stream_register(2, DMA_SxFCR)) >> 3 & 7) == 1);
    CHECK(dma->cursor_m0ar[2] == RAM_BASE + 0x3c0);
    CHECK(dma_read(dma, stream_register(2, DMA_SxCR)) & DMA_CR_EN);
    CHECK(!memcmp(ram_bytes + 0x3c0, (uint8_t[2]){ 0xa6, 0xa6 }, 2));

    probe.retry_reads = 1;
    CHECK(!dm_mc02_dma_request_endpoint(
        dma, dmamux, 26, P2M_PERIPHERAL, &endpoint, 7041));
    CHECK(probe.calls == 1);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 1);
    CHECK((dma_read(dma, stream_register(2, DMA_SxFCR)) >> 3 & 7) == 1);
    CHECK(dma->cursor_m0ar[2] == RAM_BASE + 0x3c0);
    CHECK(dma_read(dma, stream_register(2, DMA_SxCR)) & DMA_CR_EN);
    CHECK(!memcmp(ram_bytes + 0x3c0, (uint8_t[2]){ 0xa6, 0xa6 }, 2));
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);

    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, 26, P2M_PERIPHERAL, &endpoint, 7042));
    CHECK(probe.calls == 2);
    CHECK(probe.observed[1] == 0x92);
    CHECK(probe.timestamps[1] == 7042);
    CHECK(!memcmp(ram_bytes + 0x3c0, (uint8_t[2]){ 0x91, 0x92 }, 2));
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 0);
    CHECK(dma->cursor_m0ar[2] == RAM_BASE + 0x3c2);
    CHECK((dma_read(dma, stream_register(2, DMA_SxFCR)) >> 3 & 7) == 0);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TCIF) != 0);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);
}

static void test_p2m_direct_ram_failure_aborts_reservation(
    DmMc02Dma *dma, DmMc02Dmamux *dmamux)
{
    ReservedReadProbe probe = {
        .input = { 0xd1, 0xd2 },
    };
    DmMc02DmaEndpoint endpoint = {
        .read_prepare = endpoint_read_prepare,
        .read_commit = endpoint_read_commit,
        .read_abort = endpoint_read_abort,
        .opaque = &probe,
    };
    const hwaddr invalid_memory = RAM_BASE + RAM_SIZE;
    const hwaddr valid_memory = RAM_BASE + 0x3d0;

    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    ram_bytes[0x3d0] = 0xa4;
    configure_p2m_direct_retry(dma, dmamux, invalid_memory);

    /* The endpoint can hold the queue head until DMA knows the destination
     * write completed.  A rejected address must therefore abort, not consume
     * the byte or advance the DMA producer state. */
    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, 25, P2M_PERIPHERAL, &endpoint, 7060));
    CHECK(probe.prepare_calls == 1);
    CHECK(probe.abort_calls == 1);
    CHECK(probe.commit_calls == 0);
    CHECK(!probe.reserved);
    CHECK(ram_bytes[0x3d0] == 0xa4);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 1);
    CHECK(dma_read(dma, stream_register(2, DMA_SxM0AR)) == invalid_memory);
    CHECK(dma->cursor_m0ar[2] == invalid_memory);
    CHECK(!(dma_read(dma, stream_register(2, DMA_SxCR)) & DMA_CR_EN));
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) != 0);

    /* Reconfigure DMA without resetting the producer.  The same queued byte
     * must be the next direct-P2M source and commits only after its RAM write. */
    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    configure_p2m_direct_retry(dma, dmamux, valid_memory);
    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, 25, P2M_PERIPHERAL, &endpoint, 7061));
    CHECK(probe.prepare_calls == 2);
    CHECK(probe.abort_calls == 1);
    CHECK(probe.commit_calls == 1);
    CHECK(!probe.reserved);
    CHECK(ram_bytes[0x3d0] == 0xd1);
    CHECK(dma_read(dma, stream_register(2, DMA_SxNDTR)) == 0);
    CHECK(dma->cursor_m0ar[2] == valid_memory + 1);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TEIF) == 0);
    CHECK((dma_read(dma, DMA_LISR) & DMA_STREAM2_TCIF) != 0);
}

static void check_dbm_registers(const DmMc02Dma *dma, unsigned stream,
                                bool active_m1, hwaddr m0ar, hwaddr m1ar)
{
    uint32_t control = dma_read(dma, stream_register(stream, DMA_SxCR));

    CHECK((control & (DMA_CR_EN | DMA_CR_DBM)) ==
          (DMA_CR_EN | DMA_CR_DBM));
    CHECK(!!(control & (1u << 19)) == active_m1);
    CHECK(dma_read(dma, stream_register(stream, DMA_SxNDTR)) == 8);
    CHECK(dma_read(dma, stream_register(stream, DMA_SxM0AR)) == m0ar);
    CHECK(dma_read(dma, stream_register(stream, DMA_SxM1AR)) == m1ar);
}

static void check_timestamps(const EndpointProbe *probe, unsigned start,
                             unsigned count, uint64_t first)
{
    CHECK(start + count <= probe->calls);
    for (unsigned i = 0; i < count; ++i) {
        CHECK(probe->timestamps[start + i] == first + i);
    }
}

static void test_m2p(DmMc02Dma *dma, DmMc02Dmamux *dmamux)
{
    static const uint8_t m0[] = {
        0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
        0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
    };
    static const uint8_t m1[] = {
        0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
        0x88, 0x89, 0x8a, 0x8b, 0x8c, 0x8d, 0x8e, 0x8f,
    };
    EndpointProbe probe = { 0 };
    DmMc02DmaEndpoint endpoint = {
        .write = endpoint_write,
        .opaque = &probe,
    };

    memcpy(ram_bytes + 0x100, m0, sizeof(m0));
    memcpy(ram_bytes + 0x140, m1, sizeof(m1));
    configure_dbm_fifo(dma, dmamux, 0, 17, M2P_PERIPHERAL,
                       RAM_BASE + 0x100, RAM_BASE + 0x140, true);

    for (unsigned i = 0; i < 8; ++i) {
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, 17, M2P_PERIPHERAL, &endpoint, 1000 + i));
        CHECK(probe.observed[i] == m0[i]);
        CHECK(probe.timestamps[i] == 1000 + i);
        CHECK(dma_read(dma, stream_register(0, DMA_SxNDTR)) ==
              (i == 7 ? 8 : 7 - i));
        if (i == 0) {
            /* Four 16-bit memory beats have been prefetched.  One byte has
             * been consumed by the endpoint, leaving seven FIFO bytes; the
             * chip-layer FS encoding reports that as state 2. */
            CHECK((dma_read(dma, stream_register(0, DMA_SxFCR)) >> 3 & 7) ==
                  2);
        }
    }
    CHECK(probe.calls == 8);
    CHECK(probe.size_errors == 0);
    check_dbm_registers(dma, 0, true, RAM_BASE + 0x100, RAM_BASE + 0x140);
    CHECK((dma_read(dma, DMA_LISR) &
           (DMA_FLAG_HTIF(0) | DMA_FLAG_TCIF(0))) ==
          (DMA_FLAG_HTIF(0) | DMA_FLAG_TCIF(0)));
    CHECK((dma_read(dma, stream_register(0, DMA_SxFCR)) >> 3 & 7) == 0);

    for (unsigned i = 0; i < 8; ++i) {
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, 17, M2P_PERIPHERAL, &endpoint, 2000 + i));
        CHECK(probe.observed[8 + i] == m1[i]);
        CHECK(probe.timestamps[8 + i] == 2000 + i);
    }
    CHECK(probe.calls == 16);
    check_timestamps(&probe, 0, 8, 1000);
    check_timestamps(&probe, 8, 8, 2000);
    check_dbm_registers(dma, 0, false, RAM_BASE + 0x100, RAM_BASE + 0x140);
    CHECK((dma_read(dma, DMA_LISR) &
           (DMA_FLAG_HTIF(0) | DMA_FLAG_TCIF(0))) ==
          (DMA_FLAG_HTIF(0) | DMA_FLAG_TCIF(0)));
}

static void check_halfwords(hwaddr address, const uint8_t *bytes)
{
    uint8_t result[8] = { 0 };

    memory_read(address, result, sizeof(result));
    CHECK(!memcmp(result, bytes, sizeof(result)));
}

static void test_p2m(DmMc02Dma *dma, DmMc02Dmamux *dmamux)
{
    static const uint8_t input[] = {
        0x90, 0x91, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97,
        0xa0, 0xa1, 0xa2, 0xa3, 0xa4, 0xa5, 0xa6, 0xa7,
    };
    EndpointProbe probe = { 0 };
    DmMc02DmaEndpoint endpoint = {
        .read = endpoint_read,
        .opaque = &probe,
    };

    memcpy(probe.input, input, sizeof(input));
    memset(ram_bytes + 0x200, 0, 8);
    memset(ram_bytes + 0x240, 0, 8);
    configure_dbm_fifo(dma, dmamux, 1, 18, P2M_PERIPHERAL,
                       RAM_BASE + 0x200, RAM_BASE + 0x240, false);

    for (unsigned i = 0; i < 4; ++i) {
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, 18, P2M_PERIPHERAL, &endpoint, 3000 + i));
        CHECK(probe.observed[i] == input[i]);
        CHECK(dma_read(dma, stream_register(1, DMA_SxNDTR)) == 7 - i);
    }
    /* The four bytes are below the half threshold and must remain buffered. */
    CHECK((dma_read(dma, stream_register(1, DMA_SxFCR)) >> 3 & 7) == 1);
    CHECK(!memcmp(ram_bytes + 0x200, (uint8_t[8]){ 0 }, 8));

    for (unsigned i = 4; i < 8; ++i) {
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, 18, P2M_PERIPHERAL, &endpoint, 3000 + i));
        CHECK(probe.observed[i] == input[i]);
    }
    CHECK(probe.calls == 8);
    check_halfwords(RAM_BASE + 0x200, input);
    check_dbm_registers(dma, 1, true, RAM_BASE + 0x200, RAM_BASE + 0x240);
    CHECK((dma_read(dma, DMA_LISR) &
           (DMA_FLAG_HTIF(1) | DMA_FLAG_TCIF(1))) ==
          (DMA_FLAG_HTIF(1) | DMA_FLAG_TCIF(1)));
    CHECK((dma_read(dma, stream_register(1, DMA_SxFCR)) >> 3 & 7) == 0);

    for (unsigned i = 8; i < 16; ++i) {
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, 18, P2M_PERIPHERAL, &endpoint, 3000 + i));
        CHECK(probe.observed[i] == input[i]);
    }
    CHECK(probe.calls == 16);
    check_timestamps(&probe, 0, 16, 3000);
    check_halfwords(RAM_BASE + 0x240, input + 8);
    check_dbm_registers(dma, 1, false, RAM_BASE + 0x200, RAM_BASE + 0x240);
    CHECK((dma_read(dma, DMA_LISR) &
           (DMA_FLAG_HTIF(1) | DMA_FLAG_TCIF(1))) ==
          (DMA_FLAG_HTIF(1) | DMA_FLAG_TCIF(1)));
}

static void test_m2p_burst_codes(DmMc02Dma *dma, DmMc02Dmamux *dmamux)
{
    static const uint8_t input[] = {
        0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38,
        0x39, 0x3a, 0x3b, 0x3c, 0x3d, 0x3e, 0x3f, 0x40,
    };

    for (unsigned memory_burst = DM_MC02_DMA_BURST_SINGLE;
         memory_burst <= DM_MC02_DMA_BURST_INCR16; ++memory_burst) {
        for (unsigned peripheral_burst = DM_MC02_DMA_BURST_SINGLE;
             peripheral_burst <= DM_MC02_DMA_BURST_INCR16;
             ++peripheral_burst) {
            EndpointProbe probe = { 0 };
            DmMc02DmaEndpoint endpoint = {
                .write = endpoint_write,
                .opaque = &probe,
            };
            uint32_t control;

            dm_mc02_dma_reset(dma);
            dm_mc02_dmamux_reset(dmamux);
            memcpy(ram_bytes + 0x300, input, sizeof(input));
            configure_m2p_fifo(dma, dmamux, memory_burst,
                                peripheral_burst);
            control = dma_read(dma, stream_register(0, DMA_SxCR));
            CHECK(((control >> DMA_CR_MBURST_SHIFT) & 3u) == memory_burst);
            CHECK(((control >> DMA_CR_PBURST_SHIFT) & 3u) == peripheral_burst);

            for (unsigned i = 0; i < 8; ++i) {
                CHECK(dm_mc02_dma_request_endpoint(
                    dma, dmamux, 19, M2P_PERIPHERAL, &endpoint, 4000 + i));
                CHECK(probe.observed[i] == input[i]);
            }
            CHECK(probe.calls == 8);
            CHECK(probe.size_errors == 0);
            CHECK(dma_read(dma, stream_register(0, DMA_SxNDTR)) == 0);
            CHECK((dma_read(dma, DMA_LISR) & (1u << 5)) != 0);
            CHECK((dma_read(dma, stream_register(0, DMA_SxFCR)) >> 3 & 7) == 0);
        }
    }
}

int main(void)
{
    DmMc02Dma dma;
    DmMc02Dmamux dmamux;

    dm_mc02_dma_init(&dma, NULL, "dma-smoke-dma");
    dm_mc02_dmamux_init(&dmamux, NULL, "dma-smoke-dmamux");
    test_endpoint_retry_preserves_dma_state(&dma, &dmamux);
    test_p2m_direct_ram_failure_aborts_reservation(&dma, &dmamux);
    test_p2m_fifo_overflow_is_transactional(&dma, &dmamux);
    test_m2p(&dma, &dmamux);
    dm_mc02_dma_reset(&dma);
    dm_mc02_dmamux_reset(&dmamux);
    test_p2m(&dma, &dmamux);
    test_m2p_burst_codes(&dma, &dmamux);

    puts("RESULT: DMA FIFO/DBM endpoint M2P/P2M smoke passed");
    return 0;
}
