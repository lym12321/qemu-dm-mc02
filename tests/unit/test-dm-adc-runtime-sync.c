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
#define ADC_CR  0x08
#define ADC_JSQR 0x4c
#define ADC_JDR1 0x80
#define ADC_ISR_EOC (1u << 2)
#define ADC_ISR_JEOC (1u << 5)
#define ADC_CR_ADEN (1u << 0)
#define ADC_CR_ADSTART (1u << 2)
#define ADC_CR_JADSTART (1u << 3)
#define ADC_CR_ADCAL (1u << 31)

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
    int ret = vmstate_load_state(file, dm_mc02_adc_vmstate(), state, 1);

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

static void test_rearms_paused_regular_rank_after_load(void)
{
    DmMc02Adc source;
    DmMc02Adc restored;
    int64_t deadline;

    adc_clock_ns = 0;
    dm_mc02_adc_init(&source, NULL, "adc-vmstate-source");
    source.regs[ADC_CR] = ADC_CR_ADEN | ADC_CR_ADSTART;
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
    g_assert_cmpuint(restored.regs[ADC_JDR1 + 1], !=, 0);

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
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
