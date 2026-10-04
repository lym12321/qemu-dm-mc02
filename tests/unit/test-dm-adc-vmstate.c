/* Focused VMState contract test for the board-independent ADC component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc.h"
#include "migration/migration.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_adc_sync_calls;

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

static void save_state_v(const DmMc02Adc *state,
                         const VMStateDescription *description,
                         int version_id)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state_v(file, description, (void *)state,
                                         NULL, version_id, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static void save_state(const DmMc02Adc *state)
{
    save_state_v(state, dm_mc02_adc_vmstate(), 2);
}

static int load_state_v(DmMc02Adc *state,
                        const VMStateDescription *description,
                        int version_id)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, description, state, version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static int load_state(DmMc02Adc *state)
{
    return load_state_v(state, dm_mc02_adc_vmstate(), 2);
}

static void store_reg(DmMc02Adc *state, unsigned offset, uint32_t value)
{
    for (unsigned byte = 0; byte < sizeof(value); ++byte) {
        state->regs[offset + byte] = value >> (byte * 8);
    }
}

static void set_resolution(DmMc02Adc *state, unsigned code)
{
    state->regs[0x0c] = (state->regs[0x0c] & ~0x1cu) | (code << 2);
}

static void prepare_source(DmMc02Adc *state)
{
    uint32_t jsqr = 1u | (3u << 9) | (7u << 15);

    memset(state, 0, sizeof(*state));
    for (unsigned i = 0; i < ARRAY_SIZE(state->regs); ++i) {
        state->regs[i] = (uint8_t)(0x31u + i * 7u);
    }
    state->regs[0x08] = 0x05; /* ADEN | ADSTART. */
    state->regs[0x08] |= 1u << 3; /* JADSTART. */
    {
        uint32_t cfgr = 1u << 13; /* CONT. */

        for (unsigned byte = 0; byte < sizeof(cfgr); ++byte) {
            state->regs[0x0c + byte] = cfgr >> (byte * 8);
        }
    }
    state->regs[0x30] = 1u; /* Two regular ranks. */
    for (unsigned byte = 0; byte < sizeof(jsqr); ++byte) {
        state->regs[0x4c + byte] = jsqr >> (byte * 8);
    }

    state->conversion_active = true;
    state->current_rank = 1;
    state->rank_remaining_half_cycles = 19;
    state->rank_last_ns = 1000;
    state->rank_clock_hz = 1500000;
    state->next_sample_ns = 2000;
    state->injected_conversion_active = true;
    state->current_injected_rank = 1;
    state->injected_rank_remaining_half_cycles = 23;
    state->injected_rank_last_ns = 1100;
    state->injected_rank_clock_hz = 1500000;
    state->next_injected_sample_ns = 2200;
    state->regulator_ready = true;
    state->regulator_ready_ns = 3000;
    state->linear_calibration_window = 5;
    for (unsigned i = 0; i < ARRAY_SIZE(state->linear_calibration_words); ++i) {
        state->linear_calibration_words[i] = 0x01020300u + i;
    }
    state->linear_calibration_words[5] = 0x2a5;
    state->injected_active_context_valid = true;
    state->injected_active_jsqr = jsqr;
    state->regular_sequence_id = 8;
    state->regular_active_sequence_id = 9;
    state->regular_shared_conversion = true;
    state->legacy_samples = false;
    state->sample_values[0] = 0x1234;
    state->sample_values[1] = 0xabcd;
    state->board_source_values[2] = 0x2345;
    state->board_source_valid = 1u << 2;
    state->external_values[4] = 0x3456;
    state->external_values[5] = 0x4567;
    state->external_raw_valid = 1u << 4;
    state->external_pin_voltage_valid = 1u << 5;
    state->external_override_kind[4] = 1;
    state->external_override_kind[5] = 2;
}

static void test_round_trip_restores_dynamic_state(void)
{
    DmMc02Adc source;
    DmMc02Adc restored;

    prepare_source(&source);
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_adc_sync_calls = 0;

    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_true(restored.conversion_active);
    g_assert_true(restored.regular_shared_conversion);
    g_assert_cmpuint(restored.regular_sequence_id, ==, 8);
    g_assert_cmpuint(restored.regular_active_sequence_id, ==, 9);
    g_assert_cmpuint(restored.current_rank, ==, source.current_rank);
    g_assert_cmpuint(restored.next_sample_ns, ==, source.next_sample_ns);
    g_assert_cmpuint(restored.rank_remaining_half_cycles, ==,
                     source.rank_remaining_half_cycles);
    g_assert_true(restored.injected_conversion_active);
    g_assert_cmpuint(restored.next_injected_sample_ns, ==,
                     source.next_injected_sample_ns);
    g_assert_cmpuint(restored.injected_active_jsqr, ==,
                     source.injected_active_jsqr);
    g_assert_cmpmem(restored.linear_calibration_words,
                    sizeof(restored.linear_calibration_words),
                    source.linear_calibration_words,
                    sizeof(source.linear_calibration_words));
    g_assert_cmpmem(restored.external_override_kind,
                    sizeof(restored.external_override_kind),
                    source.external_override_kind,
                    sizeof(source.external_override_kind));
    g_assert_cmpuint(restored.board_source_valid, ==,
                     source.board_source_valid);
    g_assert_cmpuint(restored.external_raw_valid, ==,
                     source.external_raw_valid);
    g_assert_cmpuint(restored.external_pin_voltage_valid, ==,
                     source.external_pin_voltage_valid);
    g_assert_cmpuint(dm_mc02_adc_sync_calls, ==, 1);
}

