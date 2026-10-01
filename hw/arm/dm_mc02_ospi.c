/* Minimal STM32H723 OCTOSPI register model with a W25Q64JV data path.
 *
 * This deliberately models the register-triggered transactions used by the
 * board firmware.  It is not a complete OCTOSPI protocol engine: line modes,
 * DTR timing, alternate bytes, DMA and interrupts are retained as registers,
 * but are not interpreted by the flash model.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/arm/dm_mc02_ospi.h"

#ifdef DM_MC02_OSPI_TEST_FIXTURE

#define OCTOSPI_CR             0x000
#define OCTOSPI_SR             0x020
#define OCTOSPI_FCR            0x024
#define OCTOSPI_DLR            0x040
#define OCTOSPI_AR             0x048
#define OCTOSPI_DR             0x050
#define OCTOSPI_PSMKR          0x080
#define OCTOSPI_PSMAR          0x088
#define OCTOSPI_PIR            0x090
#define OCTOSPI_CCR            0x100
#define OCTOSPI_TCR            0x108
#define OCTOSPI_IR             0x110

#define OCTOSPI_CR_FMODE_MASK  (3u << 28)
#define OCTOSPI_FMODE_AUTO     (2u << 28)
#define OCTOSPI_FMODE_MEMORY   (3u << 28)
#define OCTOSPI_SR_TCF (1u << 1)
#define OCTOSPI_SR_FTF (1u << 2)
#define OCTOSPI_SR_SMF (1u << 3)
#define OCTOSPI_SR_TEF (1u << 0)
#define OCTOSPI_SR_BUSY (1u << 5)

#define W25Q_CMD_WRITE_ENABLE       0x06
#define W25Q_CMD_READ_STATUS1       0x05
#define W25Q_CMD_JEDEC_ID           0x9f
#define W25Q_CMD_PAGE_PROGRAM       0x02
#define W25Q_CMD_QUAD_PAGE_PROGRAM  0x32
#define W25Q_CMD_SECTOR_ERASE       0x20
#define W25Q_CMD_BLOCK_ERASE_32K    0x52
#define W25Q_CMD_BLOCK_ERASE_64K    0xd8
#define W25Q_CMD_CHIP_ERASE         0xc7
#define W25Q_CMD_READ               0x03
#define W25Q_CMD_FAST_READ          0x0b
#define W25Q_CMD_QUAD_READ         0x6b
#define W25Q_CMD_QUAD_IO_READ      0xeb

#define W25Q_STATUS_WIP             (1u << 0)
#define W25Q_STATUS_WEL             (1u << 1)

static uint32_t dm_mc02_ospi_load(const uint32_t *regs, hwaddr offset,
                                  unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= ((const uint8_t *)regs)[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_ospi_store(uint32_t *regs, hwaddr offset, uint32_t value,
                               unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        ((uint8_t *)regs)[offset + i] = value >> (i * 8);
    }
}

static uint64_t dm_mc02_ospi_flash_read(void *opaque, hwaddr offset,
                                        unsigned size)
{
    const DmMc02Ospi *s = opaque;
    uint64_t value = 0;

    if (!s->memory_mapped || !s->has_flash ||
        offset >= s->flash_size || size > s->flash_size - offset) {
        return UINT64_MAX;
    }
    for (unsigned i = 0; i < size; ++i) {
        value |= (uint64_t)dm_nor_flash_read_byte(&s->flash, offset + i)
                 << (i * 8);
    }
    return value;
}

static void dm_mc02_ospi_flash_write(void *opaque, hwaddr offset,
                                     uint64_t value, unsigned size)
{
    /* The memory-mapped window is read-only; programming goes through DR. */
    (void)opaque;
    (void)offset;
    (void)value;
    (void)size;
}

