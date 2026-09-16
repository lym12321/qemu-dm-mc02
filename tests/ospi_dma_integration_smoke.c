#include "qemu/osdep.h"

#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "hw/arm/dm_mc02_dma.h"
#include "hw/arm/dm_mc02_ospi.h"
#include "sysemu/dma.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
    RAM_BASE = 0x00100000u,
    RAM_SIZE = 0x00001000u,
    OSPI_DMA_ENDPOINT = 0x50000000u,

    OCTOSPI_CR = 0x000,
    OCTOSPI_DLR = 0x040,
    OCTOSPI_AR = 0x048,
    OCTOSPI_CCR = 0x100,
    OCTOSPI_IR = 0x110,

    DMA_LISR = 0x000,
    DMA_STREAM_BASE = 0x010,
    DMA_STREAM_STRIDE = 0x018,
    DMA_SxCR = 0x00,
    DMA_SxNDTR = 0x04,
    DMA_SxPAR = 0x08,
    DMA_SxM0AR = 0x0c,

    DMAMUX_CXCR_STRIDE = 4,

    DMA_CR_EN = 1u << 0,
    DMA_CR_DIR_M2P = 1u << 6,
    DMA_CR_MINC = 1u << 10,
    DMA_CR_TCIF = 1u << 5,
    DMA_CR_TEIF = 1u << 3,
    DMA_CR_PSIZE_8 = 0u << 11,
    DMA_CR_MSIZE_8 = 0u << 13,

    OCTOSPI_FMODE_MEMORY = 3u << 28,

    W25Q_WRITE_ENABLE = 0x06,
    W25Q_PAGE_PROGRAM = 0x02,
    W25Q_READ = 0x03,
};

AddressSpace address_space_memory;

