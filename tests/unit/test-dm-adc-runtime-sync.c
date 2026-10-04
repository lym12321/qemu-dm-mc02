/* Runtime-sync regression for the board-independent ADC component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc.h"
#include "migration/migration.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "qapi/error.h"
#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

#define ADC_ISR 0x00
#define ADC_IER 0x04
#define ADC_CR  0x08
#define ADC_CFGR 0x0c
#define ADC_SMPR1 0x14
#define ADC_SQR1 0x30
#define ADC_DR 0x40
#define ADC_JSQR 0x4c
#define ADC_JDR1 0x80
#define ADC_CALFACT_RES13 0xc4
#define ADC_ISR_EOC (1u << 2)
#define ADC_ISR_JEOC (1u << 5)
#define ADC_CR_ADEN (1u << 0)
#define ADC_CR_ADSTART (1u << 2)
#define ADC_CR_JADSTART (1u << 3)
#define ADC_CR_ADSTP (1u << 4)
#define ADC_CR_JADSTP (1u << 5)
#define ADC_CR_ADCAL (1u << 31)
#define ADC_CFGR_RES_SHIFT 2
#define ADC_CFGR_RES_MASK (7u << ADC_CFGR_RES_SHIFT)
#define ADC_CFGR_EXTEN (1u << 10)
#define ADC_CFGR_CONT (1u << 13)
#define ADC_CFGR_JAUTO (1u << 25)
#define ADC_CFGR_JQDIS (1u << 31)
#define ADC_JSQR_JEXTEN (1u << 7)

/* Independent register/source evidence: ST stm32h723xx.h defines RES_Pos=2
 * and RES_Msk=0x1c.  ST stm32h7xx_ll_adc.h LL_ADC_RESOLUTION_{16,14,12,10}B
 * defines codes 000..011, and LL_ADC_SetResolution requires both conversion
 * groups idle.  Codes 100..111 are unsupported by this model, not a claim
 * that they are all reserved on H723 silicon. */

extern unsigned dm_adc_test_dma_requests;

typedef struct AdcObserver {
    unsigned starts;
    unsigned triggers;
    unsigned samples;
    uint16_t sample_value;
    unsigned irq_calls;
    int irq_level;
} AdcObserver;

static int temp_fd;
static int64_t adc_clock_ns;

int64_t cpu_get_clock(void);

int64_t cpu_get_clock(void)
{
    return adc_clock_ns;
}

static void put_reg(uint8_t *regs, unsigned offset, uint32_t value)
{
    for (unsigned byte = 0; byte < sizeof(value); ++byte) {
        regs[offset + byte] = value >> (byte * 8);
    }
}

static uint32_t get_reg(const uint8_t *regs, unsigned offset)
{
    uint32_t value = 0;

    for (unsigned byte = 0; byte < sizeof(value); ++byte) {
        value |= (uint32_t)regs[offset + byte] << (byte * 8);
    }
    return value;
}

static void mmio_write(DmMc02Adc *state, unsigned offset, uint32_t value,
                       unsigned size)
{
    g_assert_nonnull(state->iomem.ops);
    state->iomem.ops->write(state->iomem.opaque, offset, value, size);
}

static uint32_t mmio_read(DmMc02Adc *state, unsigned offset, unsigned size)
{
    g_assert_nonnull(state->iomem.ops);
    return state->iomem.ops->read(state->iomem.opaque, offset, size);
}

static bool observe_start(void *opaque, unsigned adc_index)
{
    AdcObserver *observer = opaque;

    g_assert_cmpuint(adc_index, ==, 0);
    observer->starts++;
    return true;
}

static bool observe_trigger(void *opaque, unsigned adc_index,
                            uint32_t source, bool rising,
                            unsigned event_count, uint64_t timestamp_ns,
                            uint64_t *conversion_id)
{
    AdcObserver *observer = opaque;

    (void)source;
    (void)rising;
    (void)event_count;
    (void)timestamp_ns;
    g_assert_cmpuint(adc_index, ==, 0);
    observer->triggers++;
    *conversion_id = 1;
    return true;
}