static const MemoryRegionOps dm_mc02_ospi_flash_ops = {
    .read = dm_mc02_ospi_flash_read,
    .write = dm_mc02_ospi_flash_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static uint32_t dm_mc02_ospi_status(const DmMc02Ospi *s)
{
    uint32_t status = dm_mc02_ospi_load(s->regs, OCTOSPI_SR,
                                        sizeof(uint32_t));

    status &= ~(OCTOSPI_SR_BUSY | (0x3f << 8));
    if (dm_nor_flash_status(&s->flash) & DM_NOR_FLASH_STATUS_WIP) {
        status |= OCTOSPI_SR_BUSY;
    }
    if (s->rx_pos < s->rx_size ||
        (s->command_valid && s->tx_size < s->tx_expected)) {
        status |= OCTOSPI_SR_FTF;
    } else {
        status &= ~OCTOSPI_SR_FTF;
    }
    status |= ((s->rx_size - s->rx_pos) & 0x3f) << 8;
    return status;
}

static uint8_t dm_mc02_ospi_flash_status(const DmMc02Ospi *s)
{
    return dm_nor_flash_status(&s->flash);
}

static bool dm_mc02_ospi_is_address_command(uint8_t command)
{
    return command == W25Q_CMD_PAGE_PROGRAM ||
           command == W25Q_CMD_QUAD_PAGE_PROGRAM ||
           command == W25Q_CMD_SECTOR_ERASE ||
           command == W25Q_CMD_BLOCK_ERASE_32K ||
           command == W25Q_CMD_BLOCK_ERASE_64K ||
           command == W25Q_CMD_READ || command == W25Q_CMD_FAST_READ ||
           command == W25Q_CMD_QUAD_READ || command == W25Q_CMD_QUAD_IO_READ;
}

static bool dm_mc02_ospi_command_uses_dlr(uint8_t command)
{
    switch (command) {
    case W25Q_CMD_READ_STATUS1:
    case W25Q_CMD_JEDEC_ID:
    case W25Q_CMD_READ:
    case W25Q_CMD_FAST_READ:
    case W25Q_CMD_QUAD_READ:
    case W25Q_CMD_QUAD_IO_READ:
    case W25Q_CMD_PAGE_PROGRAM:
    case W25Q_CMD_QUAD_PAGE_PROGRAM:
        return true;
    default:
        return false;
    }
}

static DmNorFlashResult dm_mc02_ospi_erase_command(DmMc02Ospi *s)
{
    switch (s->command) {
    case W25Q_CMD_SECTOR_ERASE:
        return dm_nor_flash_sector_erase(&s->flash, s->command_address);
    case W25Q_CMD_BLOCK_ERASE_32K:
        return dm_nor_flash_erase(&s->flash, s->command_address, 32 * 1024);
    case W25Q_CMD_BLOCK_ERASE_64K:
        return dm_nor_flash_erase(&s->flash, s->command_address, 64 * 1024);
    case W25Q_CMD_CHIP_ERASE:
        return dm_nor_flash_chip_erase(&s->flash);
    default:
        return DM_NOR_FLASH_INVALID;
    }
}

static bool dm_mc02_ospi_resize_rx(DmMc02Ospi *s, uint32_t size)
{
    /* A bounded allocation prevents a malformed DLR write from turning a
     * guest register access into an unbounded host allocation. */
    if (size > (1u << 20)) {
        return false;
    }
    s->rx_data = g_realloc(s->rx_data, MAX(size, 1u));
    s->rx_size = size;
    s->rx_pos = 0;
    s->rx_dma_reserved_size = 0;
    s->rx_dma_reserved = false;
    return true;
}

static bool dm_mc02_ospi_transfer_length(const DmMc02Ospi *s,
                                         uint32_t *length)
{
    uint32_t dlr = dm_mc02_ospi_load(s->regs, OCTOSPI_DLR,
                                     sizeof(uint32_t));

    /* DLR is length - 1.  UINT32_MAX cannot represent a 32-bit length. */
    if (dlr == UINT32_MAX) {
        return false;
    }
    *length = dlr + 1u;
    return true;
}

static bool dm_mc02_ospi_page_program_length(const DmMc02Ospi *s,
                                             uint32_t *length)
{
    if (!dm_mc02_ospi_transfer_length(s, length) ||
        !s->has_flash || *length > s->page_size) {
        return false;
    }
    return true;
}

static void dm_mc02_ospi_finish(DmMc02Ospi *s, bool error)
{
    uint32_t status = dm_mc02_ospi_load(s->regs, OCTOSPI_SR,
                                        sizeof(uint32_t));

    status |= OCTOSPI_SR_TCF;
    if (error) {
        status |= OCTOSPI_SR_TEF;
    }
    dm_mc02_ospi_store(s->regs, OCTOSPI_SR, status, sizeof(uint32_t));
}

static void dm_mc02_ospi_prepare_read(DmMc02Ospi *s)
{
    uint32_t length;
    uint32_t address = s->command_address;
    uint8_t status;

    if (!dm_mc02_ospi_transfer_length(s, &length) || !s->has_flash ||
        !dm_mc02_ospi_resize_rx(s, length)) {
        dm_mc02_ospi_finish(s, true);
        return;
    }
    switch (s->command) {
    case W25Q_CMD_JEDEC_ID:
        if (length > 0) {
            s->rx_data[0] = s->jedec_id[0];
        }
        if (length > 1) {
            s->rx_data[1] = s->jedec_id[1];
        }
        if (length > 2) {
            s->rx_data[2] = s->jedec_id[2];
        }
        for (uint32_t i = 3; i < length; ++i) {
            s->rx_data[i] = 0xff;
        }
        break;
    case W25Q_CMD_READ_STATUS1:
        status = dm_mc02_ospi_flash_status(s);
        if (length > 0) {
            s->rx_data[0] = status;
        }
        for (uint32_t i = 1; i < length; ++i) {
            s->rx_data[i] = 0xff;
        }
        break;
    case W25Q_CMD_READ:
    case W25Q_CMD_FAST_READ:
    case W25Q_CMD_QUAD_READ:
    case W25Q_CMD_QUAD_IO_READ:
        for (uint32_t i = 0; i < length; ++i) {
            s->rx_data[i] = dm_nor_flash_read_byte(&s->flash, address + i);
        }
        break;
    default:
        memset(s->rx_data, 0xff, length);
        dm_mc02_ospi_finish(s, true);
        return;
    }
    s->command_started = true;
    dm_mc02_ospi_finish(s, false);
}

static bool dm_mc02_ospi_commit_program(DmMc02Ospi *s)
{
    uint32_t length;
    bool success;

    /* DLR is the requested transaction length minus one.  execute() reports
     * an oversized page program as TEF, but the data path can still reach
     * this function after its bounded staging buffer fills.  Recheck the
     * original length at the commit boundary so an invalid transaction can
     * never partially program the NOR array. */
    if (!dm_mc02_ospi_page_program_length(s, &length) || !s->has_flash ||
        dm_nor_flash_page_program(&s->flash, s->command_address,
                                  s->tx_data, length) != DM_NOR_FLASH_OK) {
        dm_mc02_ospi_finish(s, true);
        s->command_started = true;
        return false;
    }
    success = true;
    dm_mc02_ospi_finish(s, false);
    s->command_started = true;
    return success;
}

static void dm_mc02_ospi_execute(DmMc02Ospi *s)
{
    uint32_t length = 0;
    uint32_t status;

    if (!s->has_flash) {
        dm_mc02_ospi_finish(s, true);
        return;
    }
    if (dm_mc02_ospi_command_uses_dlr(s->command) &&
        !dm_mc02_ospi_transfer_length(s, &length)) {
        dm_mc02_ospi_finish(s, true);
        return;
    }
    s->command_started = false;
    s->rx_size = 0;
    s->rx_pos = 0;
    s->rx_dma_reserved_size = 0;
    s->rx_dma_reserved = false;
    s->tx_size = 0;
    s->tx_expected = MIN(length, (uint32_t)DM_MC02_OSPI_MAX_PAGE_SIZE);

    switch (s->command) {
    case W25Q_CMD_WRITE_ENABLE:
        dm_nor_flash_write_enable(&s->flash);
        dm_mc02_ospi_finish(s, false);
        break;
    case W25Q_CMD_READ_STATUS1:
    case W25Q_CMD_JEDEC_ID:
    case W25Q_CMD_READ:
    case W25Q_CMD_FAST_READ:
    case W25Q_CMD_QUAD_READ:
    case W25Q_CMD_QUAD_IO_READ:
        dm_mc02_ospi_prepare_read(s);
        if ((dm_mc02_ospi_load(s->regs, OCTOSPI_CR, sizeof(uint32_t)) &
             OCTOSPI_CR_FMODE_MASK) == OCTOSPI_FMODE_AUTO &&
            s->command == W25Q_CMD_READ_STATUS1) {
            status = dm_mc02_ospi_flash_status(s);
            if ((status & dm_mc02_ospi_load(s->regs, OCTOSPI_PSMKR,
                                             sizeof(uint32_t))) ==
                (dm_mc02_ospi_load(s->regs, OCTOSPI_PSMAR,
                                   sizeof(uint32_t)) &
                 dm_mc02_ospi_load(s->regs, OCTOSPI_PSMKR,
                                   sizeof(uint32_t)))) {
                dm_mc02_ospi_store(s->regs, OCTOSPI_SR,
                                   dm_mc02_ospi_status(s) | OCTOSPI_SR_SMF,
                                   sizeof(uint32_t));
            }
        }
        break;
    case W25Q_CMD_SECTOR_ERASE:
    case W25Q_CMD_BLOCK_ERASE_32K:
    case W25Q_CMD_BLOCK_ERASE_64K:
    case W25Q_CMD_CHIP_ERASE:
        if (dm_mc02_ospi_erase_command(s) != DM_NOR_FLASH_OK) {
            dm_mc02_ospi_finish(s, true);
        } else {
            dm_mc02_ospi_finish(s, false);
        }
        break;
    case W25Q_CMD_PAGE_PROGRAM:
    case W25Q_CMD_QUAD_PAGE_PROGRAM:
        if (!dm_mc02_ospi_page_program_length(s, &length)) {
            /* Keep malformed transactions from reaching commit_program(). */
            s->tx_expected = UINT32_MAX;
            dm_mc02_ospi_finish(s, true);
        } else {
            s->tx_expected = length;
            dm_mc02_ospi_finish(s, false);
        }
        break;
    default:
        dm_mc02_ospi_finish(s, true);
        break;
    }
}

static void dm_mc02_ospi_command_write(DmMc02Ospi *s, hwaddr offset,
                                        uint32_t value)
{
    if (offset == OCTOSPI_CCR) {
        s->command_started = false;
    } else if (offset == OCTOSPI_IR) {
        s->command = value;
        s->command_valid = true;
        if (!dm_mc02_ospi_is_address_command(s->command)) {
            dm_mc02_ospi_execute(s);
        }
    } else if (offset == OCTOSPI_AR && s->command_valid) {
        s->command_address = value;
        if (dm_mc02_ospi_is_address_command(s->command)) {
            dm_mc02_ospi_execute(s);
        }
    }
}

static bool dm_mc02_ospi_data_write(DmMc02Ospi *s, uint64_t value,
                                     unsigned size)
{
    if (!s->command_valid || s->command_started ||
        (s->command != W25Q_CMD_PAGE_PROGRAM &&
         s->command != W25Q_CMD_QUAD_PAGE_PROGRAM) ||
        s->tx_expected > sizeof(s->tx_data) ||
        s->tx_size > s->tx_expected ||
        size > s->tx_expected - s->tx_size) {
        dm_mc02_ospi_finish(s, true);
        return false;
    }
    for (unsigned i = 0; i < size; ++i) {
        s->tx_data[s->tx_size++] = value >> (i * 8);
    }
    if (s->tx_size >= s->tx_expected) {
        return dm_mc02_ospi_commit_program(s);
    }
    return true;
}

static DmMc02DmaEndpointResult dm_mc02_ospi_dma_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    DmMc02Ospi *s = opaque;

    (void)timestamp_ns;
    if (!s || !data || !size || !s->has_flash || !s->command_valid ||
        !s->command_started || s->rx_dma_reserved || s->rx_pos > s->rx_size ||
        size > s->rx_size - s->rx_pos) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    memcpy(data, s->rx_data + s->rx_pos, size);
    s->rx_dma_reserved_size = size;
    s->rx_dma_reserved = true;
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void dm_mc02_ospi_dma_read_commit(void *opaque)
{
    DmMc02Ospi *s = opaque;

    s->rx_pos += s->rx_dma_reserved_size;
    s->rx_dma_reserved_size = 0;
    s->rx_dma_reserved = false;
}

static void dm_mc02_ospi_dma_read_abort(void *opaque)
{
    DmMc02Ospi *s = opaque;

    s->rx_dma_reserved_size = 0;
    s->rx_dma_reserved = false;
}

static bool dm_mc02_ospi_dma_read(void *opaque, uint8_t *data, unsigned size,
                                  uint64_t timestamp_ns)
{
    DmMc02DmaEndpointResult result = dm_mc02_ospi_dma_read_prepare(
        opaque, data, size, timestamp_ns);

    if (result != DM_MC02_DMA_ENDPOINT_ACCEPTED) {
        return false;
    }
    dm_mc02_ospi_dma_read_commit(opaque);
    return true;
}

static bool dm_mc02_ospi_dma_write(void *opaque, const uint8_t *data,
                                   unsigned size, uint64_t timestamp_ns)
{
    DmMc02Ospi *s = opaque;
    uint64_t value = 0;

    (void)timestamp_ns;
    if (!s || !data || !size || size > sizeof(value)) {
        return false;
    }
    for (unsigned i = 0; i < size; ++i) {
        value |= (uint64_t)data[i] << (i * 8);
    }
    return dm_mc02_ospi_data_write(s, value, size);
}

static uint64_t dm_mc02_ospi_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Ospi *s = opaque;
    uint32_t value;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_OSPI_REGION_SIZE ||
        size > DM_MC02_OSPI_REGION_SIZE - offset) {
        return 0;
    }
    if (offset == OCTOSPI_DR) {
        value = UINT32_MAX;
        for (unsigned i = 0; i < size; ++i) {
            if (s->rx_pos < s->rx_size) {
                value &= ~(0xffu << (i * 8));
                value |= (uint32_t)s->rx_data[s->rx_pos++] << (i * 8);
            }
        }
    } else if (offset == OCTOSPI_SR && size == sizeof(uint32_t)) {
        value = dm_mc02_ospi_status(s);
    } else {
        value = dm_mc02_ospi_load(s->regs, offset & ~3u, sizeof(uint32_t));
    }
    return (value >> ((offset % sizeof(uint32_t)) * 8)) &
           (size == sizeof(uint32_t) ? UINT32_MAX :
            ((1u << (size * 8)) - 1u));
}

