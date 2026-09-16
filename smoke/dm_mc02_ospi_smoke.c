#include <stdint.h>

#define OSPI2_BASE       0x5200a000u
#define OSPI2_MEMORY     0x70000000u
#define RESULT           ((volatile uint32_t *)0x20000000u)

#define OSPI_CR          (*(volatile uint32_t *)(OSPI2_BASE + 0x000u))
#define OSPI_SR          (*(volatile uint32_t *)(OSPI2_BASE + 0x020u))
#define OSPI_FCR         (*(volatile uint32_t *)(OSPI2_BASE + 0x024u))
#define OSPI_DLR         (*(volatile uint32_t *)(OSPI2_BASE + 0x040u))
#define OSPI_AR          (*(volatile uint32_t *)(OSPI2_BASE + 0x048u))
#define OSPI_DR          (*(volatile uint32_t *)(OSPI2_BASE + 0x050u))
#define OSPI_DR8         (*(volatile uint8_t *)(OSPI2_BASE + 0x050u))
#define OSPI_CCR         (*(volatile uint32_t *)(OSPI2_BASE + 0x100u))
#define OSPI_IR          (*(volatile uint32_t *)(OSPI2_BASE + 0x110u))

#define W25Q_WREN        0x06u
#define W25Q_RDSR1       0x05u
#define W25Q_JEDEC       0x9fu
#define W25Q_QUAD_PP     0x32u
#define W25Q_SECTOR_ERASE 0x20u
#define W25Q_BLOCK_ERASE_32K 0x52u
#define W25Q_BLOCK_ERASE_64K 0xd8u
#define W25Q_CHIP_ERASE  0xc7u

#define OSPI_FMODE_MEMORY (3u << 28)
#define TEST_ADDRESS      0x000100u

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static void ospi_indirect(void)
{
    OSPI_CR = 0;
    OSPI_FCR = 0x1eu;
}

static void ospi_read_command(uint32_t command, uint32_t address,
                              uint32_t length, uint8_t *data)
{
    ospi_indirect();
    OSPI_DLR = length - 1u;
    OSPI_CCR = 0;
    OSPI_IR = command;
    if (command != W25Q_JEDEC && command != W25Q_RDSR1) {
        OSPI_AR = address;
    }
    for (uint32_t i = 0; i < length; ++i) {
        data[i] = OSPI_DR8;
    }
}

static uint8_t read_status(void)
{
    uint8_t status;
    ospi_read_command(W25Q_RDSR1, 0, 1, &status);
    return status;
}

static void write_enable(void)
{
    ospi_indirect();
    OSPI_DLR = 0;
    OSPI_CCR = 0;
    OSPI_IR = W25Q_WREN;
}

static void quad_page_program(uint32_t address, const uint32_t *data)
{
    ospi_indirect();
    OSPI_DLR = 15;
    OSPI_CCR = 0;
    OSPI_IR = W25Q_QUAD_PP;
    OSPI_AR = address;
    for (uint32_t i = 0; i < 4; ++i) {
        OSPI_DR = data[i];
    }
}

static void oversized_page_program(uint32_t address)
{
    ospi_indirect();
    OSPI_DLR = 256;
    OSPI_CCR = 0;
    OSPI_IR = W25Q_QUAD_PP;
    OSPI_AR = address;
    for (uint32_t i = 0; i < 64; ++i) {
        OSPI_DR = 0;
    }
    OSPI_DR8 = 0;
}

static void erase_command(uint32_t command, uint32_t address)
{
    ospi_indirect();
    OSPI_DLR = 0;
    OSPI_CCR = 0;
    OSPI_IR = command;
    if (command != W25Q_CHIP_ERASE) {
        OSPI_AR = address;
    }
}

static void sector_erase(uint32_t address)
{
    erase_command(W25Q_SECTOR_ERASE, address);
}

