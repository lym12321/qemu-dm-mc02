/* Minimal functional STM32H723 CRC register model for DM-MC02. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_crc.h"

#define CRC_DR      0x00
#define CRC_IDR     0x04
#define CRC_CR      0x08
#define CRC_INIT    0x10
#define CRC_POL     0x14

#define CRC_CR_RESET          (1u << 0)
#define CRC_CR_POLYSIZE_SHIFT 3
#define CRC_CR_POLYSIZE_MASK  (3u << CRC_CR_POLYSIZE_SHIFT)
#define CRC_CR_REV_IN_SHIFT   5
#define CRC_CR_REV_IN_MASK    (3u << CRC_CR_REV_IN_SHIFT)
#define CRC_CR_REV_OUT        (1u << 7)
#define CRC_DEFAULT_POLY      UINT32_C(0x04c11db7)
#define CRC_DEFAULT_INIT      UINT32_C(0xffffffff)

static uint32_t crc_load(const uint8_t *regs, hwaddr offset, unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)regs[offset + i] << (i * 8);
    }
    return value;
}

static void crc_store(uint8_t *regs, hwaddr offset, uint32_t value,
                      unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        regs[offset + i] = value >> (i * 8);
    }
}

static unsigned crc_width(const DmMc02Crc *s)
{
    switch ((crc_load((const uint8_t *)s->regs, CRC_CR, 4) &
             CRC_CR_POLYSIZE_MASK) >> CRC_CR_POLYSIZE_SHIFT) {
    case 1:
        return 16;
    case 2:
        return 8;
    case 3:
        return 7;
    default:
        return 32;
    }
}

static uint32_t bit_reverse(uint32_t value, unsigned bits)
{
    uint32_t result = 0;

    for (unsigned i = 0; i < bits; ++i) {
        result = (result << 1) | ((value >> i) & 1u);
    }
    return result;
}

static uint32_t crc_input_transform(uint32_t value, unsigned size,
                                    unsigned mode)
{
    switch (mode) {
    case 1: /* reverse each byte */
        for (unsigned i = 0; i < size; ++i) {
            uint32_t byte = bit_reverse((value >> (i * 8)) & 0xffu, 8);
            value = (value & ~(UINT32_C(0xff) << (i * 8))) |
                    (byte << (i * 8));
        }
        return value;
    case 2: /* reverse each half-word */
        for (unsigned i = 0; i < size; i += 2) {
            uint32_t half = bit_reverse((value >> (i * 8)) & 0xffffu, 16);
            value = (value & ~(UINT32_C(0xffff) << (i * 8))) |
                    (half << (i * 8));
        }
        return value;
    case 3: /* reverse the complete input word */
        return bit_reverse(value, size * 8);
    default:
        return value;
    }
}

static void crc_feed(DmMc02Crc *s, uint32_t input, unsigned size)
{
    uint32_t cr = crc_load((const uint8_t *)s->regs, CRC_CR, 4);
    unsigned width = crc_width(s);
    uint32_t mask = width == 32 ? UINT32_MAX : ((UINT32_C(1) << width) - 1);
    uint32_t top = UINT32_C(1) << (width - 1);
    uint32_t poly = crc_load((const uint8_t *)s->regs, CRC_POL, 4) & mask;
    unsigned reverse_in = (cr & CRC_CR_REV_IN_MASK) >> CRC_CR_REV_IN_SHIFT;

    input = crc_input_transform(input, size, reverse_in);
    if (width == 32 && poly == 0) {
        poly = CRC_DEFAULT_POLY;
    }
    s->value &= mask;
    for (unsigned byte = 0; byte < size; ++byte) {
        uint8_t data = input >> ((size - 1 - byte) * 8);

        for (unsigned bit = 0; bit < 8; ++bit) {
            bool high = (s->value & top) != 0;

            s->value = (s->value << 1) & mask;
            if (high ^ ((data & (UINT8_C(0x80) >> bit)) != 0)) {
                s->value ^= poly;
            }
        }
    }
}

static uint32_t crc_result(const DmMc02Crc *s)
{
    uint32_t cr = crc_load((const uint8_t *)s->regs, CRC_CR, 4);
    unsigned width = crc_width(s);
    uint32_t mask = width == 32 ? UINT32_MAX : ((UINT32_C(1) << width) - 1);
    uint32_t value = s->value & mask;

    if (cr & CRC_CR_REV_OUT) {
        value = bit_reverse(value, width);
    }
    return value;
}

static uint64_t dm_mc02_crc_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Crc *s = opaque;
    uint32_t value;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_CRC_REGION_SIZE ||
        size > DM_MC02_CRC_REGION_SIZE - offset) {
        return 0;
    }
    value = offset == CRC_DR ? crc_result(s) :
            crc_load((const uint8_t *)s->regs, offset & ~3u, 4);
    return (value >> ((offset & 3u) * 8)) &
           (size == sizeof(uint32_t) ? UINT32_MAX :
            ((UINT32_C(1) << (size * 8)) - 1));
}

static void dm_mc02_crc_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    DmMc02Crc *s = opaque;
    uint32_t value32 = (uint32_t)value;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_CRC_REGION_SIZE ||
        size > DM_MC02_CRC_REGION_SIZE - offset) {
        return;
    }
    if (offset == CRC_DR) {
        crc_feed(s, value32, size);
        return;
    }
    if (offset == CRC_CR && size == sizeof(uint32_t)) {
        uint32_t next = value32 & ~CRC_CR_RESET;

        crc_store((uint8_t *)s->regs, CRC_CR, next, sizeof(uint32_t));
        if (value32 & CRC_CR_RESET) {
            s->value = crc_load((const uint8_t *)s->regs, CRC_INIT, 4);
        }
        return;
    }
    crc_store((uint8_t *)s->regs, offset, value32, size);
}

static const MemoryRegionOps dm_mc02_crc_ops = {
    .read = dm_mc02_crc_read,
    .write = dm_mc02_crc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_crc_reset(DmMc02Crc *state)
{
    if (!state) {
        return;
    }
    memset(state->regs, 0, sizeof(state->regs));
    crc_store((uint8_t *)state->regs, CRC_INIT, CRC_DEFAULT_INIT, 4);
    crc_store((uint8_t *)state->regs, CRC_POL, CRC_DEFAULT_POLY, 4);
    state->value = CRC_DEFAULT_INIT;
}

void dm_mc02_crc_init(DmMc02Crc *state, Object *owner)
{
    memset(state, 0, sizeof(*state));
    dm_mc02_crc_reset(state);
    memory_region_init_io(&state->iomem, owner, &dm_mc02_crc_ops, state,
                          "dm-mc02.crc", DM_MC02_CRC_REGION_SIZE);
}