static void dm_mc02_ospi_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02Ospi *s = opaque;
    uint32_t mask;
    uint32_t old;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_OSPI_REGION_SIZE ||
        size > DM_MC02_OSPI_REGION_SIZE - offset) {
        return;
    }
    mask = size == sizeof(uint32_t) ? UINT32_MAX : ((1u << (size * 8)) - 1u);
    old = dm_mc02_ospi_load(s->regs, offset & ~3u, sizeof(uint32_t));
    old &= ~(mask << ((offset % sizeof(uint32_t)) * 8));
    old |= ((uint32_t)value & mask) << ((offset % sizeof(uint32_t)) * 8);

    if (offset == OCTOSPI_DR) {
        dm_mc02_ospi_data_write(s, value, size);
        return;
    }
    if (offset == OCTOSPI_FCR) {
        uint32_t status = dm_mc02_ospi_load(s->regs, OCTOSPI_SR,
                                             sizeof(uint32_t));
        status &= ~((uint32_t)value & (OCTOSPI_SR_TEF | OCTOSPI_SR_TCF |
                                       OCTOSPI_SR_SMF | (1u << 4)));
        dm_mc02_ospi_store(s->regs, OCTOSPI_SR, status, sizeof(uint32_t));
        return;
    }
    dm_mc02_ospi_store(s->regs, offset & ~3u, old, sizeof(uint32_t));
    if (offset == OCTOSPI_CR && size == sizeof(uint32_t)) {
        s->memory_mapped = (old & OCTOSPI_CR_FMODE_MASK) ==
                           OCTOSPI_FMODE_MEMORY;
    }
    if (size == sizeof(uint32_t) && (offset == OCTOSPI_CCR ||
                                     offset == OCTOSPI_IR ||
                                     offset == OCTOSPI_AR)) {
        dm_mc02_ospi_command_write(s, offset, old);
    }
}

