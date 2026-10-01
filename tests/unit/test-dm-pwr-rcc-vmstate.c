/* Focused VMState contract test for the STM32H723 PWR/RCC component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_pwr_rcc.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

#define RCC_CR 0x00
#define RCC_D1CFGR 0x18

static int temp_fd;

typedef struct ClockObservation {
    unsigned calls;
    uint64_t last_hz;
} ClockObservation;

static void clock_changed(void *opaque, uint64_t hz)
{
    ClockObservation *observation = opaque;

    observation->calls++;
    observation->last_hz = hz;
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

static void save_state(const DmMc02PwrRcc *state,
                       const VMStateDescription *description)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, description, (void *)state,
                                       NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02PwrRcc *state,
                      const VMStateDescription *description)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, description, state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void prepare_source(DmMc02PwrRcc *state)
{
    dm_mc02_pwr_rcc_init(state, NULL);
    for (unsigned i = 0; i < DM_MC02_PWR_RCC_REGION_SIZE; ++i) {
        state->pwr_regs[i] = (uint8_t)(0x20u + i);
        state->rcc_regs[i] = (uint8_t)(0x80u + i);
    }
    /* Keep the restored effective source valid and running at HSI. */
    memset(state->rcc_regs + RCC_CR, 0, sizeof(uint32_t));
    state->rcc_regs[RCC_CR] = 1;
    memset(state->rcc_regs + RCC_D1CFGR, 0, sizeof(uint32_t));
    state->system_clock_source = 0;
    state->adc_clock_configured = true;
}

static void test_round_trip_restores_registers_and_notifies(void)
{
    DmMc02PwrRcc source;
    DmMc02PwrRcc restored;
    ClockObservation observation = { 0 };

    prepare_source(&source);
    save_state(&source, dm_mc02_pwr_rcc_vmstate());

    dm_mc02_pwr_rcc_init(&restored, NULL);
    dm_mc02_pwr_rcc_set_clock_callback(&restored, clock_changed,
                                       &observation);
    observation.calls = 0;
    g_assert_cmpint(load_state(&restored, dm_mc02_pwr_rcc_vmstate()), ==, 0);

    g_assert_cmpmem(restored.pwr_regs, sizeof(restored.pwr_regs),
                    source.pwr_regs, sizeof(source.pwr_regs));
    g_assert_cmpmem(restored.rcc_regs, sizeof(restored.rcc_regs),
                    source.rcc_regs, sizeof(source.rcc_regs));
    g_assert_cmpuint(restored.system_clock_source, ==,
                     source.system_clock_source);
    g_assert_true(restored.adc_clock_configured);
    g_assert_cmpuint(observation.calls, ==, 1);
    g_assert_cmpuint(observation.last_hz, ==, 64000000);
}

static void test_rejects_invalid_source_without_notification(void)
{
    DmMc02PwrRcc source;
    DmMc02PwrRcc restored;
    ClockObservation observation = { 0 };

    prepare_source(&source);
    source.system_clock_source = 4;
    save_state(&source, dm_mc02_pwr_rcc_vmstate());

    dm_mc02_pwr_rcc_init(&restored, NULL);
    dm_mc02_pwr_rcc_set_clock_callback(&restored, clock_changed,
                                       &observation);
    observation.calls = 0;
    g_assert_cmpint(load_state(&restored, dm_mc02_pwr_rcc_vmstate()), !=, 0);
    g_assert_cmpuint(observation.calls, ==, 0);
}

static void test_rejects_truncated_state_without_notification(void)
{
    DmMc02PwrRcc state;
    ClockObservation observation = { 0 };
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    dm_mc02_pwr_rcc_init(&state, NULL);
    dm_mc02_pwr_rcc_set_clock_callback(&state, clock_changed,
                                       &observation);
    observation.calls = 0;
    g_assert_cmpint(load_state(&state, dm_mc02_pwr_rcc_vmstate()), !=, 0);
    g_assert_cmpuint(observation.calls, ==, 0);
}

static void test_raw_description_does_not_notify(void)
{
    DmMc02PwrRcc source;
    DmMc02PwrRcc restored;
    ClockObservation observation = { 0 };

    prepare_source(&source);
    save_state(&source, dm_mc02_pwr_rcc_vmstate_raw());

    dm_mc02_pwr_rcc_init(&restored, NULL);
    dm_mc02_pwr_rcc_set_clock_callback(&restored, clock_changed,
                                       &observation);
    observation.calls = 0;
    g_assert_cmpint(load_state(&restored,
                               dm_mc02_pwr_rcc_vmstate_raw()), ==, 0);
    g_assert_cmpmem(restored.pwr_regs, sizeof(restored.pwr_regs),
                    source.pwr_regs, sizeof(source.pwr_regs));
    g_assert_cmpmem(restored.rcc_regs, sizeof(restored.rcc_regs),
                    source.rcc_regs, sizeof(source.rcc_regs));
    g_assert_cmpuint(observation.calls, ==, 0);
}

