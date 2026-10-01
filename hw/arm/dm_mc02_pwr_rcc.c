/*
 * Minimal STM32H723 PWR and RCC model for DM-MC02.
 *
 * This is a boot-enablement model, not a cycle-accurate clock tree.  The
 * register windows are intentionally backed by byte arrays so byte/halfword
 * accesses and unknown offsets remain safe and retain their values.
 */
#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "hw/arm/dm_stm32h7_clock_tree.h"
#include "hw/arm/dm_mc02_pwr_rcc.h"

#define PWR_CR1       0x00
#define PWR_CSR1      0x04
#define PWR_CR2       0x08
#define PWR_CR3       0x0c
#define PWR_CPUCR     0x10
#define PWR_D3CR      0x18
#define PWR_WKUPCR    0x20
#define PWR_WKUPFR    0x24
#define PWR_WKUPEPR   0x28

#define PWR_CSR1_ACTVOSRDY (1u << 13)
#define PWR_CSR1_ACTVOS    (3u << 14)
#define PWR_CR3_USB33RDY   (1u << 26)
#define PWR_CR3_SCUEN     (1u << 2)
#define PWR_CR3_LDOEN     (1u << 1)
#define PWR_D3CR_VOS      (3u << 14)
#define PWR_D3CR_VOSRDY   (1u << 13)

#define RCC_CR            0x00
#define RCC_HSICFGR       0x04
#define RCC_CRRCR         0x08
#define RCC_CSICFGR       0x0c
#define RCC_CFGR          0x10
#define RCC_D1CFGR        0x18
#define RCC_D2CFGR        0x1c
#define RCC_D3CFGR        0x20
#define RCC_PLLCKSELR     0x28
#define RCC_PLLCFGR       0x2c
#define RCC_PLL1DIVR      0x30
#define RCC_PLL1FRACR     0x34
#define RCC_PLL2DIVR      0x38
#define RCC_PLL2FRACR     0x3c
#define RCC_PLL3DIVR      0x40
#define RCC_PLL3FRACR     0x44
#define RCC_D1CCIPR       0x4c
#define RCC_D2CCIP1R      0x50
#define RCC_D2CCIP2R      0x54
#define RCC_D3CCIPR       0x58
#define RCC_CIER          0x60
#define RCC_CIFR          0x64
#define RCC_CICR          0x68
#define RCC_BDCR          0x70
#define RCC_CSR           0x74
#define RCC_AHB3RSTR      0x7c
#define RCC_AHB1RSTR      0x80
#define RCC_AHB2RSTR      0x84
#define RCC_AHB4RSTR      0x88
#define RCC_APB3RSTR      0x8c
#define RCC_APB1LRSTR     0x90
#define RCC_APB1HRSTR     0x94
#define RCC_APB2RSTR      0x98
#define RCC_APB4RSTR      0x9c
#define RCC_GCR           0xa0
#define RCC_D3AMR         0xa8
#define RCC_RSR           DM_MC02_RCC_RSR_OFFSET
#define RCC_AHB3ENR       0xd4
#define RCC_AHB1ENR       0xd8
#define RCC_AHB2ENR       0xdc
#define RCC_AHB4ENR       0xe0
#define RCC_APB3ENR       0xe4
#define RCC_APB1LENR      0xe8
#define RCC_APB1HENR      0xec
#define RCC_APB2ENR       0xf0
#define RCC_APB4ENR       0xf4
#define RCC_AHB3LPENR     0xfc
#define RCC_AHB1LPENR     0x100
#define RCC_AHB2LPENR     0x104
#define RCC_AHB4LPENR     0x108
#define RCC_APB3LPENR     0x10c
#define RCC_APB1LLPENR    0x110
#define RCC_APB1HLPENR    0x114
#define RCC_APB2LPENR     0x118
#define RCC_APB4LPENR     0x11c

#define DM_MC02_HSI_HZ    64000000ULL
#define DM_MC02_HSE_HZ    24000000ULL
#define DM_MC02_CSI_HZ    4000000ULL
#define DM_MC02_LSE_HZ    32768ULL
#define DM_MC02_ADC_DEFAULT_KERNEL_HZ 96000000ULL
#define DM_MC02_ADC_PRESCALER 64ULL

#define RCC_CR_HSION      (1u << 0)
#define RCC_CR_HSIRDY     (1u << 2)
#define RCC_CR_HSIDIV     (3u << 3)
#define RCC_CR_CSION      (1u << 7)
#define RCC_CR_CSIRDY     (1u << 8)
#define RCC_CR_HSI48ON    (1u << 12)
#define RCC_CR_HSI48RDY   (1u << 13)
#define RCC_CR_HSEON      (1u << 16)
#define RCC_CR_HSERDY     (1u << 17)
#define RCC_CR_HSEBYP     (1u << 18)
#define RCC_CR_CSSHSEON   (1u << 19)
#define RCC_CR_PLL1ON     (1u << 24)
#define RCC_CR_PLL1RDY    (1u << 25)
#define RCC_CR_PLL2ON     (1u << 26)
#define RCC_CR_PLL2RDY    (1u << 27)
#define RCC_CR_PLL3ON     (1u << 28)
#define RCC_CR_PLL3RDY    (1u << 29)
#define RCC_PLLCFGR_PLL1FRACEN (1u << 0)
#define RCC_PLLCFGR_PLL2FRACEN (1u << 4)
#define RCC_PLLCFGR_PLL3FRACEN (1u << 8)
#define RCC_PLLCFGR_PLL1DIVPEN (1u << 16)
#define RCC_PLLCFGR_PLL1DIVQEN (1u << 17)
#define RCC_PLLCFGR_PLL1DIVREN (1u << 18)
#define RCC_PLLCFGR_PLL2DIVPEN (1u << 19)
#define RCC_PLLCFGR_PLL2DIVQEN (1u << 20)
#define RCC_PLLCFGR_PLL2DIVREN (1u << 21)
#define RCC_PLLCFGR_PLL3DIVPEN (1u << 22)
#define RCC_PLLCFGR_PLL3DIVQEN (1u << 23)
#define RCC_PLLCFGR_PLL3DIVREN (1u << 24)
#define RCC_BDCR_LSEON    (1u << 0)
#define RCC_BDCR_LSERDY   (1u << 1)
#define RCC_CSR_LSION     (1u << 0)
#define RCC_CSR_LSIRDY    (1u << 1)