static const MemoryRegionOps dm_mc02_ospi_ops = {
    .read = dm_mc02_ospi_read,
    .write = dm_mc02_ospi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static uint64_t dm_mc02_ospim_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Ospim *s = opaque;
    uint32_t value;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_OSPIM_REGION_SIZE ||
        size > DM_MC02_OSPIM_REGION_SIZE - offset) {
        return 0;
    }
    value = s->regs[offset / sizeof(uint32_t)];
    return (value >> ((offset % sizeof(uint32_t)) * 8)) &
           (size == sizeof(uint32_t) ? UINT32_MAX : ((1u << (size * 8)) - 1u));
}

static void dm_mc02_ospim_write(void *opaque, hwaddr offset, uint64_t value,
                                unsigned size)
{
    DmMc02Ospim *s = opaque;
    uint32_t mask;
    uint32_t old;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_OSPIM_REGION_SIZE ||
        size > DM_MC02_OSPIM_REGION_SIZE - offset) {
        return;
    }
    mask = size == sizeof(uint32_t) ? UINT32_MAX : ((1u << (size * 8)) - 1u);
    old = s->regs[offset / sizeof(uint32_t)];
    old &= ~(mask << ((offset % sizeof(uint32_t)) * 8));
    old |= ((uint32_t)value & mask) << ((offset % sizeof(uint32_t)) * 8);
    s->regs[offset / sizeof(uint32_t)] = old;
}

static const MemoryRegionOps dm_mc02_ospim_ops = {
    .read = dm_mc02_ospim_read,
    .write = dm_mc02_ospim_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_ospi_init(DmMc02Ospi *state, Object *owner)
{
    static const DmMc02OspiFlashConfig default_config = {
        .name = "W25Q64JV",
        .storage_size = DM_MC02_OSPI_FLASH_SIZE,
        .page_size = DM_MC02_OSPI_PAGE_SIZE,
        .sector_size = 0x1000,
        .jedec_id = { 0xef, 0x40, 0x17 },
    };

    dm_mc02_ospi_init_with_config(state, owner, &default_config);
}

void dm_mc02_ospi_init_with_flash(DmMc02Ospi *state, Object *owner,
                                  bool with_flash)
{
    dm_mc02_ospi_init_with_config(state, owner, with_flash ?
                                   &(const DmMc02OspiFlashConfig) {
                                       .name = "W25Q64JV",
                                       .storage_size = DM_MC02_OSPI_FLASH_SIZE,
                                       .page_size = DM_MC02_OSPI_PAGE_SIZE,
                                       .sector_size = 0x1000,
                                       .jedec_id = { 0xef, 0x40, 0x17 },
                                   } : NULL);
}

void dm_mc02_ospi_init_with_config(DmMc02Ospi *state, Object *owner,
                                   const DmMc02OspiFlashConfig *config)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_ospi_ops, state,
                          "dm-mc02.octospi2", DM_MC02_OSPI_REGION_SIZE);
    if (config) {
        state->flash_size = config->storage_size;
        state->page_size = config->page_size;
        state->sector_size = config->sector_size;
        memcpy(state->jedec_id, config->jedec_id, sizeof(state->jedec_id));
        state->has_flash = state->flash_size && state->page_size &&
                           state->sector_size &&
                           state->page_size <= DM_MC02_OSPI_MAX_PAGE_SIZE &&
                           state->sector_size >= state->page_size &&
                           !(state->flash_size % state->page_size) &&
                           !(state->flash_size % state->sector_size);
    }
    if (state->has_flash) {
        memory_region_init_io(&state->flash_window, owner,
                              &dm_mc02_ospi_flash_ops, state,
                              config->name ? config->name :
                              "dm-mc02.ospi-flash-memory",
                              state->flash_size);
        state->flash.storage = g_malloc(state->flash_size);
        memset(state->flash.storage, 0xff, state->flash_size);
        if (!dm_nor_flash_init(&state->flash, state->flash.storage,
                               state->flash_size, state->page_size,
                               state->sector_size)) {
            g_free(state->flash.storage);
            state->flash.storage = NULL;
            state->has_flash = false;
        }
    }
    dm_mc02_ospi_reset(state);
}

void dm_mc02_ospi_reset(DmMc02Ospi *state)
{
    memset(state->regs, 0, sizeof(state->regs));
    state->rx_size = 0;
    state->rx_pos = 0;
    state->rx_dma_reserved_size = 0;
    state->rx_dma_reserved = false;
    state->tx_size = 0;
    state->tx_expected = 0;
    state->command_address = 0;
    state->command = 0;
    state->command_valid = false;
    state->command_started = false;
    dm_nor_flash_reset(&state->flash);
    state->memory_mapped = false;
}

void dm_mc02_ospi_cleanup(DmMc02Ospi *state)
{
    g_free(state->rx_data);
    state->rx_data = NULL;
    g_free(state->flash.storage);
    state->flash.storage = NULL;
}

DmMc02DmaEndpoint dm_mc02_ospi_dma_endpoint(DmMc02Ospi *state)
{
    return (DmMc02DmaEndpoint) {
        .read = dm_mc02_ospi_dma_read,
        .write = dm_mc02_ospi_dma_write,
        .read_prepare = dm_mc02_ospi_dma_read_prepare,
        .read_commit = dm_mc02_ospi_dma_read_commit,
        .read_abort = dm_mc02_ospi_dma_read_abort,
        .opaque = state,
    };
}

DmNorFlashPersistenceResult dm_mc02_ospi_load_persistence(
    DmMc02Ospi *state, const char *path)
{
    if (!state) {
        return DM_NOR_FLASH_PERSISTENCE_INVALID;
    }
    return dm_nor_flash_persistence_load(path, state->flash.storage,
                                         state->flash_size);
}

DmNorFlashPersistenceResult dm_mc02_ospi_save_persistence(
    const DmMc02Ospi *state, const char *path)
{
    if (!state) {
        return DM_NOR_FLASH_PERSISTENCE_INVALID;
    }
    return dm_nor_flash_persistence_save(path, state->flash.storage,
                                         state->flash_size);
}

void dm_mc02_ospim_init(DmMc02Ospim *state, Object *owner)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_ospim_ops, state,
                          "dm-mc02.ospim", DM_MC02_OSPIM_REGION_SIZE);
}