static uint32_t rcc_readl(const DmMc02PwrRcc *state, hwaddr offset)
{
    return state->rcc.ops->read(state->rcc.opaque, offset,
                                sizeof(uint32_t));
}

static void rcc_writel(DmMc02PwrRcc *state, hwaddr offset, uint32_t value)
{
    state->rcc.ops->write(state->rcc.opaque, offset, value,
                          sizeof(uint32_t));
}

static void rcc_writew(DmMc02PwrRcc *state, hwaddr offset, uint16_t value)
{
    state->rcc.ops->write(state->rcc.opaque, offset, value,
                          sizeof(uint16_t));
}

static void test_iwdg_reset_reason_survives_reset_and_clears_with_rmvf(void)
{
    DmMc02PwrRcc state;

    dm_mc02_pwr_rcc_init(&state, NULL);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==, 0);

    dm_mc02_pwr_rcc_note_iwdg_reset(&state);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_IWDG1RSTF);

    /* Reset-source bits are read-only; writing one of them is harmless. */
    rcc_writel(&state, DM_MC02_RCC_RSR_OFFSET,
               DM_MC02_RCC_RSR_IWDG1RSTF);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_IWDG1RSTF);

    dm_mc02_pwr_rcc_note_iwdg_reset(&state);
    rcc_writew(&state, DM_MC02_RCC_RSR_OFFSET + 2, 1);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==, 0);

    dm_mc02_pwr_rcc_note_iwdg_reset(&state);

    /* The machine reset path must not erase the reason before the next boot. */
    dm_mc02_pwr_rcc_reset(&state);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_IWDG1RSTF);

    rcc_writel(&state, DM_MC02_RCC_RSR_OFFSET, DM_MC02_RCC_RSR_RMVF);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==, 0);
}

static void test_software_reset_reason_is_latched_and_preserved(void)
{
    DmMc02PwrRcc state;

    dm_mc02_pwr_rcc_init(&state, NULL);
    dm_mc02_pwr_rcc_note_software_reset(&state, 0, 0);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==, 0);

    dm_mc02_pwr_rcc_note_software_reset(&state, 0, 1);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_SFTRSTF);
    dm_mc02_pwr_rcc_reset(&state);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_SFTRSTF);

    rcc_writel(&state, DM_MC02_RCC_RSR_OFFSET, DM_MC02_RCC_RSR_RMVF);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==, 0);
}

static void test_power_on_reset_reason_is_explicit_and_preserved(void)
{
    DmMc02PwrRcc state;

    dm_mc02_pwr_rcc_init(&state, NULL);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==, 0);

    dm_mc02_pwr_rcc_note_power_on_reset(&state);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_PORRSTF);
    dm_mc02_pwr_rcc_reset(&state);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_PORRSTF);

    /* Repeated notification is idempotent, and only RMVF clears the source. */
    dm_mc02_pwr_rcc_note_power_on_reset(&state);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_PORRSTF);
    rcc_writel(&state, DM_MC02_RCC_RSR_OFFSET, DM_MC02_RCC_RSR_RMVF);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==, 0);
}

static void test_brownout_reset_reason_is_latched_and_preserved(void)
{
    DmMc02PwrRcc state;

    dm_mc02_pwr_rcc_init(&state, NULL);
    dm_mc02_pwr_rcc_note_brownout_reset(&state);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_BORRSTF);
    dm_mc02_pwr_rcc_reset(&state);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==,
                    DM_MC02_RCC_RSR_BORRSTF);
    rcc_writel(&state, DM_MC02_RCC_RSR_OFFSET, DM_MC02_RCC_RSR_RMVF);
    g_assert_cmphex(rcc_readl(&state, DM_MC02_RCC_RSR_OFFSET), ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-pwr-rcc-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-pwr-rcc-vmstate/round-trip-notify",
                    test_round_trip_restores_registers_and_notifies);
    g_test_add_func("/dm-pwr-rcc-vmstate/reject-invalid-source",
                    test_rejects_invalid_source_without_notification);
    g_test_add_func("/dm-pwr-rcc-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_notification);
    g_test_add_func("/dm-pwr-rcc-vmstate/raw-no-notification",
                    test_raw_description_does_not_notify);
    g_test_add_func("/dm-pwr-rcc/reset-reason-rmvf",
                    test_iwdg_reset_reason_survives_reset_and_clears_with_rmvf);
    g_test_add_func("/dm-pwr-rcc/software-reset-reason",
                    test_software_reset_reason_is_latched_and_preserved);
    g_test_add_func("/dm-pwr-rcc/power-on-reset-reason",
                    test_power_on_reset_reason_is_explicit_and_preserved);
    g_test_add_func("/dm-pwr-rcc/brownout-reset-reason",
                    test_brownout_reset_reason_is_latched_and_preserved);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