#define RCC_CR_WRITABLE (RCC_CR_HSION | (1u << 1) | RCC_CR_HSIDIV | \
                         RCC_CR_CSION | (1u << 9) | RCC_CR_HSI48ON | \
                         RCC_CR_HSEON | RCC_CR_HSEBYP | RCC_CR_CSSHSEON | \
                         RCC_CR_PLL1ON | RCC_CR_PLL2ON | RCC_CR_PLL3ON)
#define RCC_CR_READY (RCC_CR_HSIRDY | RCC_CR_CSIRDY | RCC_CR_HSI48RDY | \
                      RCC_CR_HSERDY | RCC_CR_PLL1RDY | RCC_CR_PLL2RDY | \
                      RCC_CR_PLL3RDY)

static uint32_t dm_mc02_reg_load(const uint8_t *regs, hwaddr offset,
                                 unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)regs[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_reg_store(uint8_t *regs, hwaddr offset, uint32_t value,
                              unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        regs[offset + i] = value >> (i * 8);
    }
}

static uint64_t dm_mc02_hsi_divider(uint32_t value)
{
    static const uint32_t dividers[] = { 1, 2, 4, 8 };

    return dividers[(value >> 3) & 3u];
}

static bool dm_mc02_rcc_oscillator_ready(const DmMc02PwrRcc *state,
                                         unsigned source)
{
    uint32_t cr;

    if (!state) {
        return false;
    }
    cr = dm_mc02_reg_load(state->rcc_regs, RCC_CR, sizeof(uint32_t));
    switch (source) {
    case 0: /* HSI */
        return (cr & RCC_CR_HSION) != 0;
    case 1: /* CSI */
        return (cr & RCC_CR_CSION) != 0;
    case 2: /* HSE */
        return (cr & RCC_CR_HSEON) != 0;
    default:
        return false;
    }
}

static bool dm_mc02_rcc_lse_ready(const DmMc02PwrRcc *state)
{
    uint32_t bdcr;

    if (!state) {
        return false;
    }
    bdcr = dm_mc02_reg_load(state->rcc_regs, RCC_BDCR, sizeof(uint32_t));
    return (bdcr & RCC_BDCR_LSEON) != 0;
}

static bool dm_mc02_rcc_pll_input_ready(const DmMc02PwrRcc *state)
{
    uint32_t pllckselr;

    if (!state) {
        return false;
    }
    pllckselr = dm_mc02_reg_load(state->rcc_regs, RCC_PLLCKSELR,
                                  sizeof(uint32_t));
    switch (pllckselr & 3u) {
    case 0:
    case 1:
    case 2:
        return dm_mc02_rcc_oscillator_ready(state, pllckselr & 3u);
    default:
        return false;
    }
}

static uint64_t dm_mc02_pll_output_hz(const DmMc02PwrRcc *state,
                                      uint32_t on_bit, unsigned m_shift,
                                      hwaddr divr_offset, hwaddr fracr_offset,
                                      uint32_t frac_enable, unsigned out_shift,
                                      uint32_t output_enable);

static bool dm_mc02_rcc_system_source_ready(const DmMc02PwrRcc *state,
                                            unsigned source)
{
    if (source <= 2) {
        return dm_mc02_rcc_oscillator_ready(state, source);
    }
    if (source == 3) {
        return dm_mc02_pll_output_hz(state, RCC_CR_PLL1ON, 4,
                                     RCC_PLL1DIVR, RCC_PLL1FRACR,
                                     RCC_PLLCFGR_PLL1FRACEN, 9,
                                     RCC_PLLCFGR_PLL1DIVPEN) != 0;
    }
    return false;
}

static void dm_mc02_rcc_try_switch_system_clock(DmMc02PwrRcc *state)
{
    uint32_t cfgr;
    unsigned requested;

    if (!state) {
        return;
    }
    cfgr = dm_mc02_reg_load(state->rcc_regs, RCC_CFGR, sizeof(uint32_t));
    requested = cfgr & 7u;
    if (requested <= 3 &&
        dm_mc02_rcc_system_source_ready(state, requested)) {
        state->system_clock_source = requested;
    }
}