static void observe_sample(void *opaque, unsigned adc_index,
                           const DmMc02AdcRegularSample *sample)
{
    AdcObserver *observer = opaque;

    g_assert_cmpuint(adc_index, ==, 0);
    observer->samples++;
    observer->sample_value = sample->value;
}

static void observe_irq(void *opaque, int line, int level)
{
    AdcObserver *observer = opaque;

    (void)line;
    observer->irq_calls++;
    observer->irq_level = level;
}

static void init_mmio_adc(DmMc02Adc *state)
{
    adc_clock_ns = 0;
    dm_mc02_adc_init(state, NULL, "adc-resolution");
    dm_mc02_adc_set_clock_hz(state, 100000000);
    dm_mc02_adc_set_accurate_timing(state, true);
    mmio_write(state, ADC_SQR1, 0, 4); /* One regular rank, channel zero. */
    mmio_write(state, ADC_CFGR, ADC_CFGR_JQDIS, 4);
    mmio_write(state, ADC_JSQR, 0, 4); /* One injected rank, channel zero. */
    mmio_write(state, ADC_CR, ADC_CR_ADEN, 4);
}

static void attach_observer(DmMc02Adc *state, AdcObserver *observer,
                            qemu_irq irq, DmMc02Dma *dma,
                            DmMc02Dmamux *dmamux)
{
    dm_mc02_adc_set_regular_start_callback(state, 0, observe_start, observer);
    dm_mc02_adc_set_regular_trigger_callback(state, 0, observe_trigger,
                                             observer);
    dm_mc02_adc_set_regular_sample_callback(state, 0, observe_sample,
                                            observer);
    dm_mc02_adc_set_irq(state, irq);
    dm_mc02_adc_set_dma(state, dma, dmamux, 9, ADC_DR);
    dm_adc_test_dma_requests = 0;
}

static void assert_no_conversion(DmMc02Adc *state)
{
    g_assert_false(state->conversion_active);
    g_assert_false(state->injected_conversion_active);
    g_assert_false(timer_pending(state->sample_timer));
    g_assert_false(timer_pending(state->injected_timer));
    g_assert_cmpuint(state->next_sample_ns, ==, 0);
    g_assert_cmpuint(state->next_injected_sample_ns, ==, 0);
}

static QEMUFile *open_test_file(bool write)
{
    int fd = dup(temp_fd);
    QIOChannel *ioc;
    QEMUFile *file;

    g_assert_cmpint(fd, >=, 0);
    g_assert_cmpint(lseek(fd, 0, SEEK_SET), ==, 0);
    if (write) {
        g_assert_cmpint(ftruncate(fd, 0), ==, 0);
    }
    ioc = QIO_CHANNEL(qio_channel_file_new_fd(fd));
    file = write ? qemu_file_new_output(ioc) : qemu_file_new_input(ioc);
    object_unref(OBJECT(ioc));
    return file;
}

