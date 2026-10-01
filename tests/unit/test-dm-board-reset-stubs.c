/* Narrow component reset stubs for the board reset-order unit target. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_exti.h"
#include "hw/arm/dm_mc02_gpio.h"
#include "hw/arm/dm_mc02_power.h"
#include "hw/arm/dm_mc02_syscfg.h"

void dm_test_board_reset_record(unsigned event);

void dm_mc02_gpio_reset(DmMc02GpioBank *bank)
{
    dm_test_board_reset_record(100 + bank->bank_index);
}

void dm_mc02_syscfg_reset(DmMc02Syscfg *state)
{
    (void)state;
    dm_test_board_reset_record(300);
}

void dm_mc02_exti_reset(DmMc02Exti *state)
{
    (void)state;
    dm_test_board_reset_record(500);
}

void dm_mc02_power_reset(DmMc02Power *state)
{
    (void)state;
    dm_test_board_reset_record(700);
}