static uint64_t dm_mc02_pwr_rcc_system_clock_hz(const DmMc02PwrRcc *state)
{
    uint32_t cr;
    uint64_t system_clock;
    unsigned source;

    if (!state) {
        return 0;
    }
    cr = dm_mc02_reg_load(state->rcc_regs, RCC_CR, sizeof(uint32_t));
    /* SW is only a request.  The effective source changes at the RCC
     * readiness boundary and is reported through SWS. */
    source = state->system_clock_source;
    if (!dm_mc02_rcc_system_source_ready(state, source)) {
        return 0;
    }

    switch (source) {
    case 0: /* HSI */
        system_clock = DM_MC02_HSI_HZ / dm_mc02_hsi_divider(cr);
        break;
    case 1: /* CSI */
        system_clock = DM_MC02_CSI_HZ;
        break;
    case 2: /* HSE */
        system_clock = DM_MC02_HSE_HZ;
        break;
    case 3: { /* PLL1 */
        uint32_t pllckselr = dm_mc02_reg_load(state->rcc_regs,
                                               RCC_PLLCKSELR,
                                               sizeof(uint32_t));
        uint32_t pll1divr = dm_mc02_reg_load(state->rcc_regs,
                                              RCC_PLL1DIVR,
                                              sizeof(uint32_t));
        uint64_t pll_source;
        /* PLL1M1 occupies the same six-bit field width as PLL2M2/PLL3M3
         * on STM32H723.  The board uses M=2, so a four-bit mask happened to
         * work for the original firmware; larger legal dividers would have
         * silently produced the wrong CPU clock. */
        uint64_t pllm = (pllckselr >> 4) & 0x3fu;
        uint64_t plln = (pll1divr & 0x1ffu) + 1u;
        uint64_t pllp = ((pll1divr >> 9) & 0x7fu) + 1u;
        uint64_t fracn = 0;
        uint32_t pllcfgr = dm_mc02_reg_load(state->rcc_regs,
                                             RCC_PLLCFGR, sizeof(uint32_t));
        uint32_t pll1fracr = dm_mc02_reg_load(state->rcc_regs,
                                               RCC_PLL1FRACR,
                                               sizeof(uint32_t));

        if (!(cr & RCC_CR_PLL1ON) || !pllm || !pllp ||
            !dm_mc02_rcc_pll_input_ready(state)) {
            return 0;
        }
        switch (pllckselr & 3u) {
        case 0:
            pll_source = DM_MC02_HSI_HZ / dm_mc02_hsi_divider(cr);
            break;
        case 1:
            pll_source = DM_MC02_CSI_HZ;
            break;
        case 2:
            pll_source = DM_MC02_HSE_HZ;
            break;
        case 3:
            return 0; /* reserved / no PLL clock source */
        default:
            return 0;
        }
        if (pllcfgr & RCC_PLLCFGR_PLL1FRACEN) {
            fracn = (pll1fracr >> 3) & 0x1fffu;
        }
        /* PLLN is N+1 and FRACN contributes FRACN/8192.  Keep the
         * fractional part until the final division. */
        system_clock = muldiv64(pll_source * (plln * 8192u + fracn),
                                1, pllm * pllp * 8192u);
        break;
    }
    default:
        return 0;
    }

    return system_clock;
}

uint64_t dm_mc02_pwr_rcc_cpu_clock_hz(const DmMc02PwrRcc *state)
{
    uint64_t system_clock = dm_mc02_pwr_rcc_system_clock_hz(state);

    if (!system_clock) {
        return 0;
    }
    return dm_stm32h7_cpu_clock_hz(system_clock,
        dm_mc02_reg_load(state->rcc_regs, RCC_D1CFGR, sizeof(uint32_t)));
}

uint64_t dm_mc02_pwr_rcc_hclk_hz(const DmMc02PwrRcc *state)
{
    uint64_t system_clock = dm_mc02_pwr_rcc_system_clock_hz(state);

    if (!system_clock) {
        return 0;
    }
    return dm_stm32h7_hclk_hz(system_clock,
        dm_mc02_reg_load(state->rcc_regs, RCC_D1CFGR, sizeof(uint32_t)));
}

uint64_t dm_mc02_pwr_rcc_apb1_clock_hz(const DmMc02PwrRcc *state)
{
    uint64_t hclk_hz = dm_mc02_pwr_rcc_hclk_hz(state);

    if (!hclk_hz) {
        return 0;
    }
    return dm_stm32h7_apb1_hz(
        hclk_hz,
        dm_mc02_reg_load(state->rcc_regs, RCC_D2CFGR, sizeof(uint32_t)));
}

uint64_t dm_mc02_pwr_rcc_apb2_clock_hz(const DmMc02PwrRcc *state)
{
    uint64_t hclk_hz = dm_mc02_pwr_rcc_hclk_hz(state);

    if (!hclk_hz) {
        return 0;
    }
    return dm_stm32h7_apb2_hz(
        hclk_hz,
        dm_mc02_reg_load(state->rcc_regs, RCC_D2CFGR, sizeof(uint32_t)));
}

uint64_t dm_mc02_pwr_rcc_apb1_timer_clock_hz(const DmMc02PwrRcc *state)
{
    uint64_t hclk_hz = dm_mc02_pwr_rcc_hclk_hz(state);

    if (!hclk_hz) {
        return 0;
    }
    return dm_stm32h7_apb1_timer_clock_hz(
        hclk_hz,
        dm_mc02_reg_load(state->rcc_regs, RCC_D2CFGR, sizeof(uint32_t)),
        dm_mc02_reg_load(state->rcc_regs, RCC_CFGR, sizeof(uint32_t)));
}

uint64_t dm_mc02_pwr_rcc_apb2_timer_clock_hz(const DmMc02PwrRcc *state)
{
    uint64_t hclk_hz = dm_mc02_pwr_rcc_hclk_hz(state);

    if (!hclk_hz) {
        return 0;
    }
    return dm_stm32h7_apb2_timer_clock_hz(
        hclk_hz,
        dm_mc02_reg_load(state->rcc_regs, RCC_D2CFGR, sizeof(uint32_t)),
        dm_mc02_reg_load(state->rcc_regs, RCC_CFGR, sizeof(uint32_t)));
}