#else /* DM_MC02_OSPI_TEST_FIXTURE */

/*
 * Production QEMU OCTOSPI wrapper.
 *
 * The register-facing controller remains local to the H723 model, but the
 * Flash die is the project-owned dm-w25q64 device.  This file only translates
 * the narrow indirect OCTOSPI transactions into SSI byte transfers; it does
 * not reproduce NOR command or storage semantics.
 */
#include "hw/arm/dm_mc02_ssi_nor.h"

#define OCTOSPI_CR             0x000
#define OCTOSPI_SR             0x020
#define OCTOSPI_FCR            0x024
#define OCTOSPI_DLR            0x040
#define OCTOSPI_AR             0x048
#define OCTOSPI_DR             0x050
#define OCTOSPI_PSMKR          0x080
#define OCTOSPI_PSMAR          0x088
#define OCTOSPI_CCR            0x100
#define OCTOSPI_IR             0x110

#define OCTOSPI_CR_FMODE_MASK  (3u << 28)
#define OCTOSPI_FMODE_AUTO     (2u << 28)
#define OCTOSPI_FMODE_MEMORY   (3u << 28)
#define OCTOSPI_SR_TCF         (1u << 1)
#define OCTOSPI_SR_FTF         (1u << 2)
#define OCTOSPI_SR_SMF         (1u << 3)
#define OCTOSPI_SR_TEF         (1u << 0)
#define OCTOSPI_SR_BUSY        (1u << 5)

#define W25Q_CMD_WRITE_ENABLE       0x06
#define W25Q_CMD_READ_STATUS1       0x05
#define W25Q_CMD_JEDEC_ID           0x9f
#define W25Q_CMD_PAGE_PROGRAM       0x02
#define W25Q_CMD_QUAD_PAGE_PROGRAM  0x32
#define W25Q_CMD_SECTOR_ERASE       0x20
#define W25Q_CMD_BLOCK_ERASE_32K    0x52
#define W25Q_CMD_BLOCK_ERASE_64K    0xd8
#define W25Q_CMD_CHIP_ERASE         0xc7
#define W25Q_CMD_READ               0x03
#define W25Q_CMD_FAST_READ          0x0b
#define W25Q_CMD_QUAD_READ          0x6b
#define W25Q_CMD_QUAD_IO_READ      0xeb

#define W25Q_STATUS_WIP             (1u << 0)