static void test_rejects_inconsistent_state_without_sync(void)
{
    DmMc02Adc source;
    DmMc02Adc restored;

    prepare_source(&source);
    source.regs[0x08] &= ~(1u << 2); /* Active conversion without ADSTART. */
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_adc_sync_calls = 0;

    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_cmpuint(dm_mc02_adc_sync_calls, ==, 0);
}

static void test_rejects_truncated_state_without_sync(void)
{
    DmMc02Adc state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    dm_mc02_adc_sync_calls = 0;

    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_cmpuint(dm_mc02_adc_sync_calls, ==, 0);
}

enum ConversionState {
    REGULAR_ARMED,
    INJECTED_ARMED,
    REGULAR_ACTIVE,
    INJECTED_ACTIVE,
    REGULAR_WAITING_FOR_DATA,
    REGULAR_DISCONTINUOUS_PAUSED,
    REGULAR_WAITING_FOR_AUTO_INJECTED,
    INJECTED_DISCONTINUOUS_PAUSED,
    CONVERSION_STATE_COUNT,
};

static void prepare_conversion_state(DmMc02Adc *state,
                                      enum ConversionState kind)
{
    uint32_t cr = 1u; /* ADEN. */
    uint32_t cfgr = 0;
    uint32_t jsqr = 0;

    memset(state, 0, sizeof(*state));
    switch (kind) {
    case REGULAR_ARMED:
        cr |= 1u << 2;
        cfgr = 1u << 10; /* EXTEN: wait for an external trigger. */
        break;
    case INJECTED_ARMED:
        cr |= 1u << 3;
        jsqr = 1u << 7; /* JEXTEN: wait for an external trigger. */
        state->injected_active_context_valid = true;
        break;
    case REGULAR_ACTIVE:
        cr |= 1u << 2;
        state->conversion_active = true;
        state->rank_remaining_half_cycles = 17;
        state->rank_last_ns = 1000;
        state->rank_clock_hz = 1500000;
        state->next_sample_ns = 2000;
        break;
    case INJECTED_ACTIVE:
        cr |= 1u << 3;
        state->injected_conversion_active = true;
        state->injected_active_context_valid = true;
        state->injected_rank_remaining_half_cycles = 17;
        state->injected_rank_last_ns = 1000;
        state->injected_rank_clock_hz = 1500000;
        state->next_injected_sample_ns = 2000;
        break;
    case REGULAR_WAITING_FOR_DATA:
        cr |= 1u << 2;
        cfgr = 1u << 14; /* AUTDLY. */
        state->conversion_active = true;
        state->regular_waiting_for_data = true;
        state->regs[0x00] = 1u << 2; /* Unacknowledged EOC. */
        break;
    case REGULAR_DISCONTINUOUS_PAUSED:
        cr |= 1u << 2;
        cfgr = (1u << 10) | (1u << 16); /* EXTEN | DISCEN. */
        state->regular_discontinuous_paused = true;
        break;
    case REGULAR_WAITING_FOR_AUTO_INJECTED:
        cr |= (1u << 2) | (1u << 3);
        cfgr = 1u << 25; /* JAUTO. */
        state->conversion_active = true;
        state->injected_conversion_active = true;
        state->injected_active_context_valid = true;
        state->regular_waiting_for_auto_injected = true;
        state->injected_rank_remaining_half_cycles = 17;
        state->injected_rank_last_ns = 1000;
        state->injected_rank_clock_hz = 1500000;
        state->next_injected_sample_ns = 2000;
        break;
    case INJECTED_DISCONTINUOUS_PAUSED:
        cr |= 1u << 3;
        cfgr = 1u << 20; /* JDISCEN. */
        jsqr = 1u << 7;
        state->injected_discontinuous_paused = true;
        state->injected_active_context_valid = true;
        break;
    default:
        g_assert_not_reached();
    }
    store_reg(state, 0x08, cr);
    store_reg(state, 0x0c, cfgr);
    store_reg(state, 0x4c, jsqr);
    state->injected_active_jsqr = jsqr;
}