static uint64_t dm_mc02_pll_output_hz(const DmMc02PwrRcc *state,
                                      uint32_t on_bit, unsigned m_shift,
                                      hwaddr divr_offset, hwaddr fracr_offset,
                                      uint32_t frac_enable, unsigned out_shift,
                                      uint32_t output_enable)
{
    uint32_t cr;
    uint32_t pllckselr;
    uint32_t pll2divr;
    uint32_t pllcfgr;
    uint32_t pll2fracr;
    uint64_t source_hz;
    uint64_t divm;
    uint64_t divn;
    uint64_t divp;
    uint64_t fracn = 0;

    if (!state) {
        return 0;
    }
    cr = dm_mc02_reg_load(state->rcc_regs, RCC_CR, sizeof(uint32_t));
    if (!(cr & on_bit) || !dm_mc02_rcc_pll_input_ready(state)) {
        return 0;
    }
    pllckselr = dm_mc02_reg_load(state->rcc_regs, RCC_PLLCKSELR,
                                  sizeof(uint32_t));
    pll2divr = dm_mc02_reg_load(state->rcc_regs, divr_offset,
                                sizeof(uint32_t));
    pllcfgr = dm_mc02_reg_load(state->rcc_regs, RCC_PLLCFGR,
                               sizeof(uint32_t));
    if (!(pllcfgr & output_enable)) {
        return 0;
    }
    pll2fracr = dm_mc02_reg_load(state->rcc_regs, fracr_offset,
                                 sizeof(uint32_t));
    divm = (pllckselr >> m_shift) & 0x3fu;
    divn = (pll2divr & 0x1ffu) + 1u;
    divp = ((pll2divr >> out_shift) & 0x7fu) + 1u;
    if (!divm || !divp) {
        return 0;
    }

    switch (pllckselr & 3u) {
    case 0:
        source_hz = DM_MC02_HSI_HZ / dm_mc02_hsi_divider(cr);
        break;
    case 1:
        source_hz = DM_MC02_CSI_HZ;
        break;
    case 2:
        source_hz = DM_MC02_HSE_HZ;
        break;
    default:
        return 0;
    }
    if (pllcfgr & frac_enable) {
        fracn = (pll2fracr >> 3) & 0x1fffu;
    }

    /* PLL output = source / M * (N + FRACN / 8192) / P/R. */
    return muldiv64(source_hz * (divn * 8192u + fracn), 1,
                    divm * divp * 8192u);
}

uint64_t dm_mc02_pwr_rcc_adc_kernel_clock_hz(const DmMc02PwrRcc *state)
{
    uint32_t d3ccipr;
    unsigned source;

    if (!state || !state->adc_clock_configured) {
        return DM_MC02_ADC_DEFAULT_KERNEL_HZ;
    }
    d3ccipr = dm_mc02_reg_load(state->rcc_regs, RCC_D3CCIPR,
                               sizeof(uint32_t));
    source = (d3ccipr >> 16) & 3u;
    switch (source) {
    case 0: /* PLL2P */
        return dm_mc02_pll_output_hz(state, RCC_CR_PLL2ON, 12,
                                      RCC_PLL2DIVR, RCC_PLL2FRACR,
                                      RCC_PLLCFGR_PLL2FRACEN, 9,
                                      RCC_PLLCFGR_PLL2DIVPEN);
    case 1: /* PLL3R */
        return dm_mc02_pll_output_hz(state, RCC_CR_PLL3ON, 20,
                                      RCC_PLL3DIVR, RCC_PLL3FRACR,
                                      RCC_PLLCFGR_PLL3FRACEN, 24,
                                      RCC_PLLCFGR_PLL3DIVREN);
    case 2: { /* CLKP, selected by D1CCIPR.CKPERSEL */
        uint32_t d1ccipr = dm_mc02_reg_load(state->rcc_regs, RCC_D1CCIPR,
                                            sizeof(uint32_t));
        switch ((d1ccipr >> 28) & 3u) {
        case 0:
            if (!dm_mc02_rcc_oscillator_ready(state, 0)) {
                return 0;
            }
            return DM_MC02_HSI_HZ / dm_mc02_hsi_divider(
                dm_mc02_reg_load(state->rcc_regs, RCC_CR, sizeof(uint32_t)));
        case 1:
            if (!dm_mc02_rcc_oscillator_ready(state, 1)) {
                return 0;
            }
            return DM_MC02_CSI_HZ;
        case 2:
            if (!dm_mc02_rcc_oscillator_ready(state, 2)) {
                return 0;
            }
            return DM_MC02_HSE_HZ;
        default:
            return 0;
        }
    }
    default:
        return 0;
    }
}

uint64_t dm_mc02_pwr_rcc_fdcan_kernel_clock_hz(const DmMc02PwrRcc *state)
{
    uint32_t d2ccip1r;
    unsigned source;

    if (!state) {
        return 0;
    }
    d2ccip1r = dm_mc02_reg_load(state->rcc_regs, RCC_D2CCIP1R,
                                sizeof(uint32_t));
    source = (d2ccip1r >> 28) & 3u;
    switch (source) {
    case 0: /* HSE */
        return dm_mc02_rcc_oscillator_ready(state, 2) ? DM_MC02_HSE_HZ : 0;
    case 1: /* PLL1Q */
        return dm_mc02_pll_output_hz(state, RCC_CR_PLL1ON, 4,
                                     RCC_PLL1DIVR, RCC_PLL1FRACR,
                                     RCC_PLLCFGR_PLL1FRACEN, 16,
                                     RCC_PLLCFGR_PLL1DIVQEN);
    case 2: /* PLL2Q */
        return dm_mc02_pll_output_hz(state, RCC_CR_PLL2ON, 12,
                                     RCC_PLL2DIVR, RCC_PLL2FRACR,
                                     RCC_PLLCFGR_PLL2FRACEN, 16,
                                     RCC_PLLCFGR_PLL2DIVQEN);
    default:
        return 0;
    }
}