static void save_state(const DmMc02Adc *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_adc_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Adc *state)
{
    QEMUFile *file = open_test_file(false);
    const VMStateDescription *description = dm_mc02_adc_vmstate();
    int ret = vmstate_load_state(file, description, state,
                                 description->version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void free_adc(DmMc02Adc *state)
{
    timer_free(state->sample_timer);
    timer_free(state->injected_timer);
    timer_free(state->regulator_timer);
}

static void test_resolution_mmio_readback(void)
{
    static const unsigned sizes[] = { 1, 2, 4 };

    g_assert_false(dm_mc02_adc_resolution_supported(NULL));
    for (unsigned code = 0; code < 8; code++) {
        for (unsigned index = 0; index < ARRAY_SIZE(sizes); index++) {
            DmMc02Adc state;
            unsigned size = sizes[index];
            uint32_t value = (code << ADC_CFGR_RES_SHIFT) | 1u;
            uint32_t mask = size == 4 ? UINT32_MAX :
                            (UINT32_C(1) << (size * 8)) - 1;
            uint32_t old_cfgr;

            init_mmio_adc(&state);
            old_cfgr = mmio_read(&state, ADC_CFGR, 4);
            if (size >= 2) {
                value |= ADC_CFGR_CONT;
            }
            if (size == 4) {
                value |= ADC_CFGR_JQDIS;
            }
            mmio_write(&state, ADC_CFGR, value, size);
            g_assert_cmphex(mmio_read(&state, ADC_CFGR, 4), ==,
                            (old_cfgr & ~mask) | (value & mask));
            g_assert_cmphex(mmio_read(&state, ADC_CFGR, 1) &
                            ADC_CFGR_RES_MASK, ==,
                            code << ADC_CFGR_RES_SHIFT);
            g_assert_cmpint(dm_mc02_adc_resolution_supported(&state), ==,
                            code < 4);
            assert_no_conversion(&state);
            if (code >= 4) {
                uint32_t disabled_cr;

                dm_mc02_adc_stop(&state);
                disabled_cr = mmio_read(&state, ADC_CR, 4);
                g_assert_false(disabled_cr & ADC_CR_ADEN);
                dm_mc02_adc_start(&state);
                g_assert_cmphex(mmio_read(&state, ADC_CR, 4), ==, disabled_cr);
                mmio_write(&state, ADC_CR, ADC_CR_ADEN, size);
                g_assert_true(mmio_read(&state, ADC_CR, 4) & ADC_CR_ADEN);
                assert_no_conversion(&state);
            }
            free_adc(&state);
        }
    }
}

static void test_supported_resolution_deadlines(void)
{
    /* RM0468 Rev 3, 28.4.23 Table 228 gives TSAR=8.5/7.5/6.5/5.5
     * clocks; 28.4.13 gives SMP=1.5/32.5/810.5 clocks for codes 0/4/7.
     * These literal deadlines also exercise integer nanosecond rounding. */
    static const struct {
        uint64_t clock_hz;
        unsigned sampling_code;
        unsigned rank_ns[4];
    } cases[] = {
        { 24000000, 0, { 417, 375, 334, 292 } },
        { 24000000, 4, { 1709, 1667, 1625, 1584 } },
        { 1500000, 0, { 6667, 6000, 5334, 4667 } },
        { 1500000, 4, { 27334, 26667, 26000, 25334 } },
        { 1500000, 7, { 546000, 545334, 544667, 544000 } },
    };
    static const uint16_t results[] = { 0x0100, 0x0040, 0x0010, 0x0004 };

    for (unsigned test = 0; test < ARRAY_SIZE(cases); test++) {
        for (unsigned code = 0; code < 4; code++) {
            for (unsigned injected = 0; injected < 2; injected++) {
                DmMc02Adc state;
                DmMc02Dma dma = { 0 };
                DmMc02Dmamux dmamux = { 0 };
                AdcObserver observer = { 0 };
                qemu_irq irq = qemu_allocate_irq(observe_irq, &observer, 0);
                uint32_t flag = injected ? ADC_ISR_JEOC : ADC_ISR_EOC;
                uint32_t start = injected ? ADC_CR_JADSTART : ADC_CR_ADSTART;
                unsigned data_reg = injected ? ADC_JDR1 : ADC_DR;
                unsigned deadline = cases[test].rank_ns[code];
                QEMUTimer *timer;

                init_mmio_adc(&state);
                dm_mc02_adc_set_clock_hz(&state, cases[test].clock_hz);
                mmio_write(&state, ADC_SMPR1, cases[test].sampling_code, 4);
                mmio_write(&state, ADC_CFGR, ADC_CFGR_JQDIS | 1u |
                            (code << ADC_CFGR_RES_SHIFT), 4);
                put_reg(state.regs, data_reg, 0x2468);
                mmio_write(&state, ADC_IER, flag, 4);
                attach_observer(&state, &observer, irq, &dma, &dmamux);
                mmio_write(&state, ADC_CR, ADC_CR_ADEN | start, 1);
                timer = injected ? state.injected_timer : state.sample_timer;
                g_assert_true(timer_pending(timer));
                g_assert_cmpuint(injected ? state.next_injected_sample_ns :
                                 state.next_sample_ns, ==, deadline);
                adc_clock_ns = deadline - 1;
                qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL);
                g_assert_false(mmio_read(&state, ADC_ISR, 4) & flag);
                g_assert_cmphex(get_reg(state.regs, data_reg), ==, 0x2468);
                g_assert_cmpint(observer.irq_level, ==, 0);
                g_assert_cmpuint(observer.samples, ==, 0);
                g_assert_cmpuint(dm_adc_test_dma_requests, ==, 0);
                adc_clock_ns++;
                g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
                g_assert_true(mmio_read(&state, ADC_ISR, 4) & flag);
                g_assert_cmphex(get_reg(state.regs, data_reg), ==, results[code]);
                g_assert_cmpint(observer.irq_level, ==, 1);
                g_assert_cmpuint(observer.samples, ==, injected ? 0 : 1);
                g_assert_cmpuint(dm_adc_test_dma_requests, ==, injected ? 0 : 1);
                g_assert_false(mmio_read(&state, ADC_CR, 4) & start);
                g_assert_false(timer_pending(timer));
                free_adc(&state);
                qemu_free_irq(irq);
            }
        }
    }
}

static void test_resolution_results(void)
{
    /* Fixed ST LL __LL_ADC_CONVERT_DATA_RESOLUTION (V5_V90) and
     * __LL_ADC_DIGITAL_SCALE: canonical raw16 retains its high bits.
     * Calibration is the existing raw16 approximation, not silicon trim. */
    static const struct {
        unsigned code;
        uint16_t raw;
        uint16_t offset;
        uint16_t result;
    } cases[] = {
        { 3, 0x0000, 0, 0x0000 },
        { 0, 0xffff, 0, 0xffff },
        { 1, 0xffff, 0, 0x3fff },
        { 2, 0xffff, 0, 0x0fff },
        { 3, 0xffff, 0, 0x03ff },
        { 0, 0xabcd, 0, 0xabcd },
        { 1, 0xabcd, 0, 0x2af3 },
        { 2, 0xabcd, 0, 0x0abc },
        { 3, 0xabcd, 0, 0x02af },
        { 3, 0xabcd, 0x3d, 0x02ae },
    };

    for (unsigned i = 0; i < ARRAY_SIZE(cases); ++i) {
        for (unsigned injected = 0; injected < 2; ++injected) {
            DmMc02Adc state;
            AdcObserver observer = { 0 };
            unsigned data_reg = injected ? ADC_JDR1 : ADC_DR;
            uint32_t start = injected ? ADC_CR_JADSTART : ADC_CR_ADSTART;

            init_mmio_adc(&state);
            dm_mc02_adc_set_channel_raw(&state, 0, cases[i].raw);
            mmio_write(&state, ADC_CALFACT_RES13, cases[i].offset, 4);
            mmio_write(&state, ADC_CFGR, ADC_CFGR_JQDIS |
                        (cases[i].code << ADC_CFGR_RES_SHIFT), 4);
            dm_mc02_adc_set_regular_sample_callback(&state, 0,
                                                    observe_sample, &observer);
            mmio_write(&state, ADC_CR, ADC_CR_ADEN | start, 4);
            adc_clock_ns = 100; /* (1.5 + 8.5) clocks at 100 MHz, or less. */
            g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
            g_assert_cmphex(mmio_read(&state, data_reg, 4), ==, cases[i].result);
            g_assert_cmphex(state.external_values[0], ==, cases[i].raw);
            if (!injected) {
                g_assert_cmpuint(observer.samples, ==, 1);
                g_assert_cmphex(observer.sample_value, ==, cases[i].result);
            }
            free_adc(&state);
        }
    }
}

static void test_unsupported_resolution_starts_preserve_results(void)
{
    static const unsigned sizes[] = { 1, 2, 4 };

    for (unsigned code = 4; code < 8; code++) {
        for (unsigned index = 0; index < ARRAY_SIZE(sizes); index++) {
            for (unsigned group = 0; group < 3; group++) {
                DmMc02Adc state;
                DmMc02Dma dma = { 0 };
                DmMc02Dmamux dmamux = { 0 };
                AdcObserver observer = { 0 };
                qemu_irq irq = qemu_allocate_irq(observe_irq, &observer, 0);
                uint8_t before[DM_MC02_ADC_REGION_SIZE];
                uint32_t start = group == 1 ? ADC_CR_JADSTART :
                                             ADC_CR_ADSTART;
                uint32_t cfgr = ADC_CFGR_JQDIS | 1u |
                                (code << ADC_CFGR_RES_SHIFT);
                unsigned irq_calls;

                init_mmio_adc(&state);
                if (group == 2) {
                    cfgr |= ADC_CFGR_JAUTO;
                }
                mmio_write(&state, ADC_CFGR, cfgr, 4);
                put_reg(state.regs, ADC_DR, 0x579b);
                put_reg(state.regs, ADC_JDR1, 0x2468);
                put_reg(state.regs, ADC_ISR, mmio_read(&state, ADC_ISR, 4) |
                         ADC_ISR_EOC | ADC_ISR_JEOC);
                mmio_write(&state, ADC_IER, ADC_ISR_EOC | ADC_ISR_JEOC, 4);
                attach_observer(&state, &observer, irq, &dma, &dmamux);
                irq_calls = observer.irq_calls;
                g_assert_cmpint(observer.irq_level, ==, 1);
                memcpy(before, state.regs, sizeof(before));

                mmio_write(&state, ADC_CR, ADC_CR_ADEN | start, sizes[index]);
                g_assert_cmpmem(state.regs, sizeof(state.regs),
                                before, sizeof(before));
                g_assert_false(dm_mc02_adc_start_regular_at(&state, 5, 50));
                dm_mc02_adc_start(&state);
                g_assert_cmpmem(state.regs, sizeof(state.regs),
                                before, sizeof(before));
                assert_no_conversion(&state);
                g_assert_cmpuint(state.regular_sequence_id, ==, 0);
                g_assert_cmpuint(state.regular_active_sequence_id, ==, 0);
                g_assert_cmpuint(observer.starts, ==, 0);
                g_assert_cmpuint(observer.triggers, ==, 0);
                g_assert_cmpuint(observer.samples, ==, 0);
                g_assert_cmpuint(observer.irq_calls, ==, irq_calls);
                g_assert_cmpuint(dm_adc_test_dma_requests, ==, 0);

                /* A later supported configuration can use the existing
                 * wiring; rejected starts do not leave a phantom busy bit. */
                mmio_write(&state, ADC_CFGR, ADC_CFGR_JQDIS, 4);
                mmio_write(&state, ADC_CR, ADC_CR_ADEN | start, 1);
                g_assert_true(timer_pending(group == 1 ? state.injected_timer :
                                                         state.sample_timer));
                g_assert_cmpuint(observer.starts, ==, group == 1 ? 0 : 1);
                g_assert_cmphex(get_reg(state.regs, ADC_DR), ==, 0x579b);
                g_assert_cmphex(get_reg(state.regs, ADC_JDR1), ==, 0x2468);
                free_adc(&state);
                qemu_free_irq(irq);
            }
        }
    }
}

static void test_unsupported_resolution_external_boundaries(void)
{
    for (unsigned code = 4; code < 8; code++) {
        DmMc02Adc state;
        DmMc02Dma dma = { 0 };
        DmMc02Dmamux dmamux = { 0 };
        AdcObserver observer = { 0 };
        qemu_irq irq = qemu_allocate_irq(observe_irq, &observer, 0);
        DmMc02TriggerEvent event = {
            .source_id = DM_MC02_TRIGGER_TIM2_TRGO,
            .rising = true,
            .event_count = 1,
            .timestamp_ns = 500,
        };
        uint8_t before[DM_MC02_ADC_REGION_SIZE];
        unsigned irq_calls;

        init_mmio_adc(&state);
        mmio_write(&state, ADC_CFGR, ADC_CFGR_JQDIS | 1u |
                   (code << ADC_CFGR_RES_SHIFT) | ADC_CFGR_EXTEN |
                   (DM_MC02_TRIGGER_TIM2_TRGO << 5), 4);
        mmio_write(&state, ADC_JSQR, ADC_JSQR_JEXTEN | (2u << 2), 4);
        mmio_write(&state, ADC_CR, ADC_CR_ADEN | ADC_CR_ADSTART |
                   ADC_CR_JADSTART, 4);
        g_assert_false(mmio_read(&state, ADC_CR, 4) &
                       (ADC_CR_ADSTART | ADC_CR_JADSTART));

        /* Exercise the public trigger guards themselves with an armed raw
         * fixture.  Normal VMState rejects this invalid combination; a
         * trigger must still not notify common or schedule a conversion. */
        put_reg(state.regs, ADC_CR, ADC_CR_ADEN | ADC_CR_ADSTART |
                 ADC_CR_JADSTART);
        put_reg(state.regs, ADC_DR, 0x579b);
        put_reg(state.regs, ADC_JDR1, 0x2468);
        put_reg(state.regs, ADC_ISR, mmio_read(&state, ADC_ISR, 4) |
                 ADC_ISR_EOC | ADC_ISR_JEOC);
        mmio_write(&state, ADC_IER, ADC_ISR_EOC | ADC_ISR_JEOC, 4);
        attach_observer(&state, &observer, irq, &dma, &dmamux);
        irq_calls = observer.irq_calls;
        memcpy(before, state.regs, sizeof(before));
        g_assert_false(dm_mc02_adc_external_trigger(
            &state, event.source_id, true));
        g_assert_false(dm_mc02_adc_external_trigger_with_id(
            &state, event.source_id, true, 1, 500, 5));
        g_assert_false(dm_mc02_adc_external_trigger_with_id_at(
            &state, event.source_id, true, 1, 500, 750, 5));
        dm_mc02_adc_trigger_sink(&state, &event);
        assert_no_conversion(&state);
        g_assert_cmpmem(state.regs, sizeof(state.regs), before, sizeof(before));
        g_assert_cmpuint(state.regular_sequence_id, ==, 0);
        g_assert_cmpuint(state.regular_active_sequence_id, ==, 0);
        g_assert_cmpuint(observer.starts, ==, 0);
        g_assert_cmpuint(observer.triggers, ==, 0);
        g_assert_cmpuint(observer.samples, ==, 0);
        g_assert_cmpuint(observer.irq_calls, ==, irq_calls);
        g_assert_cmpuint(dm_adc_test_dma_requests, ==, 0);
        free_adc(&state);
        qemu_free_irq(irq);
    }
}

static void test_resolution_change_protected_while_busy(void)
{
    static const unsigned sizes[] = { 1, 2, 4 };

    for (unsigned group = 0; group < 4; group++) {
        for (unsigned index = 0; index < ARRAY_SIZE(sizes); index++) {
            DmMc02Adc state;
            bool injected = group % 2;
            bool armed = group >= 2;
            unsigned size = sizes[index];
            uint32_t cfgr = ADC_CFGR_JQDIS | (1u << ADC_CFGR_RES_SHIFT);
            uint32_t start = injected ? ADC_CR_JADSTART : ADC_CR_ADSTART;
            uint32_t stop = injected ? ADC_CR_JADSTP : ADC_CR_ADSTP;
            uint32_t mask = size == 4 ? UINT32_MAX :
                            (UINT32_C(1) << (size * 8)) - 1;
            uint32_t candidate;
            uint32_t expected;
            bool context_valid;
            uint32_t context;
            uint64_t deadline;
            QEMUTimer *timer;

            init_mmio_adc(&state);
            if (armed && !injected) {
                cfgr |= ADC_CFGR_EXTEN | (DM_MC02_TRIGGER_TIM2_TRGO << 5);
            }
            mmio_write(&state, ADC_CFGR, cfgr, 4);
            if (armed && injected) {
                mmio_write(&state, ADC_JSQR, ADC_JSQR_JEXTEN | (2u << 2), 4);
            }
            mmio_write(&state, ADC_CR, ADC_CR_ADEN | start, 4);
            g_assert_true(mmio_read(&state, ADC_CR, 4) & start);
            timer = injected ? state.injected_timer : state.sample_timer;
            g_assert_cmpint(timer_pending(timer), ==, !armed);
            deadline = injected ? state.next_injected_sample_ns :
                                  state.next_sample_ns;
            context_valid = state.injected_active_context_valid;
            context = state.injected_active_jsqr;

            candidate = (cfgr & ~(ADC_CFGR_RES_MASK | ADC_CFGR_JQDIS)) |
                        (7u << ADC_CFGR_RES_SHIFT) | ADC_CFGR_CONT | 2u;
            expected = (cfgr & ~mask) | (candidate & mask);
            expected = (expected & ~ADC_CFGR_RES_MASK) |
                       (cfgr & ADC_CFGR_RES_MASK) | ADC_CFGR_JQDIS;
            mmio_write(&state, ADC_CFGR, candidate, size);
            g_assert_cmphex(mmio_read(&state, ADC_CFGR, 4), ==, expected);
            g_assert_true(dm_mc02_adc_resolution_supported(&state));
            g_assert_cmpint(timer_pending(timer), ==, !armed);
            g_assert_cmpuint(injected ? state.next_injected_sample_ns :
                             state.next_sample_ns, ==, deadline);
            g_assert_cmpint(state.injected_active_context_valid, ==,
                            context_valid);
            g_assert_cmphex(state.injected_active_jsqr, ==, context);

            /* Stop releases both RES and the existing independent JQDIS
             * protection; changing JQDIS while idle flushes the context. */
            mmio_write(&state, ADC_CR, ADC_CR_ADEN | stop, 4);
            assert_no_conversion(&state);
            mmio_write(&state, ADC_CFGR, candidate, 4);
            g_assert_cmphex(mmio_read(&state, ADC_CFGR, 4), ==, candidate);
            g_assert_false(dm_mc02_adc_resolution_supported(&state));
            g_assert_false(state.injected_active_context_valid);
            free_adc(&state);
        }
    }
}

static void test_interleaved_resolution_sentinel(void)
{
    /* ST LL_ADC_MULTI_TWOSMP_DELAY_1CYCLE_5 and SAMPLINGTIME_1CYCLE_5
     * encode zero; together at 100 MHz they advance the start by 30 ns. */
    for (unsigned code = 0; code < 8; code++) {
        DmMc02Adc state;

        init_mmio_adc(&state);
        mmio_write(&state, ADC_CFGR, ADC_CFGR_JQDIS |
                   (code << ADC_CFGR_RES_SHIFT), 4);
        g_assert_cmpuint(dm_mc02_adc_interleaved_slave_start_ns(&state, 500, 0),
                         ==, code < 4 ? 530 : UINT64_MAX);
        dm_mc02_adc_set_clock_hz(&state, 0);
        g_assert_cmpuint(dm_mc02_adc_interleaved_slave_start_ns(&state, 500, 0),
                         ==, UINT64_MAX);
        free_adc(&state);
    }
}

static void test_rearms_paused_regular_rank_after_load(void)
{
    DmMc02Adc source;
    DmMc02Adc restored;
    int64_t deadline;

    adc_clock_ns = 0;
    dm_mc02_adc_init(&source, NULL, "adc-vmstate-source");
    source.regs[ADC_CR] = ADC_CR_ADEN | ADC_CR_ADSTART;
    source.regs[ADC_CFGR] = 3u << ADC_CFGR_RES_SHIFT;
    source.conversion_active = true;
    source.current_rank = 0;
    source.rank_remaining_half_cycles = 5;
    source.rank_last_ns = 0;
    source.rank_clock_hz = 0;
    source.next_sample_ns = 0;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    dm_mc02_adc_init(&restored, NULL, "adc-vmstate-restored");
    g_assert_false(timer_pending(restored.sample_timer));
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_true(timer_pending(restored.sample_timer));

    /* Five half-cycles at the restored 1.5 MHz clock take ceil(5 / 3 MHz)
     * = 1667 ns.  Before this fix, the zero serialized deadline left the
     * active conversion permanently disarmed. */
    deadline = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                          QEMU_TIMER_ATTR_ALL);
    g_assert_cmpint(deadline, ==, 1667);
    adc_clock_ns += deadline;
    g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_true(restored.regs[ADC_ISR] & ADC_ISR_EOC);
    g_assert_cmphex(restored.sample_values[0], ==, 0x0100);
    g_assert_cmphex(mmio_read(&restored, ADC_DR, 4), ==, 0x0004);

    free_adc(&restored);
    free_adc(&source);
}