static uint8_t ram_bytes[RAM_SIZE];

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
    if (!data || address < RAM_BASE || size > RAM_SIZE ||
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
        fprintf(stderr, "OSPI DMA integration smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static hwaddr dma_stream_register(unsigned stream, hwaddr offset)
{
    return DMA_STREAM_BASE + stream * DMA_STREAM_STRIDE + offset;
}

static void mmio_write(MemoryRegion *region, hwaddr offset, uint32_t value)
{
    region->ops->write(region->opaque, offset, value, sizeof(value));
}

static uint32_t dma_read(const DmMc02Dma *dma, hwaddr offset)
{
    return (uint32_t)dma->iomem.ops->read(dma->iomem.opaque, offset,
                                          sizeof(uint32_t));
}

static void dma_write(DmMc02Dma *dma, hwaddr offset, uint32_t value)
{
    mmio_write(&dma->iomem, offset, value);
}

static void dmamux_write(DmMc02Dmamux *dmamux, unsigned stream,
                         uint32_t request)
{
    mmio_write(&dmamux->iomem, stream * DMAMUX_CXCR_STRIDE, request);
}

static void ospi_write(DmMc02Ospi *ospi, hwaddr offset, uint32_t value)
{
    mmio_write(&ospi->iomem, offset, value);
}

static void prepare_page_program(DmMc02Ospi *ospi, hwaddr address,
                                  unsigned size)
{
    ospi_write(ospi, OCTOSPI_IR, W25Q_WRITE_ENABLE);
    ospi_write(ospi, OCTOSPI_DLR, size - 1);
    ospi_write(ospi, OCTOSPI_CCR, 0);
    ospi_write(ospi, OCTOSPI_IR, W25Q_PAGE_PROGRAM);
    ospi_write(ospi, OCTOSPI_AR, address);
}

static void prepare_read(DmMc02Ospi *ospi, hwaddr address, unsigned size)
{
    ospi_write(ospi, OCTOSPI_DLR, size - 1);
    ospi_write(ospi, OCTOSPI_CCR, 0);
    ospi_write(ospi, OCTOSPI_IR, W25Q_READ);
    ospi_write(ospi, OCTOSPI_AR, address);
}

static void enable_memory_mapped(DmMc02Ospi *ospi)
{
    ospi_write(ospi, OCTOSPI_CR, OCTOSPI_FMODE_MEMORY);
}

static void configure_stream(DmMc02Dma *dma, DmMc02Dmamux *dmamux,
                             unsigned stream, uint32_t request,
                             bool memory_to_peripheral, hwaddr m0ar,
                             unsigned count)
{
    uint32_t control = DMA_CR_EN | DMA_CR_MINC | DMA_CR_PSIZE_8 |
                       DMA_CR_MSIZE_8;

    if (memory_to_peripheral) {
        control |= DMA_CR_DIR_M2P;
    }
    dmamux_write(dmamux, stream, request);
    dma_write(dma, dma_stream_register(stream, DMA_SxPAR),
              OSPI_DMA_ENDPOINT);
    dma_write(dma, dma_stream_register(stream, DMA_SxM0AR), m0ar);
    dma_write(dma, dma_stream_register(stream, DMA_SxNDTR), count);
    dma_write(dma, dma_stream_register(stream, DMA_SxCR), control);
}

static uint8_t flash_read_byte(DmMc02Ospi *ospi, hwaddr offset)
{
    return (uint8_t)ospi->flash_window.ops->read(
        ospi->flash_window.opaque, offset, sizeof(uint8_t));
}

static void test_dma_m2p_programs_flash(DmMc02Dma *dma,
                                        DmMc02Dmamux *dmamux,
                                        DmMc02Ospi *ospi)
{
    static const uint8_t data[] = { 0x21, 0x43, 0x65, 0x87 };
    const unsigned stream = 0;
    const uint32_t request = 41;
    const hwaddr address = 0x500;
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(ospi);

    memset(ram_bytes + 0x100, 0, sizeof(data));
    memcpy(ram_bytes + 0x100, data, sizeof(data));
    prepare_page_program(ospi, address, sizeof(data));
    configure_stream(dma, dmamux, stream, request, true,
                     RAM_BASE + 0x100, sizeof(data));

    for (unsigned i = 0; i < sizeof(data); ++i) {
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, request, OSPI_DMA_ENDPOINT, &endpoint,
            1000 + i));
        CHECK(dma_read(dma, dma_stream_register(stream, DMA_SxNDTR)) ==
              sizeof(data) - i - 1);
    }
    CHECK(dma_read(dma, dma_stream_register(stream, DMA_SxNDTR)) == 0);
    CHECK(!(dma_read(dma, dma_stream_register(stream, DMA_SxCR)) &
            DMA_CR_EN));
    CHECK((dma_read(dma, DMA_LISR) & DMA_CR_TCIF) != 0);

    enable_memory_mapped(ospi);
    for (size_t i = 0; i < sizeof(data); ++i) {
        CHECK(flash_read_byte(ospi, address + i) == data[i]);
    }
}

static void test_dma_p2m_reads_flash(DmMc02Dma *dma,
                                     DmMc02Dmamux *dmamux,
                                     DmMc02Ospi *ospi)
{
    static const uint8_t expected[] = { 0x21, 0x43, 0x65, 0x87 };
    const unsigned stream = 1;
    const uint32_t request = 42;
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(ospi);
    uint8_t actual[sizeof(expected)] = { 0 };

    dm_mc02_ospi_reset(ospi);
    memset(ram_bytes + 0x200, 0, sizeof(actual));
    prepare_read(ospi, 0x500, sizeof(expected));
    configure_stream(dma, dmamux, stream, request, false,
                     RAM_BASE + 0x200, sizeof(expected));

    for (unsigned i = 0; i < sizeof(expected); ++i) {
        CHECK(dm_mc02_dma_request_endpoint(
            dma, dmamux, request, OSPI_DMA_ENDPOINT, &endpoint,
            2000 + i));
    }
    memcpy(actual, ram_bytes + 0x200, sizeof(actual));
    CHECK(!memcmp(actual, expected, sizeof(actual)));
    CHECK(dma_read(dma, dma_stream_register(stream, DMA_SxNDTR)) == 0);
    CHECK(!(dma_read(dma, dma_stream_register(stream, DMA_SxCR)) &
            DMA_CR_EN));
    CHECK((dma_read(dma, DMA_LISR) & (DMA_CR_TCIF << 6)) != 0);
}