static uint64_t dm_mc02_rcc_usart_kernel_clock_hz(
    const DmMc02PwrRcc *state, unsigned mux_shift, uint64_t apb_hz)
{
    uint32_t d2ccip2r;
    uint32_t cr;
    unsigned source;

    if (!state) {
        return 0;
    }
    d2ccip2r = dm_mc02_reg_load(state->rcc_regs, RCC_D2CCIP2R,
                                sizeof(uint32_t));
    source = (d2ccip2r >> mux_shift) & 7u;
    switch (source) {
    case 0: /* APB clock */
        return apb_hz;
    case 1: /* PLL2Q */
        return dm_mc02_pll_output_hz(state, RCC_CR_PLL2ON, 12,
                                      RCC_PLL2DIVR, RCC_PLL2FRACR,
                                      RCC_PLLCFGR_PLL2FRACEN, 16,
                                      RCC_PLLCFGR_PLL2DIVQEN);
    case 2: /* PLL3Q */
        return dm_mc02_pll_output_hz(state, RCC_CR_PLL3ON, 20,
                                      RCC_PLL3DIVR, RCC_PLL3FRACR,
                                      RCC_PLLCFGR_PLL3FRACEN, 16,
                                      RCC_PLLCFGR_PLL3DIVQEN);
    case 3: /* HSI */
        cr = dm_mc02_reg_load(state->rcc_regs, RCC_CR, sizeof(uint32_t));
        return dm_mc02_rcc_oscillator_ready(state, 0) ?
               DM_MC02_HSI_HZ / dm_mc02_hsi_divider(cr) : 0;
    case 4: /* CSI */
        return dm_mc02_rcc_oscillator_ready(state, 1) ? DM_MC02_CSI_HZ : 0;
    case 5: /* LSE */
        return dm_mc02_rcc_lse_ready(state) ? DM_MC02_LSE_HZ : 0;
    default:
        return 0;
    }
}

uint64_t dm_mc02_pwr_rcc_usart16_kernel_clock_hz(
    const DmMc02PwrRcc *state)
{
    return dm_mc02_rcc_usart_kernel_clock_hz(
        state, 3, dm_mc02_pwr_rcc_apb2_clock_hz(state));
}

uint64_t dm_mc02_pwr_rcc_usart234578_kernel_clock_hz(
    const DmMc02PwrRcc *state)
{
    return dm_mc02_rcc_usart_kernel_clock_hz(
        state, 0, dm_mc02_pwr_rcc_apb1_clock_hz(state));
}

uint64_t dm_mc02_pwr_rcc_adc_clock_hz(const DmMc02PwrRcc *state)
{
    uint64_t kernel_hz = dm_mc02_pwr_rcc_adc_kernel_clock_hz(state);

    /* Before PLL2 setup, retain the old post-/64 compatibility rate. */
    if (!state || !state->adc_clock_configured) {
        return kernel_hz / DM_MC02_ADC_PRESCALER;
    }
    return kernel_hz ? kernel_hz / DM_MC02_ADC_PRESCALER : 0;
}

static void dm_mc02_pwr_rcc_notify_clock(DmMc02PwrRcc *state)
{
    if (state->clock_changed) {
        state->clock_changed(state->clock_changed_opaque,
                              dm_mc02_pwr_rcc_cpu_clock_hz(state));
    }
}

void dm_mc02_pwr_rcc_sync_runtime(DmMc02PwrRcc *state)
{
    if (state) {
        dm_mc02_pwr_rcc_notify_clock(state);
    }
}

static uint32_t dm_mc02_pwr_value(DmMc02PwrRccRegBlock *block,
                                   hwaddr offset)
{
    DmMc02PwrRcc *s = block->state;
    uint32_t value = dm_mc02_reg_load(s->pwr_regs, offset, sizeof(uint32_t));

    if (offset == PWR_CSR1) {
        /* ACTVOS tracks the selected D3 voltage range in this model. */
        uint32_t vos = dm_mc02_reg_load(s->pwr_regs, PWR_D3CR,
                                         sizeof(uint32_t)) & PWR_D3CR_VOS;
        value = (value & ~(PWR_CSR1_ACTVOSRDY | PWR_CSR1_ACTVOS)) |
                PWR_CSR1_ACTVOSRDY | (vos << 0);
    } else if (offset == PWR_D3CR) {
        value |= PWR_D3CR_VOSRDY;
    } else if (offset == PWR_CR3) {
        /* H723 has the LDO supply path; report it ready from reset. */
        value |= PWR_CR3_USB33RDY;
    }
    return value;
}