static void test_rearms_paused_injected_rank_after_load(void)
{
    DmMc02Adc source;
    DmMc02Adc restored;
    int64_t deadline;

    adc_clock_ns = 0;
    dm_mc02_adc_init(&source, NULL, "adc-vmstate-source");
    source.regs[ADC_CR] = ADC_CR_ADEN | ADC_CR_JADSTART;
    source.regs[ADC_CFGR] = 3u << ADC_CFGR_RES_SHIFT;
    source.regs[ADC_JSQR] = 0; /* One injected rank. */
    source.injected_conversion_active = true;
    source.current_injected_rank = 0;
    source.injected_rank_remaining_half_cycles = 5;
    source.injected_rank_last_ns = 0;
    source.injected_rank_clock_hz = 0;
    source.next_injected_sample_ns = 0;
    source.injected_active_context_valid = true;
    source.injected_active_jsqr = 0;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    dm_mc02_adc_init(&restored, NULL, "adc-vmstate-restored");
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_true(timer_pending(restored.injected_timer));

    deadline = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                          QEMU_TIMER_ATTR_ALL);
    g_assert_cmpint(deadline, ==, 1667);
    adc_clock_ns += deadline;
    g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_true(restored.regs[ADC_ISR] & ADC_ISR_JEOC);
    g_assert_cmphex(restored.sample_values[0], ==, 0x0100);
    g_assert_cmphex(mmio_read(&restored, ADC_JDR1, 4), ==, 0x0004);

    free_adc(&restored);
    free_adc(&source);
}