static void test_rejects_unsupported_conversion_without_sync(void)
{
    const VMStateDescription *descriptions[] = {
        dm_mc02_adc_vmstate(), &vmstate_dm_mc02_adc_raw,
    };

    /* RES width is independently defined by ST H723 CMSIS as [4:2].
     * Codes 4..7 are an explicitly unsupported model subset, not a claim
     * that silicon assigns identical behavior to all these encodings. */
    for (unsigned mode = 0; mode < ARRAY_SIZE(descriptions); ++mode) {
        for (int version = 1; version <= 2; ++version) {
            for (unsigned code = 4; code < 8; ++code) {
                for (enum ConversionState kind = REGULAR_ARMED;
                     kind < CONVERSION_STATE_COUNT; ++kind) {
                    DmMc02Adc source;
                    DmMc02Adc restored = { 0 };

                    prepare_conversion_state(&source, kind);
                    g_assert_true(dm_mc02_adc_state_valid(&source));
                    set_resolution(&source, code);
                    save_state_v(&source, descriptions[mode], version);
                    dm_mc02_adc_sync_calls = 0;
                    g_assert_cmpint(load_state_v(&restored,
                                                 descriptions[mode], version),
                                    ==, -EINVAL);
                    g_assert_cmpuint(dm_mc02_adc_sync_calls, ==, 0);
                }
            }
        }
    }
}

static void test_supported_resolution_stream_compatibility(void)
{
    const VMStateDescription *descriptions[] = {
        dm_mc02_adc_vmstate(), &vmstate_dm_mc02_adc_raw,
    };

    for (unsigned mode = 0; mode < ARRAY_SIZE(descriptions); ++mode) {
        for (int version = 1; version <= 2; ++version) {
            for (unsigned code = 0; code < 4; ++code) {
                DmMc02Adc source;
                DmMc02Adc restored = { 0 };

                prepare_source(&source);
                set_resolution(&source, code);
                save_state_v(&source, descriptions[mode], version);
                dm_mc02_adc_sync_calls = 0;
                g_assert_cmpint(load_state_v(&restored, descriptions[mode],
                                             version), ==, 0);
                g_assert_cmpmem(restored.regs, sizeof(restored.regs),
                                source.regs, sizeof(source.regs));
                g_assert_true(restored.conversion_active);
                g_assert_true(restored.injected_conversion_active);
                g_assert_cmpuint(restored.next_sample_ns, ==, 2000);
                g_assert_cmpuint(restored.next_injected_sample_ns, ==, 2200);
                g_assert_cmpint(restored.regular_shared_conversion, ==,
                                version == 2);
                g_assert_cmpuint(dm_mc02_adc_sync_calls, ==, mode == 0);
            }
        }
    }
}

static void test_unsupported_idle_and_calibration_round_trip(void)
{
    const VMStateDescription *descriptions[] = {
        dm_mc02_adc_vmstate(), &vmstate_dm_mc02_adc_raw,
    };

    for (unsigned mode = 0; mode < ARRAY_SIZE(descriptions); ++mode) {
        for (int version = 1; version <= 2; ++version) {
            for (unsigned code = 4; code < 8; ++code) {
                for (unsigned calibration = 0; calibration < 2;
                     ++calibration) {
                    DmMc02Adc source = { 0 };
                    DmMc02Adc restored = { 0 };

                    set_resolution(&source, code);
                    source.regs[0x00] = 1u << 2; /* A prior EOC may remain. */
                    store_reg(&source, 0x40, 0x1234); /* Prior ADC_DR. */
                    if (calibration) {
                        store_reg(&source, 0x08, 1u << 31); /* ADCAL. */
                        source.calibration_active = true;
                        source.calibration_cycles = 16384;
                        source.calibration_remaining_cycles = 1024;
                        source.calibration_last_ns = 1000;
                        source.calibration_clock_hz = 1500000;
                        source.next_sample_ns = 2000;
                    }
                    save_state_v(&source, descriptions[mode], version);
                    dm_mc02_adc_sync_calls = 0;
                    g_assert_cmpint(load_state_v(&restored,
                                                 descriptions[mode], version),
                                    ==, 0);
                    g_assert_cmpmem(restored.regs, sizeof(restored.regs),
                                    source.regs, sizeof(source.regs));
                    g_assert_false(restored.conversion_active);
                    g_assert_false(restored.injected_conversion_active);
                    g_assert_cmpint(restored.calibration_active, ==,
                                    calibration != 0);
                    g_assert_cmpuint(restored.next_sample_ns, ==,
                                     source.next_sample_ns);
                    g_assert_cmpuint(restored.calibration_remaining_cycles,
                                     ==, source.calibration_remaining_cycles);
                    g_assert_cmpuint(dm_mc02_adc_sync_calls, ==, mode == 0);
                }
            }
        }
    }
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-adc-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-adc-vmstate/round-trip",
                    test_round_trip_restores_dynamic_state);
    g_test_add_func("/dm-adc-vmstate/reject-invalid",
                    test_rejects_inconsistent_state_without_sync);
    g_test_add_func("/dm-adc-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_sync);
    g_test_add_func("/dm-adc-vmstate/reject-unsupported-conversion",
                    test_rejects_unsupported_conversion_without_sync);
    g_test_add_func("/dm-adc-vmstate/supported-resolution-compatibility",
                    test_supported_resolution_stream_compatibility);
    g_test_add_func("/dm-adc-vmstate/unsupported-idle-calibration-round-trip",
                    test_unsupported_idle_and_calibration_round_trip);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