static uint32_t dm_mc02_rcc_value(DmMc02PwrRccRegBlock *block,
                                   hwaddr offset)
{
    DmMc02PwrRcc *s = block->state;
    uint32_t value = dm_mc02_reg_load(s->rcc_regs, offset, sizeof(uint32_t));

    if (offset == RCC_RSR) {
        /* RMVF is a write-one-to-clear control bit and never reads back as a
         * latched reset source. */
        value &= ~DM_MC02_RCC_RSR_RMVF;
    } else if (offset == RCC_CR) {
        /* Ready flags follow enabled and valid sources and settle
         * immediately.  PLLs additionally require a valid input/divider
         * configuration. */
        value &= ~RCC_CR_READY;
        if (value & RCC_CR_HSION) {
            value |= RCC_CR_HSIRDY;
        }
        if (value & RCC_CR_CSION) {
            value |= RCC_CR_CSIRDY;
        }
        if (value & RCC_CR_HSI48ON) {
            value |= RCC_CR_HSI48RDY;
        }
        if (value & RCC_CR_HSEON) {
            value |= RCC_CR_HSERDY;
        }
        if ((value & RCC_CR_PLL1ON) &&
            dm_mc02_pll_output_hz(s, RCC_CR_PLL1ON, 4, RCC_PLL1DIVR,
                                  RCC_PLL1FRACR, RCC_PLLCFGR_PLL1FRACEN,
                                  9, RCC_PLLCFGR_PLL1DIVPEN)) {
            value |= RCC_CR_PLL1RDY;
        }
        if ((value & RCC_CR_PLL2ON) &&
            dm_mc02_pll_output_hz(s, RCC_CR_PLL2ON, 12, RCC_PLL2DIVR,
                                  RCC_PLL2FRACR, RCC_PLLCFGR_PLL2FRACEN,
                                  9, RCC_PLLCFGR_PLL2DIVPEN)) {
            value |= RCC_CR_PLL2RDY;
        }
        if ((value & RCC_CR_PLL3ON) &&
            dm_mc02_pll_output_hz(s, RCC_CR_PLL3ON, 20, RCC_PLL3DIVR,
                                  RCC_PLL3FRACR, RCC_PLLCFGR_PLL3FRACEN,
                                  24, RCC_PLLCFGR_PLL3DIVREN)) {
            value |= RCC_CR_PLL3RDY;
        }
    } else if (offset == RCC_CFGR) {
        /* SWS reports the source currently feeding the system clock. */
        value = (value & ~(7u << 3)) |
                ((uint32_t)s->system_clock_source << 3);
    } else if (offset == RCC_BDCR) {
        value &= ~RCC_BDCR_LSERDY;
        if (value & RCC_BDCR_LSEON) {
            value |= RCC_BDCR_LSERDY;
        }
    } else if (offset == RCC_CSR) {
        value &= ~RCC_CSR_LSIRDY;
        if (value & RCC_CSR_LSION) {
            value |= RCC_CSR_LSIRDY;
        }
    }
    return value;
}

static uint64_t dm_mc02_pwr_rcc_read(void *opaque, hwaddr offset,
                                     unsigned size)
{
    DmMc02PwrRccRegBlock *block = opaque;
    DmMc02PwrRcc *s = block->state;
    const uint8_t *regs = block->pwr ? s->pwr_regs : s->rcc_regs;

    if (size > sizeof(uint32_t) || offset >= DM_MC02_PWR_RCC_REGION_SIZE ||
        size > DM_MC02_PWR_RCC_REGION_SIZE - offset) {
        return 0;
    }
    if (size == sizeof(uint32_t) && block->pwr &&
        (offset == PWR_CSR1 || offset == PWR_D3CR || offset == PWR_CR3)) {
        return dm_mc02_pwr_value(block, offset);
    }
    if (size == sizeof(uint32_t) && !block->pwr &&
        (offset == RCC_CR || offset == RCC_CFGR || offset == RCC_BDCR ||
         offset == RCC_CSR || offset == RCC_RSR)) {
        return dm_mc02_rcc_value(block, offset);
    }
    return dm_mc02_reg_load(regs, offset, size);
}

