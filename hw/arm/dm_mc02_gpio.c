/*
 * Minimal STM32H7 GPIO bank implementation used by DM-MC02.
 *
 * There is deliberately no alternate-function matrix or electrical drive
 * model in this first stage; board inputs are injected explicitly by the
 * machine/fixture layer.
 */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_gpio.h"

#define GPIO_MODER   0x00
#define GPIO_OTYPER  0x04
#define GPIO_OSPEEDR 0x08
#define GPIO_PUPDR   0x0c
#define GPIO_IDR     0x10
#define GPIO_ODR     0x14
#define GPIO_BSRR    0x18
#define GPIO_AFR0    0x20
#define GPIO_AFR1    0x24

static uint32_t dm_mc02_gpio_output_mask(const DmMc02GpioBank *bank)
{
    uint32_t mask = 0;

    for (unsigned pin = 0; pin < 16; ++pin) {
        if (((bank->moder >> (pin * 2)) & 3) == 1) {
            mask |= 1u << pin;
        }
    }
    return mask;
}

static bool gpio_lane(hwaddr offset, unsigned size, hwaddr reg)
{
    return offset >= reg && offset <= reg + sizeof(uint32_t) - size;
}

static uint32_t gpio_lane_mask(unsigned size, unsigned shift)
{
    uint32_t width = size == sizeof(uint32_t) ? UINT32_MAX :
                     (UINT32_C(1) << (size * 8)) - 1;

    return width << shift;
}

static uint32_t gpio_register_value(const DmMc02GpioBank *bank, hwaddr reg)
{
    switch (reg) {
    case GPIO_MODER:
        return bank->moder;
    case GPIO_OTYPER:
        return bank->otyper;
    case GPIO_OSPEEDR:
        return bank->ospeedr;
    case GPIO_PUPDR:
        return bank->pupdr;
    case GPIO_IDR: {
        uint32_t outputs = dm_mc02_gpio_output_mask(bank);

        return (bank->idr & ~outputs) | (bank->odr & outputs);
    }
    case GPIO_ODR:
        return bank->odr;
    case GPIO_AFR0:
        return bank->afr0;
    case GPIO_AFR1:
        return bank->afr1;
    default:
        return 0;
    }
}

static uint64_t dm_mc02_gpio_read(void *opaque, hwaddr offset,
                                  unsigned size)
{
    DmMc02GpioBank *bank = opaque;
    static const hwaddr registers[] = {
        GPIO_MODER, GPIO_OTYPER, GPIO_OSPEEDR, GPIO_PUPDR,
        GPIO_IDR, GPIO_ODR, GPIO_AFR0, GPIO_AFR1,
    };

    for (unsigned i = 0; i < ARRAY_SIZE(registers); ++i) {
        hwaddr reg = registers[i];

        if (gpio_lane(offset, size, reg)) {
            unsigned shift = (offset - reg) * 8;
            uint32_t mask = gpio_lane_mask(size, shift);

            return (gpio_register_value(bank, reg) & mask) >> shift;
        }
    }
    /* LCKR and all other offsets are not part of this milestone. */
    return 0;
}

static void dm_mc02_gpio_notify_changed(DmMc02GpioBank *bank)
{
    if (bank->odr_changed) {
        bank->odr_changed(bank->odr_changed_opaque, bank->bank_index,
                          bank->odr);
    }
}

static void dm_mc02_gpio_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02GpioBank *bank = opaque;
    uint32_t old_odr = bank->odr;
    bool config_changed = false;

    static const hwaddr registers[] = {
        GPIO_MODER, GPIO_OTYPER, GPIO_OSPEEDR, GPIO_PUPDR,
        GPIO_ODR, GPIO_BSRR, GPIO_AFR0, GPIO_AFR1,
    };
    for (unsigned i = 0; i < ARRAY_SIZE(registers); ++i) {
        hwaddr reg = registers[i];

        if (gpio_lane(offset, size, reg)) {
            unsigned shift = (offset - reg) * 8;
            uint32_t lane_mask = gpio_lane_mask(size, shift);
            uint32_t lane_value = (uint32_t)value;

            if (reg == GPIO_BSRR) {
                /* Expand a sub-word write into the corresponding 32-bit
                 * BSRR lane before applying its atomic set/reset action. */
                uint32_t command = (lane_value << shift) & lane_mask;

                bank->odr = (bank->odr | (command & 0xffff)) &
                            ~(command >> 16 & 0xffff);
            } else {
                uint32_t *target;
                uint32_t old_value;
                uint32_t next;

                switch (reg) {
                case GPIO_MODER:
                    target = &bank->moder;
                    break;
                case GPIO_OTYPER:
                    target = &bank->otyper;
                    break;
                case GPIO_OSPEEDR:
                    target = &bank->ospeedr;
                    break;
                case GPIO_PUPDR:
                    target = &bank->pupdr;
                    break;
                case GPIO_ODR:
                    target = &bank->odr;
                    break;
                case GPIO_AFR0:
                    target = &bank->afr0;
                    break;
                case GPIO_AFR1:
                    target = &bank->afr1;
                    break;
                default:
                    target = NULL;
                    break;
                }
                if (!target) {
                    break;
                }
                old_value = *target;
                next = (old_value & ~lane_mask) |
                       ((lane_value << shift) & lane_mask);
                if (reg == GPIO_OTYPER || reg == GPIO_ODR) {
                    next &= 0xffff;
                }
                *target = next;
                if (reg == GPIO_MODER || reg == GPIO_AFR0 ||
                    reg == GPIO_AFR1) {
                    config_changed = old_value != next;
                }
            }
            break;
        }
    }
    if (bank->odr != old_odr || config_changed) {
        dm_mc02_gpio_notify_changed(bank);
    }
}

static const MemoryRegionOps dm_mc02_gpio_ops = {
    .read = dm_mc02_gpio_read,
    .write = dm_mc02_gpio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_gpio_init(DmMc02GpioBank *bank, Object *owner,
                       const char *name, unsigned bank_index,
                       DmMc02GpioOdrChanged *odr_changed,
                       void *odr_changed_opaque)
{
    memset(bank, 0, sizeof(*bank));
    bank->bank_index = bank_index;
    bank->odr_changed = odr_changed;
    bank->odr_changed_opaque = odr_changed_opaque;
    memory_region_init_io(&bank->iomem, owner, &dm_mc02_gpio_ops, bank,
                          name, 0x400);
}

void dm_mc02_gpio_set_odr(DmMc02GpioBank *bank, uint32_t value)
{
    if (bank->odr != (value & 0xffff)) {
        bank->odr = value & 0xffff;
        dm_mc02_gpio_notify_changed(bank);
    }
}

void dm_mc02_gpio_set_input(DmMc02GpioBank *bank, uint32_t mask,
                            uint32_t value)
{
    uint32_t next;

    if (!bank) {
        return;
    }
    next = (bank->idr & ~mask) | (value & mask);
    bank->idr = next & 0xffff;
}

void dm_mc02_gpio_reset(DmMc02GpioBank *bank)
{
    if (!bank) {
        return;
    }
    bank->moder = 0;
    bank->otyper = 0;
    bank->ospeedr = 0;
    bank->pupdr = 0;
    bank->idr = 0;
    bank->odr = 0;
    bank->afr0 = 0;
    bank->afr1 = 0;
    dm_mc02_gpio_notify_changed(bank);
}
