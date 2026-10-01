/* Focused VMState contract test for the STM32H723 ADC pair boundary. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc_pair.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_adc_pair_common_sync_calls;
extern unsigned dm_mc02_adc_pair_adc_sync_calls;

static int temp_fd;

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

static void save_state(const DmMc02AdcPair *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_adc_pair_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02AdcPair *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_adc_pair_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void prepare_adc(DmMc02Adc *adc, uint64_t sequence_id)
{
    memset(adc, 0, sizeof(*adc));
    /* ADEN | ADSTART: a regular conversion is in flight. */
    adc->regs[0x08] = 0x05;
    adc->conversion_active = true;
    adc->current_rank = 0;
    adc->rank_remaining_half_cycles = 17;
    adc->rank_last_ns = 1000;
    adc->rank_clock_hz = 1500000;
    adc->next_sample_ns = 2000;
    adc->clock_hz = 1500000;
    adc->regular_sequence_id = sequence_id;
    adc->regular_active_sequence_id = sequence_id;
    adc->regular_shared_conversion = true;
    adc->sample_values[0] = 0x1234;
    adc->sample_values[1] = 0xabcd;
}

static void prepare_source(DmMc02AdcPair *state)
{
    memset(state, 0, sizeof(*state));
    state->common.ccr =
        DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
        (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14);
    state->common.ccr_configured = true;
    state->common.next_conversion_id = 9;
    prepare_adc(&state->adc[0], 9);
    prepare_adc(&state->adc[1], 9);
}

static void test_round_trip_restores_pair_before_projection(void)
{
    DmMc02AdcPair source;
    DmMc02AdcPair restored;

    prepare_source(&source);
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    dm_mc02_adc_pair_common_sync_calls = 0;
    dm_mc02_adc_pair_adc_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), ==, 0);

    g_assert_cmphex(restored.common.ccr, ==, source.common.ccr);
    g_assert_cmpuint(restored.common.next_conversion_id, ==,
                     source.common.next_conversion_id);
    for (unsigned i = 0; i < DM_MC02_ADC_PAIR_COUNT; ++i) {
        g_assert_cmpmem(restored.adc[i].regs, sizeof(restored.adc[i].regs),
                        source.adc[i].regs, sizeof(source.adc[i].regs));
        g_assert_true(restored.adc[i].conversion_active);
        g_assert_true(restored.adc[i].regular_shared_conversion);
        g_assert_cmpuint(restored.adc[i].regular_active_sequence_id, ==, 9);
        g_assert_cmpuint(restored.adc[i].next_sample_ns, ==, 2000);
    }
    g_assert_cmpuint(dm_mc02_adc_pair_common_sync_calls, ==, 1);
    g_assert_cmpuint(dm_mc02_adc_pair_adc_sync_calls, ==,
                     DM_MC02_ADC_PAIR_COUNT);
}

static void test_rejects_common_adc_identity_mismatch(void)
{
    DmMc02AdcPair source;
    DmMc02AdcPair restored;

    prepare_source(&source);
    source.common.master_sample_valid = true;
    source.common.master_sample_conversion_id = 8;
    source.common.master_sample_rank = 0;
    source.common.master_sample_timestamp_ns = 1500;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    dm_mc02_adc_pair_common_sync_calls = 0;
    dm_mc02_adc_pair_adc_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_cmpuint(dm_mc02_adc_pair_common_sync_calls, ==, 0);
    g_assert_cmpuint(dm_mc02_adc_pair_adc_sync_calls, ==, 0);
}

static void test_rejects_truncated_state_without_projection(void)
{
    DmMc02AdcPair state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    dm_mc02_adc_pair_common_sync_calls = 0;
    dm_mc02_adc_pair_adc_sync_calls = 0;
    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_cmpuint(dm_mc02_adc_pair_common_sync_calls, ==, 0);
    g_assert_cmpuint(dm_mc02_adc_pair_adc_sync_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf(
        "%s/dm-adc-pair-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-adc-pair-vmstate/round-trip",
                    test_round_trip_restores_pair_before_projection);
    g_test_add_func("/dm-adc-pair-vmstate/reject-identity-mismatch",
                    test_rejects_common_adc_identity_mismatch);
    g_test_add_func("/dm-adc-pair-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_projection);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