static void dm_mc02_pwr_rcc_write(void *opaque, hwaddr offset, uint64_t value,
                                  unsigned size)
{
    DmMc02PwrRccRegBlock *block = opaque;
    DmMc02PwrRcc *s = block->state;
    uint8_t *regs = block->pwr ? s->pwr_regs : s->rcc_regs;
    bool cpu_clock_dirty = false;
    bool adc_clock_dirty = false;
    bool fdcan_clock_dirty = false;
    bool uart_clock_dirty = false;
    bool adc_source_selected;
    bool pll2_enabled;

    if (size > sizeof(uint32_t) || offset >= DM_MC02_PWR_RCC_REGION_SIZE ||
        size > DM_MC02_PWR_RCC_REGION_SIZE - offset) {
        return;
    }
    if (!block->pwr && size == sizeof(uint32_t) && offset == RCC_CR) {
        uint32_t old = dm_mc02_reg_load(regs, offset, sizeof(uint32_t));
        uint32_t next = (old & ~RCC_CR_WRITABLE) |
                        ((uint32_t)value & RCC_CR_WRITABLE);
        dm_mc02_reg_store(regs, offset, next, sizeof(uint32_t));
        if (next & RCC_CR_PLL2ON) {
            s->adc_clock_configured = true;
        }
        dm_mc02_rcc_try_switch_system_clock(s);
        dm_mc02_pwr_rcc_notify_clock(s);
        return;
    }
    if (!block->pwr && size == sizeof(uint32_t) && offset == RCC_CFGR) {
        uint32_t old = dm_mc02_reg_load(regs, offset, sizeof(uint32_t));
        dm_mc02_reg_store(regs, offset, (old & (7u << 3)) |
                          ((uint32_t)value & ~(7u << 3)),
                          sizeof(uint32_t));
        dm_mc02_rcc_try_switch_system_clock(s);
        dm_mc02_pwr_rcc_notify_clock(s);
        return;
    }
    if (!block->pwr && offset >= RCC_RSR &&
        offset < RCC_RSR + sizeof(uint32_t)) {
        uint32_t write_mask;
        uint32_t write_value;
        uint32_t reset_flags;

        /* Handle aligned and sub-word writes without treating the status
         * register as ordinary storage.  Only a write of RMVF clears the
         * currently modelled reset sources; source bits are read-only. */
        write_mask = size == sizeof(uint32_t) ? UINT32_MAX :
                     (1u << (size * 8)) - 1u;
        write_value = ((uint32_t)value & write_mask) <<
                      ((offset - RCC_RSR) * 8);
        if (write_value & DM_MC02_RCC_RSR_RMVF) {
            reset_flags = dm_mc02_reg_load(regs, RCC_RSR,
                                           sizeof(uint32_t));
            reset_flags &= ~DM_MC02_RCC_RSR_RESET_FLAGS;
            dm_mc02_reg_store(regs, RCC_RSR, reset_flags,
                              sizeof(uint32_t));
        }
        return;
    }
    /* Unknown offsets and ordinary control registers are retained safely. */
    dm_mc02_reg_store(regs, offset, value, size);
    if (!block->pwr) {
        /* Most RCC writes are enable/status bookkeeping and must not cause
         * a live timer phase update or clock propagation.  Recompute only
         * when a register that can affect one of the modeled clock roots is
         * touched. */
        cpu_clock_dirty =
            (offset < RCC_CR + 4 && offset + size > RCC_CR) ||
            (offset < RCC_CFGR + 4 && offset + size > RCC_CFGR) ||
            (offset < RCC_D1CFGR + 4 && offset + size > RCC_D1CFGR) ||
            (offset < RCC_D2CFGR + 4 && offset + size > RCC_D2CFGR) ||
            (offset < RCC_PLLCKSELR + 4 && offset + size > RCC_PLLCKSELR) ||
            (offset < RCC_PLLCFGR + 4 && offset + size > RCC_PLLCFGR) ||
            (offset < RCC_PLL1DIVR + 4 && offset + size > RCC_PLL1DIVR) ||
            (offset < RCC_PLL1FRACR + 4 && offset + size > RCC_PLL1FRACR);
        adc_clock_dirty =
            (offset < RCC_CR + 4 && offset + size > RCC_CR) ||
            (offset < RCC_D1CFGR + 4 && offset + size > RCC_D1CFGR) ||
            (offset < RCC_PLLCKSELR + 4 && offset + size > RCC_PLLCKSELR) ||
            (offset < RCC_PLLCFGR + 4 && offset + size > RCC_PLLCFGR) ||
            (offset < RCC_PLL2DIVR + 4 && offset + size > RCC_PLL2DIVR) ||
            (offset < RCC_PLL2FRACR + 4 && offset + size > RCC_PLL2FRACR) ||
            (offset < RCC_PLL3DIVR + 4 && offset + size > RCC_PLL3DIVR) ||
            (offset < RCC_PLL3FRACR + 4 && offset + size > RCC_PLL3FRACR) ||
            (offset < RCC_D1CCIPR + 4 && offset + size > RCC_D1CCIPR) ||
            (offset < RCC_D3CCIPR + 4 && offset + size > RCC_D3CCIPR);
        fdcan_clock_dirty =
            (offset < RCC_CR + 4 && offset + size > RCC_CR) ||
            (offset < RCC_PLLCKSELR + 4 && offset + size > RCC_PLLCKSELR) ||
            (offset < RCC_PLLCFGR + 4 && offset + size > RCC_PLLCFGR) ||
            (offset < RCC_PLL1DIVR + 4 && offset + size > RCC_PLL1DIVR) ||
            (offset < RCC_PLL1FRACR + 4 && offset + size > RCC_PLL1FRACR) ||
            (offset < RCC_PLL2DIVR + 4 && offset + size > RCC_PLL2DIVR) ||
            (offset < RCC_PLL2FRACR + 4 && offset + size > RCC_PLL2FRACR) ||
            (offset < RCC_D2CCIP1R + 4 && offset + size > RCC_D2CCIP1R);
        uart_clock_dirty =
            (offset < RCC_CR + 4 && offset + size > RCC_CR) ||
            (offset < RCC_D1CFGR + 4 && offset + size > RCC_D1CFGR) ||
            (offset < RCC_D2CFGR + 4 && offset + size > RCC_D2CFGR) ||
            (offset < RCC_PLLCKSELR + 4 && offset + size > RCC_PLLCKSELR) ||
            (offset < RCC_PLLCFGR + 4 && offset + size > RCC_PLLCFGR) ||
            (offset < RCC_PLL2DIVR + 4 && offset + size > RCC_PLL2DIVR) ||
            (offset < RCC_PLL2FRACR + 4 && offset + size > RCC_PLL2FRACR) ||
            (offset < RCC_PLL3DIVR + 4 && offset + size > RCC_PLL3DIVR) ||
            (offset < RCC_PLL3FRACR + 4 && offset + size > RCC_PLL3FRACR) ||
            (offset < RCC_D2CCIP2R + 4 && offset + size > RCC_D2CCIP2R) ||
            (offset < RCC_BDCR + 4 && offset + size > RCC_BDCR);
        /* PLL1 setup is commonly performed before the independent ADC clock
         * is configured.  It must not turn the compatibility default into a
         * zero-rate PLL2P clock merely because PLLCKSELR/PLL1DIVR changed.
         * The ADC source becomes explicit when the source mux is written, or
         * when the default PLL2P source is actually enabled. */
        adc_source_selected =
            (offset < RCC_D3CCIPR + 4 && offset + size > RCC_D3CCIPR);
        pll2_enabled =
            (dm_mc02_reg_load(regs, RCC_CR, sizeof(uint32_t)) &
             RCC_CR_PLL2ON) != 0;

        if (adc_source_selected || pll2_enabled) {
            s->adc_clock_configured = true;
        }
        dm_mc02_rcc_try_switch_system_clock(s);
        if (cpu_clock_dirty || adc_clock_dirty || fdcan_clock_dirty ||
            uart_clock_dirty) {
            dm_mc02_pwr_rcc_notify_clock(s);
        }
    }
}

