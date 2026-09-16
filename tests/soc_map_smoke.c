#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_soc.h"

#include <stdio.h>
#include <stdlib.h>

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "soc map smoke: line %u: %s\n", line, expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static void test_stm32h723_map(void)
{
    const DmMc02SocProfile *soc = dm_mc02_soc_stm32h723();

    CHECK(soc == &dm_mc02_stm32h723_soc);
    CHECK(dm_mc02_soc_validate_map(soc));
    CHECK(soc->tim2_base == 0x40000000);
    CHECK(soc->adc1_base == 0x40022000);
    CHECK(soc->adc2_base == 0x40022100);
    CHECK(soc->adc_common_base == 0x40022300);
}

static void test_rejected_maps(void)
{
    DmMc02SocProfile invalid = dm_mc02_stm32h723_soc;

    CHECK(!dm_mc02_soc_validate_map(NULL));

    invalid.tim2_base = 0;
    CHECK(!dm_mc02_soc_validate_map(&invalid));

    invalid = dm_mc02_stm32h723_soc;
    invalid.adc1_base = 0;
    CHECK(!dm_mc02_soc_validate_map(&invalid));

    invalid = dm_mc02_stm32h723_soc;
    invalid.adc2_base = invalid.adc1_base + 0xff;
    CHECK(!dm_mc02_soc_validate_map(&invalid));

    invalid = dm_mc02_stm32h723_soc;
    invalid.adc_common_base = invalid.adc2_base + 0xff;
    CHECK(!dm_mc02_soc_validate_map(&invalid));
}

int main(void)
{
    test_stm32h723_map();
    test_rejected_maps();
    puts("soc map smoke: PASS");
    return 0;
}