static void test_endpoint_error_latches_dma_error(DmMc02Dma *dma,
                                                  DmMc02Dmamux *dmamux,
                                                  DmMc02Ospi *ospi)
{
    const unsigned stream = 2;
    const uint32_t request = 43;
    const hwaddr memory = RAM_BASE + 0x300;
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(ospi);

    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    dm_mc02_ospi_reset(ospi);
    ram_bytes[0x300] = 0xa5;
    configure_stream(dma, dmamux, stream, request, false, memory, 1);

    /* No active OSPI read exists after reset, so the endpoint must reject the
     * P2M beat before DMA commits it to guest RAM. */
    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, request, OSPI_DMA_ENDPOINT, &endpoint, 3000));
    CHECK(ram_bytes[0x300] == 0xa5);
    CHECK(dma_read(dma, dma_stream_register(stream, DMA_SxNDTR)) == 1);
    CHECK(!(dma_read(dma, dma_stream_register(stream, DMA_SxCR)) &
            DMA_CR_EN));
    CHECK((dma_read(dma, DMA_LISR) & (DMA_CR_TEIF << 16)) != 0);
}

static void test_p2m_ram_failure_preserves_ospi_source(
    DmMc02Dma *dma, DmMc02Dmamux *dmamux, DmMc02Ospi *ospi)
{
    const unsigned stream = 3;
    const uint32_t request = 44;
    const hwaddr invalid_memory = RAM_BASE + RAM_SIZE;
    const hwaddr valid_memory = RAM_BASE + 0x340;
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(ospi);

    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    dm_mc02_ospi_reset(ospi);
    ram_bytes[0x340] = 0xa7;
    prepare_read(ospi, 0x500, 1);
    configure_stream(dma, dmamux, stream, request, false, invalid_memory, 1);

    /* The OCTOSPI endpoint reserves its RX byte.  The failed destination
     * must abort that reservation, so a freshly configured DMA stream can
     * read the same byte rather than silently dropping it. */
    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, request, OSPI_DMA_ENDPOINT, &endpoint, 4000));
    CHECK(ram_bytes[0x340] == 0xa7);
    CHECK(dma_read(dma, dma_stream_register(stream, DMA_SxNDTR)) == 1);
    CHECK(!(dma_read(dma, dma_stream_register(stream, DMA_SxCR)) &
            DMA_CR_EN));
    CHECK((dma_read(dma, DMA_LISR) & (DMA_CR_TEIF << 22)) != 0);

    dm_mc02_dma_reset(dma);
    dm_mc02_dmamux_reset(dmamux);
    configure_stream(dma, dmamux, stream, request, false, valid_memory, 1);
    CHECK(dm_mc02_dma_request_endpoint(
        dma, dmamux, request, OSPI_DMA_ENDPOINT, &endpoint, 4001));
    CHECK(ram_bytes[0x340] == 0x21);
    CHECK(dma_read(dma, dma_stream_register(stream, DMA_SxNDTR)) == 0);
    CHECK((dma_read(dma, DMA_LISR) & (DMA_CR_TEIF << 22)) == 0);
}

int main(void)
{
    DmMc02Dma dma;
    DmMc02Dmamux dmamux;
    DmMc02Ospi ospi;

    dm_mc02_dma_init(&dma, NULL, "ospi-dma-smoke-dma");
    dm_mc02_dmamux_init(&dmamux, NULL, "ospi-dma-smoke-dmamux");
    dm_mc02_ospi_init(&ospi, NULL);

    test_dma_m2p_programs_flash(&dma, &dmamux, &ospi);
    dm_mc02_dma_reset(&dma);
    dm_mc02_dmamux_reset(&dmamux);
    test_dma_p2m_reads_flash(&dma, &dmamux, &ospi);
    test_endpoint_error_latches_dma_error(&dma, &dmamux, &ospi);
    test_p2m_ram_failure_preserves_ospi_source(&dma, &dmamux, &ospi);

    dm_mc02_ospi_cleanup(&ospi);
    puts("RESULT: OSPI DMA integration smoke passed");
    return 0;
}