static const MemoryRegionOps dm_mc02_pwr_rcc_ops = {
    .read = dm_mc02_pwr_rcc_read,
    .write = dm_mc02_pwr_rcc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_pwr_rcc_init(DmMc02PwrRcc *state, Object *owner)
{
    memset(state, 0, sizeof(*state));

    /* Reset state: LDO supply selected, VOS ready, HSI running. */
    dm_mc02_reg_store(state->pwr_regs, PWR_CR3, PWR_CR3_LDOEN,
                      sizeof(uint32_t));
    dm_mc02_reg_store(state->pwr_regs, PWR_D3CR, 0,
                      sizeof(uint32_t));
    dm_mc02_reg_store(state->rcc_regs, RCC_CR, RCC_CR_HSION,
                      sizeof(uint32_t));
    state->system_clock_source = 0;

    state->pwr_block.state = state;
    state->pwr_block.pwr = true;
    state->rcc_block.state = state;
    state->rcc_block.pwr = false;

    memory_region_init_io(&state->pwr, owner, &dm_mc02_pwr_rcc_ops,
                          &state->pwr_block, "dm-mc02.pwr",
                          DM_MC02_PWR_RCC_REGION_SIZE);
    memory_region_init_io(&state->rcc, owner, &dm_mc02_pwr_rcc_ops,
                          &state->rcc_block, "dm-mc02.rcc",
                          DM_MC02_PWR_RCC_REGION_SIZE);
}

void dm_mc02_pwr_rcc_set_clock_callback(DmMc02PwrRcc *state,
                                        DmMc02PwrRccClockChanged *callback,
                                        void *opaque)
{
    state->clock_changed = callback;
    state->clock_changed_opaque = opaque;
    dm_mc02_pwr_rcc_notify_clock(state);
}

void dm_mc02_pwr_rcc_reset(DmMc02PwrRcc *s)
{
    DmMc02PwrRccClockChanged *callback;
    void *opaque;
    uint32_t reset_flags;

    if (!s) {
        return;
    }
    callback = s->clock_changed;
    opaque = s->clock_changed_opaque;
    reset_flags = dm_mc02_reg_load(s->rcc_regs, RCC_RSR,
                                   sizeof(uint32_t)) &
                  DM_MC02_RCC_RSR_RESET_FLAGS;
    memset(s->pwr_regs, 0, sizeof(s->pwr_regs));
    memset(s->rcc_regs, 0, sizeof(s->rcc_regs));
    s->adc_clock_configured = false;
    s->system_clock_source = 0;
    dm_mc02_reg_store(s->pwr_regs, PWR_CR3, PWR_CR3_LDOEN, sizeof(uint32_t));
    dm_mc02_reg_store(s->rcc_regs, RCC_CR, RCC_CR_HSION, sizeof(uint32_t));
    s->clock_changed = callback;
    s->clock_changed_opaque = opaque;
    dm_mc02_reg_store(s->rcc_regs, RCC_RSR, reset_flags,
                      sizeof(uint32_t));
    dm_mc02_pwr_rcc_notify_clock(s);
}

void dm_mc02_pwr_rcc_note_iwdg_reset(void *opaque)
{
    DmMc02PwrRcc *s = opaque;
    uint32_t reset_status;

    if (!s) {
        return;
    }
    reset_status = dm_mc02_reg_load(s->rcc_regs, RCC_RSR,
                                    sizeof(uint32_t));
    reset_status |= DM_MC02_RCC_RSR_IWDG1RSTF;
    dm_mc02_reg_store(s->rcc_regs, RCC_RSR, reset_status,
                      sizeof(uint32_t));
}

void dm_mc02_pwr_rcc_note_wwdg_reset(void *opaque)
{
    DmMc02PwrRcc *s = opaque;
    uint32_t reset_status;

    if (!s) {
        return;
    }
    reset_status = dm_mc02_reg_load(s->rcc_regs, RCC_RSR,
                                    sizeof(uint32_t));
    reset_status |= DM_MC02_RCC_RSR_WWDG1RSTF;
    dm_mc02_reg_store(s->rcc_regs, RCC_RSR, reset_status,
                      sizeof(uint32_t));
}

void dm_mc02_pwr_rcc_note_power_on_reset(DmMc02PwrRcc *s)
{
    uint32_t reset_status;

    if (!s) {
        return;
    }
    reset_status = dm_mc02_reg_load(s->rcc_regs, RCC_RSR,
                                    sizeof(uint32_t));
    reset_status |= DM_MC02_RCC_RSR_PORRSTF;
    dm_mc02_reg_store(s->rcc_regs, RCC_RSR, reset_status,
                      sizeof(uint32_t));
}

void dm_mc02_pwr_rcc_note_brownout_reset(DmMc02PwrRcc *s)
{
    uint32_t reset_status;

    if (!s) {
        return;
    }
    reset_status = dm_mc02_reg_load(s->rcc_regs, RCC_RSR,
                                    sizeof(uint32_t));
    reset_status |= DM_MC02_RCC_RSR_BORRSTF;
    dm_mc02_reg_store(s->rcc_regs, RCC_RSR, reset_status,
                      sizeof(uint32_t));
}

void dm_mc02_pwr_rcc_note_pin_reset(void *opaque)
{
    DmMc02PwrRcc *s = opaque;
    uint32_t reset_status;

    if (!s) {
        return;
    }
    reset_status = dm_mc02_reg_load(s->rcc_regs, RCC_RSR,
                                    sizeof(uint32_t));
    reset_status |= DM_MC02_RCC_RSR_PINRSTF;
    dm_mc02_reg_store(s->rcc_regs, RCC_RSR, reset_status,
                      sizeof(uint32_t));
}

void dm_mc02_pwr_rcc_note_software_reset(void *opaque, int line, int level)
{
    DmMc02PwrRcc *s = opaque;
    uint32_t reset_status;

    (void)line;

    /* ARMv7M signals SYSRESETREQ with qemu_irq_pulse().  Ignore the
     * deassertion so one request produces one latched source event. */
    if (!s || !level) {
        return;
    }
    reset_status = dm_mc02_reg_load(s->rcc_regs, RCC_RSR,
                                    sizeof(uint32_t));
    reset_status |= DM_MC02_RCC_RSR_SFTRSTF;
    dm_mc02_reg_store(s->rcc_regs, RCC_RSR, reset_status,
                      sizeof(uint32_t));
}
