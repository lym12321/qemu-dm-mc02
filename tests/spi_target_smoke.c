#include "qemu/osdep.h"

#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "hw/arm/dm_mc02_bmi088.h"
#include "hw/arm/dm_mc02_bmi088_spi.h"
#include "hw/arm/dm_mc02_spi.h"
#include "hw/arm/dm_mc02_spi_nor.h"
#include "qemu/timer.h"
#include "sysemu/dma.h"

#include <stdio.h>
#include <stdlib.h>

#define SPI_TXDR 0x20
#define SPI_RXDR 0x30

/* Keep this test independent of the full QEMU system link.  No DMA channel is
 * configured, so the SPI core exercises its direct MMIO target path. */
AddressSpace address_space_memory;

MemTxResult address_space_rw(AddressSpace *as, hwaddr address,
                             MemTxAttrs attrs, void *data, hwaddr size,
                             bool is_write)
{
    (void)as;
    (void)address;
    (void)attrs;
    (void)data;
    (void)size;
    (void)is_write;
    return MEMTX_ERROR;
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

void qemu_set_irq(qemu_irq irq, int level)
{
    (void)irq;
    (void)level;
}

int64_t qemu_clock_get_ns(QEMUClockType type)
{
    static int64_t now;

    (void)type;
    return ++now;
}

void timer_init_full(QEMUTimer *timer, QEMUTimerListGroup *timer_list_group,
                     QEMUClockType type, int scale, int attributes,
                     QEMUTimerCB *cb, void *opaque)
{
    (void)timer;
    (void)timer_list_group;
    (void)type;
    (void)scale;
    (void)attributes;
    (void)cb;
    (void)opaque;
}

void timer_mod(QEMUTimer *timer, int64_t expire_time)
{
    (void)timer;
    (void)expire_time;
}

void timer_del(QEMUTimer *timer)
{
    (void)timer;
}

typedef struct TargetProbe {
    unsigned transfer_count;
    unsigned select_count;
    uint8_t last_tx;
    uint64_t last_timestamp_ns;
    uint32_t last_mask;
    bool selected;
} TargetProbe;

static uint8_t probe_transfer(void *opaque, uint8_t tx,
                              uint64_t timestamp_ns)
{
    TargetProbe *probe = opaque;

    probe->transfer_count++;
    probe->last_tx = tx;
    probe->last_timestamp_ns = timestamp_ns;
    return (uint8_t)(tx ^ 0xa5u);
}

static void probe_select(void *opaque, bool selected, uint32_t selected_mask)
{
    TargetProbe *probe = opaque;

    probe->select_count++;
    probe->selected = selected;
    probe->last_mask = selected_mask;
}

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "SPI target smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static void spi_write(DmMc02Spi *spi, hwaddr offset, uint32_t value)
{
    spi->iomem.ops->write(spi->iomem.opaque, offset, value,
                          sizeof(uint32_t));
}

static uint32_t spi_read(DmMc02Spi *spi, hwaddr offset)
{
    return (uint32_t)spi->iomem.ops->read(spi->iomem.opaque, offset,
                                          sizeof(uint32_t));
}

static void test_core_dispatch(void)
{
    DmMc02Spi spi;
    TargetProbe probes[2] = { 0 };
    DmMc02SpiTarget targets[2] = {
        {
            .transfer = probe_transfer,
            .select = probe_select,
            .opaque = &probes[0],
        },
        {
            .transfer = probe_transfer,
            .select = probe_select,
            .opaque = &probes[1],
        },
    };

    dm_mc02_spi_state_init(&spi);
    dm_mc02_spi_init(&spi, NULL, "spi-target-smoke", false);
    dm_mc02_spi_set_target(&spi, 0, &targets[0]);
    dm_mc02_spi_set_target(&spi, 1, &targets[1]);

    dm_mc02_spi_select_mask(&spi, 1u);
    CHECK(probes[0].select_count == 1 && probes[1].select_count == 1);
    CHECK(probes[0].selected && !probes[1].selected);
    CHECK(probes[0].last_mask == 1u && probes[1].last_mask == 1u);

    spi_write(&spi, SPI_TXDR, 0x12u);
    CHECK(probes[0].transfer_count == 1);
    CHECK(probes[1].transfer_count == 0);
    CHECK(probes[0].last_tx == 0x12u);
    CHECK(spi_read(&spi, SPI_RXDR) == (0x12u ^ 0xa5u));

    dm_mc02_spi_select_mask(&spi, 0u);
    spi_write(&spi, SPI_TXDR, 0x34u);
    CHECK(probes[0].transfer_count == 1 && probes[1].transfer_count == 0);
    CHECK(spi_read(&spi, SPI_RXDR) == 0);

    dm_mc02_spi_select_mask(&spi, 3u);
    CHECK(probes[0].select_count == 3 && probes[1].select_count == 3);
    CHECK(probes[0].selected && probes[1].selected);
    spi_write(&spi, SPI_TXDR, 0x56u);
    CHECK(probes[0].transfer_count == 1 && probes[1].transfer_count == 0);
    CHECK(spi_read(&spi, SPI_RXDR) == 0);

    dm_mc02_spi_select_mask(&spi, 2u);
    spi_write(&spi, SPI_TXDR, 0x78u);
    CHECK(probes[1].transfer_count == 1);
    CHECK(probes[1].last_tx == 0x78u);
    CHECK(spi_read(&spi, SPI_RXDR) == (0x78u ^ 0xa5u));
    CHECK(probes[1].last_timestamp_ns >= probes[0].last_timestamp_ns);

    dm_mc02_spi_cleanup(&spi);
}

static void test_bmi088_boundary(void)
{
    DmMc02Spi spi;
    DmMc02Bmi088 gyro;
    DmMc02Bmi088Spi adapter;
    DmMc02SpiTarget target;

    dm_mc02_spi_state_init(&spi);
    dm_mc02_spi_init(&spi, NULL, "bmi088-spi-target-smoke", false);
    dm_mc02_bmi088_init(&gyro, false);
    dm_mc02_bmi088_spi_init(&adapter, &gyro);
    target = dm_mc02_bmi088_spi_target(&adapter);
    dm_mc02_spi_set_target(&spi, 0, &target);

    dm_mc02_spi_select_mask(&spi, 1u);
    spi_write(&spi, SPI_TXDR, 0x80u);
    CHECK(spi_read(&spi, SPI_RXDR) == 0);
    spi_write(&spi, SPI_TXDR, 0x00u);
    CHECK(spi_read(&spi, SPI_RXDR) == 0x0fu);

    /* CS deassertion must discard a partial command before the next one. */
    dm_mc02_spi_select_mask(&spi, 0u);
    dm_mc02_spi_select_mask(&spi, 1u);
    spi_write(&spi, SPI_TXDR, 0x80u);
    CHECK(spi_read(&spi, SPI_RXDR) == 0);
    dm_mc02_spi_select_mask(&spi, 0u);
    dm_mc02_spi_select_mask(&spi, 1u);
    spi_write(&spi, SPI_TXDR, 0x00u);
    CHECK(spi_read(&spi, SPI_RXDR) == 0);

    dm_mc02_spi_cleanup(&spi);
}

static uint8_t nor_transfer(DmMc02Spi *spi, uint8_t value)
{
    spi_write(spi, SPI_TXDR, value);
    return (uint8_t)spi_read(spi, SPI_RXDR);
}

static void nor_address(DmMc02Spi *spi, unsigned address)
{
    nor_transfer(spi, (uint8_t)(address >> 16));
    nor_transfer(spi, (uint8_t)(address >> 8));
    nor_transfer(spi, (uint8_t)address);
}

static void test_nor_core_boundary(void)
{
    enum {
        STORAGE_SIZE = 8192,
        PAGE_SIZE = 256,
        SECTOR_SIZE = 4096,
    };
    uint8_t storage[STORAGE_SIZE];
    const uint8_t jedec[3] = { 0xaa, 0xbb, 0xcc };
    DmNorFlash flash;
    DmSpiNorFlash adapter;
    DmMc02SpiTarget target;
    DmMc02Spi spi;

    memset(storage, 0xff, sizeof(storage));
    CHECK(dm_nor_flash_init(&flash, storage, sizeof(storage), PAGE_SIZE,
                            SECTOR_SIZE));
    CHECK(dm_spi_nor_flash_init(&adapter, &flash, 1u, jedec));
    target = dm_spi_nor_flash_target(&adapter);

    dm_mc02_spi_state_init(&spi);
    dm_mc02_spi_init(&spi, NULL, "spi-nor-core-boundary", false);
    dm_mc02_spi_set_target(&spi, 0, &target);

    dm_mc02_spi_select_mask(&spi, 1u);
    CHECK(nor_transfer(&spi, 0x9f) == 0);
    CHECK(nor_transfer(&spi, 0) == 0xaa);
    CHECK(nor_transfer(&spi, 0) == 0xbb);
    CHECK(nor_transfer(&spi, 0) == 0xcc);
    dm_mc02_spi_select_mask(&spi, 0);

    dm_mc02_spi_select_mask(&spi, 1u);
    CHECK(nor_transfer(&spi, 0x06) == 0);
    dm_mc02_spi_select_mask(&spi, 0);
    dm_mc02_spi_select_mask(&spi, 1u);
    CHECK(nor_transfer(&spi, 0x05) == 0);
    CHECK(nor_transfer(&spi, 0) == DM_NOR_FLASH_STATUS_WEL);
    dm_mc02_spi_select_mask(&spi, 0);

    dm_mc02_spi_select_mask(&spi, 1u);
    nor_transfer(&spi, 0x02);
    nor_address(&spi, PAGE_SIZE - 1);
    nor_transfer(&spi, 0x0f);
    nor_transfer(&spi, 0xf0);
    dm_mc02_spi_select_mask(&spi, 0);
    CHECK(dm_spi_nor_flash_last_result(&adapter) == DM_NOR_FLASH_OK);
    CHECK(storage[PAGE_SIZE - 1] == 0x0f);
    CHECK(storage[PAGE_SIZE] == 0xff);

    dm_mc02_spi_select_mask(&spi, 1u);
    nor_transfer(&spi, 0x03);
    nor_address(&spi, PAGE_SIZE - 1);
    CHECK(nor_transfer(&spi, 0) == 0x0f);
    CHECK(nor_transfer(&spi, 0) == 0xff);
    dm_mc02_spi_select_mask(&spi, 0);

    /* An invalid multi-select must not decode WREN or alter Flash status. */
    dm_mc02_spi_select_mask(&spi, 3u);
    CHECK(nor_transfer(&spi, 0x06) == 0);
    dm_mc02_spi_select_mask(&spi, 0);
    dm_mc02_spi_select_mask(&spi, 1u);
    CHECK(nor_transfer(&spi, 0x05) == 0);
    CHECK(nor_transfer(&spi, 0) == 0);
    dm_mc02_spi_select_mask(&spi, 0);

    /* Switching CS while an address is incomplete must discard the framing. */
    dm_mc02_spi_select_mask(&spi, 1u);
    nor_transfer(&spi, 0x03);
    nor_transfer(&spi, 0);
    dm_mc02_spi_select_mask(&spi, 0);
    dm_mc02_spi_select_mask(&spi, 1u);
    CHECK(nor_transfer(&spi, 0) == 0);
    dm_mc02_spi_select_mask(&spi, 0);

    dm_mc02_spi_cleanup(&spi);
}

int main(void)
{
    test_core_dispatch();
    test_bmi088_boundary();
    test_nor_core_boundary();
    puts("RESULT: SPI target dispatch smoke passed");
    return 0;
}