static uint32_t dm_mc02_ospi_load(const uint32_t *regs, hwaddr offset,
                                  unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= ((const uint8_t *)regs)[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_ospi_store(uint32_t *regs, hwaddr offset, uint32_t value,
                               unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        ((uint8_t *)regs)[offset + i] = value >> (i * 8);
    }
}

static bool dm_mc02_ospi_valid_config(const DmMc02OspiFlashConfig *config)
{
    static const uint8_t expected_id[3] = { 0xef, 0x40, 0x17 };

    return config && config->storage_size == DM_MC02_OSPI_FLASH_SIZE &&
           config->page_size == DM_MC02_OSPI_PAGE_SIZE &&
           config->sector_size == 0x1000 &&
           !memcmp(config->jedec_id, expected_id, sizeof(expected_id));
}

static uint64_t dm_mc02_ospi_flash_read(void *opaque, hwaddr offset,
                                        unsigned size)
{
    const DmMc02Ospi *s = opaque;
    uint64_t value = 0;

    if (!s->memory_mapped || !s->has_flash ||
        offset >= s->flash_size || size > s->flash_size - offset) {
        return UINT64_MAX;
    }
    for (unsigned i = 0; i < size; ++i) {
        value |= (uint64_t)s->ssi_nor.storage[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_ospi_flash_write(void *opaque, hwaddr offset,
                                     uint64_t value, unsigned size)
{
    /* Memory-mapped OCTOSPI is read-only for this board profile. */
    (void)opaque;
    (void)offset;
    (void)value;
    (void)size;
}

static const MemoryRegionOps dm_mc02_ospi_flash_ops = {
    .read = dm_mc02_ospi_flash_read,
    .write = dm_mc02_ospi_flash_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void dm_mc02_ospi_select(DmMc02Ospi *s, bool selected)
{
    dm_mc02_ssi_nor_select(&s->ssi_nor, selected);
}

static uint8_t dm_mc02_ospi_transfer(DmMc02Ospi *s, uint8_t value)
{
    return dm_mc02_ssi_nor_transfer(&s->ssi_nor, value);
}

static void dm_mc02_ospi_begin(DmMc02Ospi *s, uint8_t command)
{
    dm_mc02_ospi_select(s, true);
    dm_mc02_ospi_transfer(s, command);
}

static void dm_mc02_ospi_end(DmMc02Ospi *s)
{
    dm_mc02_ospi_select(s, false);
}

static bool dm_mc02_ospi_transfer_length(const DmMc02Ospi *s,
                                         uint32_t *length)
{
    uint32_t dlr = dm_mc02_ospi_load(s->regs, OCTOSPI_DLR,
                                     sizeof(uint32_t));

    if (dlr == UINT32_MAX) {
        return false;
    }
    *length = dlr + 1u;
    return true;
}

static bool dm_mc02_ospi_is_address_command(uint8_t command)
{
    return command == W25Q_CMD_PAGE_PROGRAM ||
           command == W25Q_CMD_QUAD_PAGE_PROGRAM ||
           command == W25Q_CMD_SECTOR_ERASE ||
           command == W25Q_CMD_BLOCK_ERASE_32K ||
           command == W25Q_CMD_BLOCK_ERASE_64K ||
           command == W25Q_CMD_READ || command == W25Q_CMD_FAST_READ ||
           command == W25Q_CMD_QUAD_READ || command == W25Q_CMD_QUAD_IO_READ;
}

static bool dm_mc02_ospi_command_uses_dlr(uint8_t command)
{
    switch (command) {
    case W25Q_CMD_READ_STATUS1:
    case W25Q_CMD_JEDEC_ID:
    case W25Q_CMD_READ:
    case W25Q_CMD_FAST_READ:
    case W25Q_CMD_QUAD_READ:
    case W25Q_CMD_QUAD_IO_READ:
    case W25Q_CMD_PAGE_PROGRAM:
    case W25Q_CMD_QUAD_PAGE_PROGRAM:
        return true;
    default:
        return false;
    }
}

static void dm_mc02_ospi_send_address(DmMc02Ospi *s)
{
    dm_mc02_ospi_transfer(s, s->command_address >> 16);
    dm_mc02_ospi_transfer(s, s->command_address >> 8);
    dm_mc02_ospi_transfer(s, s->command_address);
}

static uint8_t dm_mc02_ospi_flash_status(DmMc02Ospi *s)
{
    uint8_t status;

    dm_mc02_ospi_begin(s, W25Q_CMD_READ_STATUS1);
    status = dm_mc02_ospi_transfer(s, 0);
    dm_mc02_ospi_end(s);
    return status;
}

static bool dm_mc02_ospi_resize_rx(DmMc02Ospi *s, uint32_t size)
{
    if (size > (1u << 20)) {
        return false;
    }
    s->rx_data = g_realloc(s->rx_data, MAX(size, 1u));
    s->rx_size = size;
    s->rx_pos = 0;
    s->rx_dma_reserved_size = 0;
    s->rx_dma_reserved = false;
    return true;
}

static void dm_mc02_ospi_finish(DmMc02Ospi *s, bool error)
{
    uint32_t status = dm_mc02_ospi_load(s->regs, OCTOSPI_SR,
                                        sizeof(uint32_t));

    status |= OCTOSPI_SR_TCF;
    if (error) {
        status |= OCTOSPI_SR_TEF;
    }
    dm_mc02_ospi_store(s->regs, OCTOSPI_SR, status, sizeof(uint32_t));
}

static void dm_mc02_ospi_prepare_read(DmMc02Ospi *s)
{
    uint32_t length;

    if (!dm_mc02_ospi_transfer_length(s, &length) ||
        !dm_mc02_ospi_resize_rx(s, length)) {
        dm_mc02_ospi_finish(s, true);
        return;
    }

    dm_mc02_ospi_begin(s, s->command);
    switch (s->command) {
    case W25Q_CMD_JEDEC_ID:
    case W25Q_CMD_READ_STATUS1:
        break;
    case W25Q_CMD_READ:
    case W25Q_CMD_FAST_READ:
    case W25Q_CMD_QUAD_READ:
    case W25Q_CMD_QUAD_IO_READ:
        dm_mc02_ospi_send_address(s);
        if (s->command == W25Q_CMD_FAST_READ ||
            s->command == W25Q_CMD_QUAD_READ) {
            /* Normalized SSI byte token for eight dummy clocks. */
            dm_mc02_ospi_transfer(s, 0);
        } else if (s->command == W25Q_CMD_QUAD_IO_READ) {
            /* Non-continuous mode, then four quad dummy clocks (2 bytes). */
            dm_mc02_ospi_transfer(s, 0xf0);
            for (unsigned i = 0; i < 2; ++i) {
                dm_mc02_ospi_transfer(s, 0);
            }
        }
        break;
    default:
        dm_mc02_ospi_end(s);
        dm_mc02_ospi_finish(s, true);
        return;
    }
    for (uint32_t i = 0; i < length; ++i) {
        s->rx_data[i] = dm_mc02_ospi_transfer(s, 0);
    }
    dm_mc02_ospi_end(s);
    s->command_started = true;
    dm_mc02_ospi_finish(s, false);
}

static bool dm_mc02_ospi_page_program_length(const DmMc02Ospi *s,
                                             uint32_t *length)
{
    return dm_mc02_ospi_transfer_length(s, length) &&
           *length <= s->page_size;
}

static void dm_mc02_ospi_execute(DmMc02Ospi *s)
{
    uint32_t length = 0;

    if (!s->has_flash ||
        (dm_mc02_ospi_command_uses_dlr(s->command) &&
         !dm_mc02_ospi_transfer_length(s, &length))) {
        dm_mc02_ospi_finish(s, true);
        return;
    }
    s->command_started = false;
    s->rx_size = 0;
    s->rx_pos = 0;
    s->rx_dma_reserved_size = 0;
    s->rx_dma_reserved = false;
    s->tx_size = 0;
    s->tx_expected = MIN(length, (uint32_t)DM_MC02_OSPI_MAX_PAGE_SIZE);

    switch (s->command) {
    case W25Q_CMD_WRITE_ENABLE:
        dm_mc02_ospi_begin(s, s->command);
        dm_mc02_ospi_end(s);
        dm_mc02_ospi_finish(s, false);
        break;
    case W25Q_CMD_READ_STATUS1:
    case W25Q_CMD_JEDEC_ID:
    case W25Q_CMD_READ:
    case W25Q_CMD_FAST_READ:
    case W25Q_CMD_QUAD_READ:
    case W25Q_CMD_QUAD_IO_READ:
        dm_mc02_ospi_prepare_read(s);
        if ((dm_mc02_ospi_load(s->regs, OCTOSPI_CR, sizeof(uint32_t)) &
             OCTOSPI_CR_FMODE_MASK) == OCTOSPI_FMODE_AUTO &&
            s->command == W25Q_CMD_READ_STATUS1) {
            uint8_t status = s->rx_size ? s->rx_data[0] :
                             dm_mc02_ospi_flash_status(s);
            uint32_t mask = dm_mc02_ospi_load(s->regs, OCTOSPI_PSMKR,
                                              sizeof(uint32_t));
            uint32_t match = dm_mc02_ospi_load(s->regs, OCTOSPI_PSMAR,
                                               sizeof(uint32_t));

            if ((status & mask) == (match & mask)) {
                dm_mc02_ospi_store(s->regs, OCTOSPI_SR,
                                   dm_mc02_ospi_load(s->regs, OCTOSPI_SR,
                                                     sizeof(uint32_t)) |
                                   OCTOSPI_SR_SMF,
                                   sizeof(uint32_t));
            }
        }
        break;
    case W25Q_CMD_SECTOR_ERASE:
    case W25Q_CMD_BLOCK_ERASE_32K:
    case W25Q_CMD_BLOCK_ERASE_64K:
        dm_mc02_ospi_begin(s, s->command);
        dm_mc02_ospi_send_address(s);
        dm_mc02_ospi_end(s);
        dm_mc02_ospi_finish(s, false);
        break;
    case W25Q_CMD_CHIP_ERASE:
        dm_mc02_ospi_begin(s, s->command);
        dm_mc02_ospi_end(s);
        dm_mc02_ospi_finish(s, false);
        break;
    case W25Q_CMD_PAGE_PROGRAM:
    case W25Q_CMD_QUAD_PAGE_PROGRAM:
        if (!dm_mc02_ospi_page_program_length(s, &length)) {
            s->tx_expected = UINT32_MAX;
            dm_mc02_ospi_finish(s, true);
            break;
        }
        dm_mc02_ospi_begin(s, s->command);
        dm_mc02_ospi_send_address(s);
        s->tx_expected = length;
        s->command_started = true;
        break;
    default:
        dm_mc02_ospi_finish(s, true);
        break;
    }
}

static void dm_mc02_ospi_command_write(DmMc02Ospi *s, hwaddr offset,
                                        uint32_t value)
{
    if (offset == OCTOSPI_CCR) {
        s->command_started = false;
    } else if (offset == OCTOSPI_IR) {
        s->command = value;
        s->command_valid = true;
        if (!dm_mc02_ospi_is_address_command(s->command)) {
            dm_mc02_ospi_execute(s);
        }
    } else if (offset == OCTOSPI_AR && s->command_valid) {
        s->command_address = value;
        if (dm_mc02_ospi_is_address_command(s->command)) {
            dm_mc02_ospi_execute(s);
        }
    }
}

static bool dm_mc02_ospi_data_write(DmMc02Ospi *s, uint64_t value,
                                     unsigned size)
{
    if (!s->command_valid || !s->command_started ||
        (s->command != W25Q_CMD_PAGE_PROGRAM &&
         s->command != W25Q_CMD_QUAD_PAGE_PROGRAM) ||
        s->tx_expected > sizeof(s->tx_data) ||
        s->tx_size > s->tx_expected ||
        size > s->tx_expected - s->tx_size) {
        dm_mc02_ospi_end(s);
        dm_mc02_ospi_finish(s, true);
        return false;
    }
    for (unsigned i = 0; i < size; ++i) {
        dm_mc02_ospi_transfer(s, value >> (i * 8));
        s->tx_size++;
    }
    if (s->tx_size >= s->tx_expected) {
        dm_mc02_ospi_end(s);
        s->command_started = false;
        dm_mc02_ospi_finish(s, false);
    }
    return true;
}

static DmMc02DmaEndpointResult dm_mc02_ospi_dma_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    DmMc02Ospi *s = opaque;

    (void)timestamp_ns;
    if (!s || !data || !size || !s->has_flash || !s->command_valid ||
        !s->command_started || s->rx_dma_reserved || s->rx_pos > s->rx_size ||
        size > s->rx_size - s->rx_pos) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    memcpy(data, s->rx_data + s->rx_pos, size);
    s->rx_dma_reserved_size = size;
    s->rx_dma_reserved = true;
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void dm_mc02_ospi_dma_read_commit(void *opaque)
{
    DmMc02Ospi *s = opaque;

    s->rx_pos += s->rx_dma_reserved_size;
    s->rx_dma_reserved_size = 0;
    s->rx_dma_reserved = false;
}

static void dm_mc02_ospi_dma_read_abort(void *opaque)
{
    DmMc02Ospi *s = opaque;

    s->rx_dma_reserved_size = 0;
    s->rx_dma_reserved = false;
}

static bool dm_mc02_ospi_dma_read(void *opaque, uint8_t *data, unsigned size,
                                  uint64_t timestamp_ns)
{
    DmMc02DmaEndpointResult result = dm_mc02_ospi_dma_read_prepare(
        opaque, data, size, timestamp_ns);

    if (result != DM_MC02_DMA_ENDPOINT_ACCEPTED) {
        return false;
    }
    dm_mc02_ospi_dma_read_commit(opaque);
    return true;
}

static bool dm_mc02_ospi_dma_write(void *opaque, const uint8_t *data,
                                   unsigned size, uint64_t timestamp_ns)
{
    DmMc02Ospi *s = opaque;
    uint64_t value = 0;

    (void)timestamp_ns;
    if (!s || !data || !size || size > sizeof(value)) {
        return false;
    }
    for (unsigned i = 0; i < size; ++i) {
        value |= (uint64_t)data[i] << (i * 8);
    }
    return dm_mc02_ospi_data_write(s, value, size);
}

static uint32_t dm_mc02_ospi_status(const DmMc02Ospi *s)
{
    uint32_t status = dm_mc02_ospi_load(s->regs, OCTOSPI_SR,
                                        sizeof(uint32_t));

    status &= ~(OCTOSPI_SR_BUSY | (0x3f << 8));
    if (s->has_flash && ((DmMc02Ospi *)s)->ssi_nor.selected &&
        (dm_mc02_ospi_flash_status((DmMc02Ospi *)s) & W25Q_STATUS_WIP)) {
        status |= OCTOSPI_SR_BUSY;
    }
    if (s->rx_pos < s->rx_size ||
        (s->command_valid && s->tx_size < s->tx_expected)) {
        status |= OCTOSPI_SR_FTF;
    } else {
        status &= ~OCTOSPI_SR_FTF;
    }
    status |= ((s->rx_size - s->rx_pos) & 0x3f) << 8;
    return status;
}

static uint64_t dm_mc02_ospi_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Ospi *s = opaque;
    uint32_t value;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_OSPI_REGION_SIZE ||
        size > DM_MC02_OSPI_REGION_SIZE - offset) {
        return 0;
    }
    if (offset == OCTOSPI_DR) {
        value = UINT32_MAX;
        for (unsigned i = 0; i < size; ++i) {
            if (s->rx_pos < s->rx_size) {
                value &= ~(0xffu << (i * 8));
                value |= (uint32_t)s->rx_data[s->rx_pos++] << (i * 8);
            }
        }
    } else if (offset == OCTOSPI_SR && size == sizeof(uint32_t)) {
        value = dm_mc02_ospi_status(s);
    } else {
        value = dm_mc02_ospi_load(s->regs, offset & ~3u, sizeof(uint32_t));
    }
    return (value >> ((offset % sizeof(uint32_t)) * 8)) &
           (size == sizeof(uint32_t) ? UINT32_MAX :
            ((1u << (size * 8)) - 1u));
}

static void dm_mc02_ospi_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02Ospi *s = opaque;
    uint32_t mask;
    uint32_t old;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_OSPI_REGION_SIZE ||
        size > DM_MC02_OSPI_REGION_SIZE - offset) {
        return;
    }
    mask = size == sizeof(uint32_t) ? UINT32_MAX : ((1u << (size * 8)) - 1u);
    old = dm_mc02_ospi_load(s->regs, offset & ~3u, sizeof(uint32_t));
    old &= ~(mask << ((offset % sizeof(uint32_t)) * 8));
    old |= ((uint32_t)value & mask) << ((offset % sizeof(uint32_t)) * 8);

    if (offset == OCTOSPI_DR) {
        dm_mc02_ospi_data_write(s, value, size);
        return;
    }
    if (offset == OCTOSPI_FCR) {
        uint32_t status = dm_mc02_ospi_load(s->regs, OCTOSPI_SR,
                                             sizeof(uint32_t));
        status &= ~((uint32_t)value & (OCTOSPI_SR_TEF | OCTOSPI_SR_TCF |
                                       OCTOSPI_SR_SMF | (1u << 4)));
        dm_mc02_ospi_store(s->regs, OCTOSPI_SR, status, sizeof(uint32_t));
        return;
    }
    dm_mc02_ospi_store(s->regs, offset & ~3u, old, sizeof(uint32_t));
    if (offset == OCTOSPI_CR && size == sizeof(uint32_t)) {
        s->memory_mapped = (old & OCTOSPI_CR_FMODE_MASK) ==
                           OCTOSPI_FMODE_MEMORY;
    }
    if (size == sizeof(uint32_t) && (offset == OCTOSPI_CCR ||
                                     offset == OCTOSPI_IR ||
                                     offset == OCTOSPI_AR)) {
        dm_mc02_ospi_command_write(s, offset, old);
    }
}

static const MemoryRegionOps dm_mc02_ospi_ops = {
    .read = dm_mc02_ospi_read,
    .write = dm_mc02_ospi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static uint64_t dm_mc02_ospim_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Ospim *s = opaque;
    uint32_t value;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_OSPIM_REGION_SIZE ||
        size > DM_MC02_OSPIM_REGION_SIZE - offset) {
        return 0;
    }
    value = s->regs[offset / sizeof(uint32_t)];
    return (value >> ((offset % sizeof(uint32_t)) * 8)) &
           (size == sizeof(uint32_t) ? UINT32_MAX : ((1u << (size * 8)) - 1u));
}