void Reset_Handler(void)
{
    static const uint32_t pattern[4] = {
        0x11223344u, 0x55667788u, 0x99aabbccu, 0xddeeff00u,
    };
    uint8_t jedec[3];
    uint8_t status;
    volatile uint32_t *window = (volatile uint32_t *)OSPI2_MEMORY;

    RESULT[0] = 0x4f535032u; /* "OSP2" */
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[33] = window[TEST_ADDRESS / 4];
    RESULT[34] = window[TEST_ADDRESS / 4 + 1];
    ospi_read_command(W25Q_JEDEC, 0, 3, jedec);
    RESULT[1] = (uint32_t)jedec[0] | ((uint32_t)jedec[1] << 8) |
                ((uint32_t)jedec[2] << 16);

    write_enable();
    status = read_status();
    RESULT[2] = status;

    quad_page_program(TEST_ADDRESS, pattern);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[3] = window[TEST_ADDRESS / 4];
    RESULT[4] = window[TEST_ADDRESS / 4 + 1];
    RESULT[5] = window[TEST_ADDRESS / 4 + 2];
    RESULT[6] = window[TEST_ADDRESS / 4 + 3];
    RESULT[35] = read_status();

    write_enable();
    sector_erase(TEST_ADDRESS);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[7] = window[TEST_ADDRESS / 4];
    RESULT[8] = window[TEST_ADDRESS / 4 + 1];
    RESULT[9] = window[TEST_ADDRESS / 4 + 2];
    RESULT[10] = window[TEST_ADDRESS / 4 + 3];

    /* The command/data path must reject programming without WREN. */
    quad_page_program(TEST_ADDRESS, pattern);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[11] = window[TEST_ADDRESS / 4];
    RESULT[12] = window[TEST_ADDRESS / 4 + 1];
    RESULT[13] = window[TEST_ADDRESS / 4 + 2];
    RESULT[14] = window[TEST_ADDRESS / 4 + 3];
    RESULT[15] = read_status();
    RESULT[16] = 1;

    /* A page program longer than 256 bytes is rejected atomically, even when
     * WREN is set and the staging buffer reaches its page-sized limit. */
    write_enable();
    oversized_page_program(TEST_ADDRESS + 0x400u);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[17] = window[(TEST_ADDRESS + 0x400u) / 4];
    RESULT[18] = window[(TEST_ADDRESS + 0x4fcu) / 4];
    RESULT[19] = read_status();

    /* DLR is length minus one; UINT32_MAX must not wrap to an accepted zero
     * length program. */
    write_enable();
    ospi_indirect();
    OSPI_DLR = UINT32_MAX;
    OSPI_CCR = 0;
    OSPI_IR = W25Q_QUAD_PP;
    OSPI_AR = TEST_ADDRESS + 0x600u;
    RESULT[20] = OSPI_SR;
    RESULT[21] = read_status();

    /* A maximum DLR on a read must fail instead of wrapping to zero bytes. */
    ospi_indirect();
    OSPI_DLR = UINT32_MAX;
    OSPI_CCR = 0;
    OSPI_IR = 0x03u;
    OSPI_AR = TEST_ADDRESS;
    RESULT[22] = OSPI_SR;
    RESULT[23] = read_status();

    /* Exercise all W25Q erase granularities exposed by the board driver. */
    write_enable();
    quad_page_program(0x008000u, pattern);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[24] = window[0x008000u / 4];
    write_enable();
    erase_command(W25Q_BLOCK_ERASE_32K, 0x008001u);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[25] = window[0x008000u / 4];

    write_enable();
    quad_page_program(0x010000u, pattern);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[26] = window[0x010000u / 4];
    write_enable();
    erase_command(W25Q_BLOCK_ERASE_64K, 0x010001u);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[27] = window[0x010000u / 4];

    write_enable();
    quad_page_program(0x020000u, pattern);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[28] = window[0x020000u / 4];
    write_enable();
    erase_command(W25Q_CHIP_ERASE, 0);
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[29] = window[0x020000u / 4];
    RESULT[30] = read_status();

    /* No-data commands must not depend on the previous transaction length. */
    write_enable();
    ospi_indirect();
    OSPI_DLR = UINT32_MAX;
    OSPI_CCR = 0;
    OSPI_IR = W25Q_CHIP_ERASE;
    OSPI_CR = OSPI_FMODE_MEMORY;
    RESULT[31] = window[0x020000u / 4];
    RESULT[32] = read_status();

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
