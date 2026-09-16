#include "qemu/osdep.h"

#include "exec/memory.h"
#include "hw/arm/dm_mc02_ospi.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

enum {
    OCTOSPI_CR = 0x000,
    OCTOSPI_SR = 0x020,
    OCTOSPI_DLR = 0x040,
    OCTOSPI_AR = 0x048,
    OCTOSPI_DR = 0x050,
    OCTOSPI_CCR = 0x100,
    OCTOSPI_IR = 0x110,
};

enum {
    OCTOSPI_FMODE_MEMORY = 3u << 28,
};

enum {
    W25Q_WRITE_ENABLE = 0x06,
    W25Q_JEDEC_ID = 0x9f,
    W25Q_PAGE_PROGRAM = 0x02,
};

AddressSpace address_space_memory;

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

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "OSPI DMA endpoint smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static void mmio_write(DmMc02Ospi *state, hwaddr offset, uint32_t value)
{
    state->iomem.ops->write(state->iomem.opaque, offset, value,
                            sizeof(value));
}

static uint32_t mmio_read(DmMc02Ospi *state, hwaddr offset, unsigned size)
{
    return state->iomem.ops->read(state->iomem.opaque, offset, size);
}

static uint8_t flash_read_byte(DmMc02Ospi *state, hwaddr offset)
{
    return state->flash_window.ops->read(state->flash_window.opaque, offset,
                                         sizeof(uint8_t));
}

static void prepare_read(DmMc02Ospi *state, uint8_t command, unsigned size)
{
    mmio_write(state, OCTOSPI_DLR, size - 1);
    mmio_write(state, OCTOSPI_CCR, 0);
    mmio_write(state, OCTOSPI_IR, command);
}

static void prepare_page_program(DmMc02Ospi *state, hwaddr address,
                                  unsigned size)
{
    mmio_write(state, OCTOSPI_IR, W25Q_WRITE_ENABLE);
    mmio_write(state, OCTOSPI_DLR, size - 1);
    mmio_write(state, OCTOSPI_CCR, 0);
    mmio_write(state, OCTOSPI_IR, W25Q_PAGE_PROGRAM);
    mmio_write(state, OCTOSPI_AR, address);
}

static void enable_memory_mapped(DmMc02Ospi *state)
{
    mmio_write(state, OCTOSPI_CR, OCTOSPI_FMODE_MEMORY);
}

static void test_read_endpoint_uses_cpu_queue(DmMc02Ospi *state)
{
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(state);
    uint8_t data[3] = { 0 };
    uint8_t tail = 0;

    prepare_read(state, W25Q_JEDEC_ID, sizeof(data));
    CHECK(endpoint.read(endpoint.opaque, data, 2, UINT64_MAX));
    CHECK(data[0] == 0xef && data[1] == 0x40);
    CHECK(mmio_read(state, OCTOSPI_DR, sizeof(tail)) == 0x17);
    CHECK(!endpoint.read(endpoint.opaque, &tail, 1, 123));
    CHECK(tail == 0);
}

static void test_write_endpoint_programs_flash(DmMc02Ospi *state)
{
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(state);
    static const uint8_t data[] = { 0x12, 0x34, 0x56, 0x78 };

    prepare_page_program(state, 0x120, sizeof(data));
    CHECK(endpoint.write(endpoint.opaque, data, sizeof(data), 456));
    enable_memory_mapped(state);
    for (size_t i = 0; i < sizeof(data); ++i) {
        CHECK(flash_read_byte(state, 0x120 + i) == data[i]);
    }
}

static void test_write_rejects_oversized_beat(DmMc02Ospi *state)
{
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(state);
    static const uint8_t data[] = { 0xa5, 0x5a, 0x3c };
    unsigned address = 0x240;

    prepare_page_program(state, address, 2);
    CHECK(!endpoint.write(endpoint.opaque, data, sizeof(data), 789));
    enable_memory_mapped(state);
    CHECK(flash_read_byte(state, address) == 0xff);
    CHECK(flash_read_byte(state, address + 1) == 0xff);
    CHECK(endpoint.write(endpoint.opaque, data, 2, 790));
    CHECK(flash_read_byte(state, address) == data[0]);
    CHECK(flash_read_byte(state, address + 1) == data[1]);
}

static void test_invalid_direction_and_reset(DmMc02Ospi *state)
{
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(state);
    uint8_t data = 0x5a;

    dm_mc02_ospi_reset(state);
    CHECK(!endpoint.read(endpoint.opaque, &data, 1, 1));
    CHECK(!endpoint.write(endpoint.opaque, &data, 1, 2));
    CHECK(!endpoint.read(endpoint.opaque, &data, 0, 3));

    prepare_read(state, W25Q_JEDEC_ID, 1);
    CHECK(endpoint.read(endpoint.opaque, &data, 1, 4));
    dm_mc02_ospi_reset(state);
    CHECK(!endpoint.read(endpoint.opaque, &data, 1, 5));

    prepare_page_program(state, 0x360, 2);
    CHECK(endpoint.write(endpoint.opaque, &data, 1, 6));
    dm_mc02_ospi_reset(state);
    CHECK(!endpoint.write(endpoint.opaque, &data, 1, 7));
    enable_memory_mapped(state);
    CHECK(flash_read_byte(state, 0x360) == 0xff);
}

static void test_persistence_boundary(DmMc02Ospi *state)
{
    static const uint8_t data[] = { 0x12, 0x34, 0x56, 0x78 };
    DmMc02DmaEndpoint endpoint = dm_mc02_ospi_dma_endpoint(state);
    DmMc02Ospi restored;
    char path[128];

    (void)snprintf(path, sizeof(path),
                   "/tmp/dm-ospi-persistence-%ld.bin", (long)getpid());
    (void)remove(path);
    prepare_page_program(state, 0x120, sizeof(data));
    CHECK(endpoint.write(endpoint.opaque, data, sizeof(data), 10));
    CHECK(dm_mc02_ospi_save_persistence(state, path) ==
          DM_NOR_FLASH_PERSISTENCE_OK);

    dm_mc02_ospi_init(&restored, NULL);
    CHECK(dm_mc02_ospi_load_persistence(&restored, path) ==
          DM_NOR_FLASH_PERSISTENCE_OK);
    enable_memory_mapped(&restored);
    for (size_t i = 0; i < sizeof(data); ++i) {
        CHECK(flash_read_byte(&restored, 0x120 + i) == data[i]);
    }
    CHECK(dm_mc02_ospi_load_persistence(&restored, NULL) ==
          DM_NOR_FLASH_PERSISTENCE_DISABLED);
    dm_mc02_ospi_cleanup(&restored);
    (void)remove(path);
}

int main(void)
{
    DmMc02Ospi state;

    dm_mc02_ospi_init(&state, NULL);
    test_read_endpoint_uses_cpu_queue(&state);
    test_write_endpoint_programs_flash(&state);
    test_write_rejects_oversized_beat(&state);
    test_invalid_direction_and_reset(&state);
    test_persistence_boundary(&state);
    dm_mc02_ospi_cleanup(&state);
    puts("RESULT: OCTOSPI DMA endpoint smoke passed");
    return 0;
}