static void dm_mc02_ospim_write(void *opaque, hwaddr offset, uint64_t value,
                                unsigned size)
{
    DmMc02Ospim *s = opaque;
    uint32_t mask;
    uint32_t old;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_OSPIM_REGION_SIZE ||
        size > DM_MC02_OSPIM_REGION_SIZE - offset) {
        return;
    }
    mask = size == sizeof(uint32_t) ? UINT32_MAX : ((1u << (size * 8)) - 1u);
    old = s->regs[offset / sizeof(uint32_t)];
    old &= ~(mask << ((offset % sizeof(uint32_t)) * 8));
    old |= ((uint32_t)value & mask) << ((offset % sizeof(uint32_t)) * 8);
    s->regs[offset / sizeof(uint32_t)] = old;
}

static const MemoryRegionOps dm_mc02_ospim_ops = {
    .read = dm_mc02_ospim_read,
    .write = dm_mc02_ospim_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_ospi_init(DmMc02Ospi *state, Object *owner)
{
    static const DmMc02OspiFlashConfig default_config = {
        .name = "W25Q64JV",
        .storage_size = DM_MC02_OSPI_FLASH_SIZE,
        .page_size = DM_MC02_OSPI_PAGE_SIZE,
        .sector_size = 0x1000,
        .jedec_id = { 0xef, 0x40, 0x17 },
    };

    dm_mc02_ospi_init_with_config_and_ssi(state, owner, NULL,
                                          &default_config);
}

void dm_mc02_ospi_init_with_flash(DmMc02Ospi *state, Object *owner,
                                  bool with_flash)
{
    dm_mc02_ospi_init_with_config_and_ssi(state, owner, NULL,
                                          with_flash ?
                                          &(const DmMc02OspiFlashConfig) {
                                              .name = "W25Q64JV",
                                              .storage_size = DM_MC02_OSPI_FLASH_SIZE,
                                              .page_size = DM_MC02_OSPI_PAGE_SIZE,
                                              .sector_size = 0x1000,
                                              .jedec_id = { 0xef, 0x40, 0x17 },
                                          } : NULL);
}

void dm_mc02_ospi_init_with_config(DmMc02Ospi *state, Object *owner,
                                   const DmMc02OspiFlashConfig *config)
{
    dm_mc02_ospi_init_with_config_and_ssi(state, owner, NULL, config);
}

void dm_mc02_ospi_init_with_config_and_ssi(
    DmMc02Ospi *state, Object *owner, DeviceState *ssi_parent,
    const DmMc02OspiFlashConfig *config)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_ospi_ops, state,
                          "dm-mc02.octospi2", DM_MC02_OSPI_REGION_SIZE);
    if (config) {
        state->flash_size = config->storage_size;
        state->page_size = config->page_size;
        state->sector_size = config->sector_size;
        memcpy(state->jedec_id, config->jedec_id, sizeof(state->jedec_id));
        state->has_flash = dm_mc02_ospi_valid_config(config) && ssi_parent;
        if (state->has_flash && !dm_mc02_ssi_nor_init(
                &state->ssi_nor, ssi_parent, "dm-mc02.ospi2-ssi",
                config->storage_size, config->jedec_id, &error_fatal)) {
            state->has_flash = false;
        }
    }
    if (state->has_flash) {
        memory_region_init_io(&state->flash_window, owner,
                              &dm_mc02_ospi_flash_ops, state,
                              config->name ? config->name :
                              "dm-mc02.ospi-flash-memory",
                              state->flash_size);
    }
    dm_mc02_ospi_reset(state);
}