static void test_rearms_paused_calibration_after_load(void)
{
    DmMc02Adc source;
    DmMc02Adc restored;
    int64_t deadline;

    adc_clock_ns = 0;
    dm_mc02_adc_init(&source, NULL, "adc-vmstate-source");
    put_reg(source.regs, ADC_CR, ADC_CR_ADCAL);
    source.calibration_active = true;
    source.calibration_cycles = 5;
    source.calibration_remaining_cycles = 5;
    source.calibration_last_ns = 0;
    source.calibration_clock_hz = 0;
    source.next_sample_ns = 0;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    dm_mc02_adc_init(&restored, NULL, "adc-vmstate-restored");
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_true(timer_pending(restored.sample_timer));

    /* Five calibration cycles at 1.5 MHz take ceil(5 / 1.5 MHz) = 3334 ns. */
    deadline = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                          QEMU_TIMER_ATTR_ALL);
    g_assert_cmpint(deadline, ==, 3334);
    adc_clock_ns += deadline;
    g_assert_true(qemu_clock_run_timers(QEMU_CLOCK_VIRTUAL));
    g_assert_false(restored.calibration_active);
    g_assert_false(restored.regs[ADC_CR] & ADC_CR_ADCAL);

    free_adc(&restored);
    free_adc(&source);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-adc-runtime.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    qemu_init_main_loop(&error_abort);
    qemu_clock_enable(QEMU_CLOCK_VIRTUAL, true);
    module_call_init(MODULE_INIT_QOM);
    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-adc/runtime-sync/rearm-paused-rank",
                    test_rearms_paused_regular_rank_after_load);
    g_test_add_func("/dm-adc/runtime-sync/rearm-paused-injected-rank",
                    test_rearms_paused_injected_rank_after_load);
    g_test_add_func("/dm-adc/runtime-sync/rearm-paused-calibration",
                    test_rearms_paused_calibration_after_load);
    g_test_add_func("/dm-adc/resolution/mmio-readback",
                    test_resolution_mmio_readback);
    g_test_add_func("/dm-adc/resolution/supported-deadlines",
                    test_supported_resolution_deadlines);
    g_test_add_func("/dm-adc/resolution/results",
                    test_resolution_results);
    g_test_add_func("/dm-adc/resolution/unsupported-starts-preserve-results",
                    test_unsupported_resolution_starts_preserve_results);
    g_test_add_func("/dm-adc/resolution/unsupported-external-boundaries",
                    test_unsupported_resolution_external_boundaries);
    g_test_add_func("/dm-adc/resolution/change-protected-while-busy",
                    test_resolution_change_protected_while_busy);
    g_test_add_func("/dm-adc/resolution/interleaved-sentinel",
                    test_interleaved_resolution_sentinel);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