void dm_mc02_ospi_reset(DmMc02Ospi *state)
{
    memset(state->regs, 0, sizeof(state->regs));
    state->rx_size = 0;
    state->rx_pos = 0;
    state->rx_dma_reserved_size = 0;
    state->rx_dma_reserved = false;
    state->tx_size = 0;
    state->tx_expected = 0;
    state->command_address = 0;
    state->command = 0;
    state->command_valid = false;
    state->command_started = false;
    dm_mc02_ssi_nor_reset(&state->ssi_nor);
    state->memory_mapped = false;
}

void dm_mc02_ospi_cleanup(DmMc02Ospi *state)
{
    if (state) {
        g_free(state->rx_data);
        state->rx_data = NULL;
    }
}

DmMc02DmaEndpoint dm_mc02_ospi_dma_endpoint(DmMc02Ospi *state)
{
    return (DmMc02DmaEndpoint) {
        .read = dm_mc02_ospi_dma_read,
        .write = dm_mc02_ospi_dma_write,
        .read_prepare = dm_mc02_ospi_dma_read_prepare,
        .read_commit = dm_mc02_ospi_dma_read_commit,
        .read_abort = dm_mc02_ospi_dma_read_abort,
        .opaque = state,
    };
}

DmNorFlashPersistenceResult dm_mc02_ospi_load_persistence(
    DmMc02Ospi *state, const char *path)
{
    if (!state) {
        return DM_NOR_FLASH_PERSISTENCE_INVALID;
    }
    return dm_nor_flash_persistence_load(path, state->ssi_nor.storage,
                                         state->flash_size);
}

DmNorFlashPersistenceResult dm_mc02_ospi_save_persistence(
    const DmMc02Ospi *state, const char *path)
{
    if (!state) {
        return DM_NOR_FLASH_PERSISTENCE_INVALID;
    }
    return dm_nor_flash_persistence_save(path, state->ssi_nor.storage,
                                         state->flash_size);
}

void dm_mc02_ospim_init(DmMc02Ospim *state, Object *owner)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_ospim_ops, state,
                          "dm-mc02.ospim", DM_MC02_OSPIM_REGION_SIZE);
}

#endif /* DM_MC02_OSPI_TEST_FIXTURE */
