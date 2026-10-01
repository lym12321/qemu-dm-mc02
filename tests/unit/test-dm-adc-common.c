/* Focused register and VMState contract test for ADC12 common. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc_common.h"
#include "migration/migration.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

static int temp_fd;

static uint64_t common_read(DmMc02AdcCommon *state, hwaddr offset,
                            unsigned size)
{
    uint64_t value = 0;

    value = state->iomem.ops->read(state->iomem.opaque, offset, size);
    return value;
}

static void common_write(DmMc02AdcCommon *state, hwaddr offset,
                         uint64_t value, unsigned size)
{
    state->iomem.ops->write(state->iomem.opaque, offset, value, size);
}

static uint32_t master_status;
static uint32_t slave_status;
static unsigned clock_callback_count;
static uint32_t clock_callback_ccr;
static unsigned regular_start_peer_count;
static unsigned regular_start_peer_source;
static uint64_t regular_start_peer_conversion_id;
static bool regular_start_peer_result;
static unsigned external_trigger_peer_count;
static unsigned external_trigger_peer_source;
static uint32_t external_trigger_peer_trigger_source;
static bool external_trigger_peer_rising;
static unsigned external_trigger_peer_event_count;
static uint64_t external_trigger_peer_timestamp_ns;
static uint64_t external_trigger_peer_conversion_id;
static bool external_trigger_peer_result;
static unsigned data_ready_count;
static uint32_t data_ready_value;
static uint64_t data_ready_timestamp_ns;
static unsigned cdr2_data_ready_count;
static uint32_t cdr2_data_ready_value;
static unsigned cdr2_data_ready_source;
static uint64_t cdr2_data_ready_timestamp_ns;
static unsigned cdr_read_count;
static unsigned cdr2_read_count;
static unsigned cdr2_read_source;

static uint32_t read_master_status(void *opaque)
{
    (void)opaque;
    return master_status;
}

static uint32_t read_slave_status(void *opaque)
{
    (void)opaque;
    return slave_status;
}

static void clock_changed(void *opaque, uint32_t ccr)
{
    (void)opaque;
    clock_callback_count++;
    clock_callback_ccr = ccr;
}

static bool regular_start_peer(void *opaque, unsigned source,
                               uint64_t conversion_id)
{
    (void)opaque;
    regular_start_peer_count++;
    regular_start_peer_source = source;
    regular_start_peer_conversion_id = conversion_id;
    return regular_start_peer_result;
}

static bool external_trigger_peer(
    void *opaque, unsigned source, uint32_t trigger_source, bool rising,
    unsigned event_count, uint64_t timestamp_ns, uint64_t conversion_id)
{
    (void)opaque;
    external_trigger_peer_count++;
    external_trigger_peer_source = source;
    external_trigger_peer_trigger_source = trigger_source;
    external_trigger_peer_rising = rising;
    external_trigger_peer_event_count = event_count;
    external_trigger_peer_timestamp_ns = timestamp_ns;
    external_trigger_peer_conversion_id = conversion_id;
    return external_trigger_peer_result;
}

static void data_ready(void *opaque, uint32_t data, uint64_t timestamp_ns)
{
    (void)opaque;
    data_ready_count++;
    data_ready_value = data;
    data_ready_timestamp_ns = timestamp_ns;
}

static void cdr2_data_ready(void *opaque, uint32_t data, unsigned source,
                            uint64_t timestamp_ns)
{
    (void)opaque;
    cdr2_data_ready_count++;
    cdr2_data_ready_value = data;
    cdr2_data_ready_source = source;
    cdr2_data_ready_timestamp_ns = timestamp_ns;
}

static void cdr_read(void *opaque)
{
    (void)opaque;
    cdr_read_count++;
}

static void cdr2_read(void *opaque, unsigned source)
{
    (void)opaque;
    cdr2_read_count++;
    cdr2_read_source = source;
}

static void init_common(DmMc02AdcCommon *state)
{
    memset(state, 0, sizeof(*state));
    master_status = 0;
    slave_status = 0;
    dm_mc02_adc_common_init(state, NULL, "adc-common-test");
    dm_mc02_adc_common_set_status_sources(
        state, read_master_status, NULL, read_slave_status, NULL);
    dm_mc02_adc_common_set_clock_callback(state, clock_changed, NULL);
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

/* Version 1 is the pre-generator stream.  Keep this fixture explicit so a
 * current loader cannot accidentally pass only because the test writes a
 * version-2 stream and asks the loader to interpret it as version 1. */
static const VMStateDescription vmstate_dm_mc02_adc_common_v1_test = {
    .name = "dm-mc02-adc-common",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32(ccr, DmMc02AdcCommon),
        VMSTATE_UINT32(cdr, DmMc02AdcCommon),
        VMSTATE_UINT32(cdr2, DmMc02AdcCommon),
        VMSTATE_BOOL(ccr_configured, DmMc02AdcCommon),
        VMSTATE_BOOL(master_sample_valid, DmMc02AdcCommon),
        VMSTATE_UINT64(master_sample_conversion_id, DmMc02AdcCommon),
        VMSTATE_UINT32(master_sample_rank, DmMc02AdcCommon),
        VMSTATE_UINT16(master_sample_value, DmMc02AdcCommon),
        VMSTATE_UINT64(master_sample_timestamp_ns, DmMc02AdcCommon),
        VMSTATE_BOOL(slave_sample_valid, DmMc02AdcCommon),
        VMSTATE_UINT64(slave_sample_conversion_id, DmMc02AdcCommon),
        VMSTATE_UINT32(slave_sample_rank, DmMc02AdcCommon),
        VMSTATE_UINT16(slave_sample_value, DmMc02AdcCommon),
        VMSTATE_UINT64(slave_sample_timestamp_ns, DmMc02AdcCommon),
        VMSTATE_END_OF_LIST()
    },
};

static void save_state_with_desc(const DmMc02AdcCommon *state,
                                 const VMStateDescription *desc)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, desc, (void *)state, NULL), ==,
                    0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static void save_state(const DmMc02AdcCommon *state)
{
    save_state_with_desc(state, dm_mc02_adc_common_vmstate());
}

static int load_state_version(DmMc02AdcCommon *state, int version_id)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_adc_common_vmstate(), state,
                                 version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static int load_state(DmMc02AdcCommon *state)
{
    return load_state_version(state, 6);
}

static void test_ccr_access_widths_and_mask(void)
{
    DmMc02AdcCommon state;
    const uint32_t mask = DM_MC02_ADC_COMMON_CCR_WRITABLE_MASK;

    init_common(&state);
    clock_callback_count = 0;

    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, UINT32_MAX, 4);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, 4),
                    ==, mask);
    g_assert_true(dm_mc02_adc_common_ccr_configured(&state));
    g_assert_cmpuint(clock_callback_count, ==, 1);
    g_assert_cmphex(clock_callback_ccr, ==, mask);

    dm_mc02_adc_common_reset(&state);
    clock_callback_count = 0;
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, 0xffff, 2);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, 4),
                    ==, 0xffff & mask);
    g_assert_cmpuint(clock_callback_count, ==, 1);

    dm_mc02_adc_common_reset(&state);
    clock_callback_count = 0;
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET + 0, 0xa5, 1);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET + 1, 0x5a, 1);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET + 2, 0x3c, 1);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET + 3, 0xc3, 1);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, 4),
                    ==, UINT32_C(0xc33c5aa5) & mask);
    g_assert_cmpuint(clock_callback_count, ==, 4);

    dm_mc02_adc_common_reset(&state);
    clock_callback_count = 0;
    /* The high half-word includes CKMODE/PRESC and must take the same path. */
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET + 2, 0x2c01, 2);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, 4),
                    ==, UINT32_C(0x2c010000) & mask);
    g_assert_cmpuint(clock_callback_count, ==, 1);
}

static void test_status_projection_and_read_only_data(void)
{
    DmMc02AdcCommon state;
    uint32_t expected;

    init_common(&state);
    master_status = UINT32_C(0xffffffff);
    slave_status = UINT32_C(0x0000057f);
    expected = DM_MC02_ADC_COMMON_STATUS_MASK |
               ((slave_status & DM_MC02_ADC_COMMON_STATUS_MASK) << 16);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CSR_OFFSET, 4),
                    ==, expected);

    common_write(&state, DM_MC02_ADC_COMMON_CSR_OFFSET, UINT32_MAX, 4);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CSR_OFFSET, 4),
                    ==, expected);

    dm_mc02_adc_common_set_data(&state, UINT32_C(0x11223344),
                                UINT32_C(0x55667788));
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x11223344));
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, UINT32_C(0x55667788));
    common_write(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, UINT32_MAX, 4);
    common_write(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, UINT32_MAX, 4);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x11223344));
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, UINT32_C(0x55667788));
}

static void test_reset_clears_state_but_keeps_wiring(void)
{
    DmMc02AdcCommon state;

    init_common(&state);
    regular_start_peer_count = 0;
    regular_start_peer_result = true;
    dm_mc02_adc_common_set_regular_start_peer(
        &state, regular_start_peer, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, UINT32_MAX, 4);
    dm_mc02_adc_common_set_data(&state, 1, 2);
    state.next_conversion_id = 23;
    dm_mc02_adc_common_reset(&state);
    g_assert_cmpuint(dm_mc02_adc_common_get_ccr(&state), ==, 0);
    g_assert_cmpuint(state.next_conversion_id, ==, 0);
    g_assert_false(dm_mc02_adc_common_ccr_configured(&state));
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, 0);
    slave_status = 0;
    master_status = 0x12;
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CSR_OFFSET, 4),
                    ==, 0x12);

    /* The master/slave admission callback is runtime wiring too. */
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS, 4);
    g_assert_true(dm_mc02_adc_common_admit_regular_start(&state, 0));
    g_assert_cmpuint(regular_start_peer_count, ==, 1);
    g_assert_cmpuint(regular_start_peer_conversion_id, ==, 1);
}

static void test_regular_pair_packing_and_rejection(void)
{
    DmMc02AdcCommon state;
    DmMc02AdcCommonRegularSampleResult result;

    init_common(&state);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);

    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 0, 0, UINT16_C(0x1234), 100);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_UNSUPPORTED);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 1, 0, UINT16_C(0x1234), 100);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 1, 1, 0, UINT16_C(0xabcd), 101);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0xabcd1234));

    /* The slave may be the first producer; packing is source-order
     * independent and remains master-low/slave-high. */
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 1, 2, 3, UINT16_C(0x5678), 200);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 2, 3, UINT16_C(0x1357), 200);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x56781357));

    /* An ID/rank mismatch never creates a synthetic pair; the next
     * matching event can still recover using the bounded pending slot. */
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 3, 0, UINT16_C(0x1111), 300);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 1, 4, 0, UINT16_C(0x2222), 400);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_MISMATCH);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x56781357));
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 4, 0, UINT16_C(0x3333), 400);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x22223333));

    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_8 << 14), 4);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 5, 0, UINT16_C(0x12a5), 500);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 1, 5, 0, UINT16_C(0x34c7), 500);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PACKING);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x22223333));
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 6, 0, UINT16_C(0x0056), 501);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 1, 6, 0, UINT16_C(0x0078), 502);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x7856c7a5));

    /* DAMDF=0 and the reserved DAMDF=1 are deliberately unsupported. */
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS, 4);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 6, 0, 1, 600);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_UNSUPPORTED);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (UINT32_C(1) << 14), 4);
    result = dm_mc02_adc_common_submit_regular_sample(
        &state, 0, 6, 0, 1, 600);
    g_assert_cmpint(result, ==, DM_MC02_ADC_COMMON_SAMPLE_UNSUPPORTED);
}

static void test_regular_pair_data_ready_callback(void)
{
    DmMc02AdcCommon state;

    init_common(&state);
    data_ready_count = 0;
    data_ready_value = 0;
    data_ready_timestamp_ns = 0;
    dm_mc02_adc_common_set_data_ready_callback(&state, data_ready, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);

    /* CDR is ready only after both producer halves arrive.  The callback
     * timestamp is the virtual time at which the pair became observable,
     * independent of producer arrival order. */
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 8, 0, UINT16_C(0xabcd), 400),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 8, 0, UINT16_C(0x1234), 500),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmpuint(data_ready_count, ==, 1);
    g_assert_cmphex(data_ready_value, ==, UINT32_C(0xabcd1234));
    g_assert_cmpuint(data_ready_timestamp_ns, ==, 500);

    /* Reset preserves runtime callback wiring but clears its producer state. */
    dm_mc02_adc_common_reset(&state);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_8 << 14), 4);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 1, 0, UINT16_C(0x0012), 600),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 1, 0, UINT16_C(0x0034), 601),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKING);
    g_assert_cmpuint(data_ready_count, ==, 1);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 2, 0, UINT16_C(0x0056), 602),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 2, 0, UINT16_C(0x0078), 550),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmpuint(data_ready_count, ==, 2);
    g_assert_cmphex(data_ready_value, ==, UINT32_C(0x78563412));
    g_assert_cmpuint(data_ready_timestamp_ns, ==, 602);
}

static void test_regular_pair_dma_overrun_gate(void)
{
    DmMc02AdcCommon state;

    init_common(&state);
    data_ready_count = 0;
    dm_mc02_adc_common_set_data_ready_callback(&state, data_ready, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);

    /* A clean pair publishes one CDR DMA event. */
    master_status = 0;
    slave_status = 0;
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 1, 0, UINT16_C(0x0101), 100),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 1, 0, UINT16_C(0x0202), 101),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmpuint(data_ready_count, ==, 1);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x02020101));

    /* The CDR register still receives the pair while either ADC reports OVR,
     * but the common DMA request is stopped at this boundary. */
    master_status = 1u << 4;
    slave_status = 0;
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 2, 0, UINT16_C(0x0303), 200),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    slave_status = 1u << 4;
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 2, 0, UINT16_C(0x0404), 201),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmpuint(data_ready_count, ==, 1);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x04040303));

    /* Clearing the producer OVR state releases the next common DMA event;
     * the common component itself has no private overrun latch. */
    master_status = 0;
    slave_status = 0;
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 3, 0, UINT16_C(0x0505), 300),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 3, 0, UINT16_C(0x0606), 301),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmpuint(data_ready_count, ==, 2);

    /* The gate is not applied to DAMDF=3, whose CDR accumulator has a
     * separate partial-word contract. */
    dm_mc02_adc_common_reset(&state);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_8 << 14), 4);
    master_status = 1u << 4;
    slave_status = 1u << 4;
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 4, 0, UINT16_C(0x0011), 400),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 4, 0, UINT16_C(0x0022), 401),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKING);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 5, 0, UINT16_C(0x0033), 402),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 5, 0, UINT16_C(0x0044), 403),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmpuint(data_ready_count, ==, 3);
    master_status = 0;
    slave_status = 0;
}

static void test_regular_interleaved_cdr2(void)
{
    DmMc02AdcCommon state;

    init_common(&state);
    data_ready_count = 0;
    cdr2_data_ready_count = 0;
    cdr2_read_count = 0;
    dm_mc02_adc_common_set_data_ready_callback(&state, data_ready, NULL);
    dm_mc02_adc_common_set_cdr2_data_ready_callback(
        &state, cdr2_data_ready, NULL);
    dm_mc02_adc_common_set_cdr2_read_callback(&state, cdr2_read, NULL);

    /* DUAL=0x7 is regular interleaved.  CDR2 carries one latest result,
     * while the CDR pair/DMA callback remains inactive. */
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED, 4);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 0, 0, UINT16_C(0x1234), 100),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, UINT32_C(0x00001234));
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, 0);
    g_assert_cmpuint(data_ready_count, ==, 0);
    g_assert_cmpuint(cdr2_data_ready_count, ==, 0);

    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 77, 4, UINT16_C(0xabcd), 200),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, UINT32_C(0x0000abcd));

    /* The combined regular-interleaved + injected-simultaneous mode uses the
     * same CDR2 regular-data path.  DAMDF=2 additionally enables the DMA
     * producer callback. */
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT, 4);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 0, 1, UINT16_C(0x0055), 300),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, UINT32_C(0x00000055));

    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 0, 0, UINT16_C(0x9999), 400),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpuint(cdr2_data_ready_count, ==, 1);
    g_assert_cmphex(cdr2_data_ready_value, ==, UINT32_C(0x9999));
    g_assert_cmpuint(cdr2_data_ready_source, ==, 0);
    g_assert_cmpuint(cdr2_data_ready_timestamp_ns, ==, 400);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, UINT32_C(0x00009999));
    g_assert_cmpuint(cdr2_read_count, ==, 1);
    g_assert_cmpuint(cdr2_read_source, ==, 0);

    /* A second producer overwrites the one-entry CDR2 register and reports
     * its own source.  The read acknowledgement follows that source. */
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 0, 0, UINT16_C(0x8888), 500),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpuint(cdr2_data_ready_count, ==, 2);
    g_assert_cmpuint(cdr2_data_ready_source, ==, 1);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, UINT32_C(0x00008888));
    g_assert_cmpuint(cdr2_read_count, ==, 2);
    g_assert_cmpuint(cdr2_read_source, ==, 1);

    /* DAMDF=1 remains outside this CDR2 DMA boundary. */
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED |
                 (UINT32_C(1) << 14), 4);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 0, 0, UINT16_C(0x7777), 600),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_UNSUPPORTED);
    g_assert_cmpuint(cdr2_data_ready_count, ==, 2);

    /* DAMDF=3 uses CDR, not CDR2, and waits for four alternating 8-bit
     * values before emitting one 32-bit word. */
    data_ready_count = 0;
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_8 << 14), 4);
    /* Deliberately make the fourth callback timestamp older than the first
     * pair.  The published word must retain the maximum timestamp covered
     * by all four producer events rather than move backwards. */
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 0, 0, UINT16_C(0x01a1), 1000),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 0, 0, UINT16_C(0x02b2), 900),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 1, 0, UINT16_C(0x03c3), 1100),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 1, 0, UINT16_C(0x04d4), 950),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmpuint(data_ready_count, ==, 1);
    g_assert_cmphex(data_ready_value, ==, UINT32_C(0xd4c3b2a1));
    g_assert_cmpuint(data_ready_timestamp_ns, ==, 1100);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, UINT32_C(0x00008888));

    /* A source-order violation drops the partial word and does not publish
     * a synthetic CDR result. */
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 2, 0, UINT16_C(0x0055), 704),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 0, 2, 0, UINT16_C(0x0066), 705),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_MISMATCH);
    g_assert_cmpuint(data_ready_count, ==, 1);

    dm_mc02_adc_common_reset(&state);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, 0);
}

static void test_cdr_read_callback_boundary(void)
{
    DmMc02AdcCommon state;

    init_common(&state);
    cdr_read_count = 0;
    dm_mc02_adc_common_set_cdr_read_callback(&state, cdr_read, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);
    dm_mc02_adc_common_set_data(&state, UINT32_C(0x11223344), 0);

    /* Any valid access overlapping CDR is a read of the common data
     * register.  CDR2 is a separate register and has no CDR acknowledgement
     * side effect. */
    g_assert_cmphex(common_read(&state,
                                DM_MC02_ADC_COMMON_CDR_OFFSET + 1, 1),
                    ==, 0x33);
    g_assert_cmpuint(cdr_read_count, ==, 1);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR2_OFFSET, 4),
                    ==, 0);
    g_assert_cmpuint(cdr_read_count, ==, 1);

    /* The callback is runtime wiring and survives a component reset.  A
     * supported register address alone is not a producer item, so an empty
     * CDR cannot acknowledge unrelated ADC EOC flags. */
    dm_mc02_adc_common_reset(&state);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_8 << 14), 4);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, 0);
    g_assert_cmpuint(cdr_read_count, ==, 1);

    /* DAMDF=3 interleaved data is also delivered through CDR and therefore
     * has the same read acknowledgement boundary. */
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_8 << 14), 4);
    (void)common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4);
    g_assert_cmpuint(cdr_read_count, ==, 1);

    /* Unsupported/unused CDR formats do not acknowledge unrelated ADCs. */
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED, 4);
    (void)common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4);
    g_assert_cmpuint(cdr_read_count, ==, 1);
}

static void test_cdr_dma_retry_admission(void)
{
    DmMc02AdcCommon state;

    init_common(&state);
    cdr_read_count = 0;
    dm_mc02_adc_common_set_cdr_read_callback(&state, cdr_read, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 6u | (2u << 14), 4);
    for (unsigned mask = 1; mask <= 3; ++mask) {
        dm_mc02_adc_common_set_data(&state, 0x12345678, 0);
        master_status = (mask & 1) ? 1u << 4 : 0;
        slave_status = (mask & 2) ? 1u << 4 : 0;
        g_assert_true(dm_mc02_adc_common_cdr_data_pending(&state));
        g_assert_false(dm_mc02_adc_common_cdr_dma_request_pending(&state));
        /* Clearing OVR admits the retained word, without a new sample. */
        master_status = slave_status = 0;
        g_assert_true(dm_mc02_adc_common_cdr_dma_request_pending(&state));
        master_status = (mask & 1) ? 1u << 4 : 0;
        slave_status = (mask & 2) ? 1u << 4 : 0;
        g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                        ==, 0x12345678);
        g_assert_cmpuint(cdr_read_count, ==, mask);
        g_assert_false(dm_mc02_adc_common_cdr_data_pending(&state));
    }
}

static void test_common_consuming_reads(void)
{
    DmMc02AdcCommon state;
    uint8_t data[4];

    init_common(&state);
    cdr_read_count = cdr2_read_count = 0;
    dm_mc02_adc_common_set_cdr_read_callback(&state, cdr_read, NULL);
    dm_mc02_adc_common_set_cdr2_read_callback(&state, cdr2_read, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, 6u | (2u << 14), 4);
    dm_mc02_adc_common_set_data(&state, 0x11223344, 0);
    g_assert_false(dm_mc02_adc_common_cdr_read_consuming(&state, data, 3));
    g_assert_true(dm_mc02_adc_common_cdr_read_prepare(&state, data, 4));
    g_assert_false(dm_mc02_adc_common_cdr_read_consuming(&state, data, 4));
    dm_mc02_adc_common_cdr_read_abort(&state);
    g_assert_true(dm_mc02_adc_common_cdr_read_consuming(&state, data, 4));
    g_assert_cmpmem(data, 4, "\x44\x33\x22\x11", 4);
    g_assert_cmpuint(cdr_read_count, ==, 1);
    g_assert_false(dm_mc02_adc_common_cdr_data_pending(&state));
    g_assert_false(dm_mc02_adc_common_cdr_read_consuming(&state, data, 4));

    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET, 7u | (2u << 14), 4);
    for (unsigned source = 0; source < 2; ++source) {
        dm_mc02_adc_common_submit_regular_sample(&state, source, 1, 0,
                                                 0x5678, 100);
        g_assert_false(dm_mc02_adc_common_cdr2_read_consuming(&state, NULL, 4));
        g_assert_true(dm_mc02_adc_common_cdr2_read_prepare(&state, data, 4));
        g_assert_false(dm_mc02_adc_common_cdr2_read_consuming(&state, data, 4));
        dm_mc02_adc_common_cdr2_read_abort(&state);
        g_assert_true(dm_mc02_adc_common_cdr2_read_consuming(&state, data, 4));
        g_assert_cmpmem(data, 4, "\x78\x56\x00\x00", 4);
        g_assert_cmpuint(cdr2_read_count, ==, source + 1);
        g_assert_cmpuint(cdr2_read_source, ==, source);
        g_assert_false(dm_mc02_adc_common_cdr2_data_pending(&state));
        g_assert_false(state.cdr2_read_reserved);
    }
}

static void test_cdr_read_reservation_boundary(void)
{
    DmMc02AdcCommon state;
    uint8_t data[sizeof(uint32_t)] = { 0 };
    QEMUFile *file;

    init_common(&state);
    cdr_read_count = 0;
    dm_mc02_adc_common_set_cdr_read_callback(&state, cdr_read, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);
    dm_mc02_adc_common_set_data(&state, UINT32_C(0x11223344), 0);
    g_assert_true(dm_mc02_adc_common_cdr_data_pending(&state));

    /* Prepare is a source borrow.  An overlapping CPU CDR read cannot steal
     * it, and abort leaves exactly the same word for the next DMA request. */
    g_assert_true(dm_mc02_adc_common_cdr_read_prepare(&state, data, 4));
    g_assert_cmpmem(data, sizeof(data), "\x44\x33\x22\x11", sizeof(data));
    g_assert_true(state.cdr_read_reserved);
    g_assert_false(dm_mc02_adc_common_cdr_data_pending(&state));
    /* The synchronous reservation must not leak across either normal or raw
     * component serialization. */
    file = open_test_file(true);
    g_assert_cmpint(vmstate_save_state(file, dm_mc02_adc_common_vmstate(),
                                       &state, NULL), ==, -EINVAL);
    qemu_fclose(file);
    file = open_test_file(true);
    g_assert_cmpint(vmstate_save_state(file, &vmstate_dm_mc02_adc_common_raw,
                                       &state, NULL), ==, -EINVAL);
    qemu_fclose(file);
    g_assert_cmphex(common_read(&state, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0x11223344));
    g_assert_cmpuint(cdr_read_count, ==, 0);
    dm_mc02_adc_common_cdr_read_abort(&state);
    g_assert_false(state.cdr_read_reserved);
    g_assert_true(dm_mc02_adc_common_cdr_data_pending(&state));
    g_assert_cmpuint(cdr_read_count, ==, 0);

    g_assert_true(dm_mc02_adc_common_cdr_read_prepare(&state, data, 2));
    g_assert_cmpuint(data[0], ==, 0x44);
    g_assert_cmpuint(data[1], ==, 0x33);
    dm_mc02_adc_common_cdr_read_commit(&state);
    g_assert_false(state.cdr_read_reserved);
    g_assert_false(dm_mc02_adc_common_cdr_data_pending(&state));
    g_assert_cmpuint(cdr_read_count, ==, 1);
    g_assert_false(dm_mc02_adc_common_cdr_read_prepare(&state, data, 4));
}

static void test_cdr2_read_reservation_boundary(void)
{
    DmMc02AdcCommon state;
    uint8_t data[sizeof(uint32_t)] = { 0 };
    QEMUFile *file;

    init_common(&state);
    cdr2_read_count = 0;
    cdr2_read_source = 0;
    dm_mc02_adc_common_set_cdr2_read_callback(&state, cdr2_read, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &state, 1, 0, 0, UINT16_C(0xbeef), 900),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_true(dm_mc02_adc_common_cdr2_data_pending(&state));

    /* Prepare borrows the result and records its producer without
     * acknowledging that producer.  A re-entrant CDR2 read sees the value
     * but cannot consume the reservation. */
    g_assert_true(dm_mc02_adc_common_cdr2_read_prepare(&state, data, 4));
    g_assert_cmpmem(data, sizeof(data), "\xef\xbe\x00\x00", sizeof(data));
    g_assert_true(state.cdr2_read_reserved);
    g_assert_cmpuint(state.cdr2_read_reserved_source, ==, 1);
    g_assert_false(dm_mc02_adc_common_cdr2_data_pending(&state));
    g_assert_cmphex(common_read(&state,
                                DM_MC02_ADC_COMMON_CDR2_OFFSET, 4), ==,
                    UINT32_C(0x0000beef));
    g_assert_cmpuint(cdr2_read_count, ==, 0);

    /* Neither component representation may cross a live source reservation. */
    file = open_test_file(true);
    g_assert_cmpint(vmstate_save_state(file, dm_mc02_adc_common_vmstate(),
                                       &state, NULL), ==, -EINVAL);
    qemu_fclose(file);
    file = open_test_file(true);
    g_assert_cmpint(vmstate_save_state(file, &vmstate_dm_mc02_adc_common_raw,
                                       &state, NULL), ==, -EINVAL);
    qemu_fclose(file);

    dm_mc02_adc_common_cdr2_read_abort(&state);
    g_assert_false(state.cdr2_read_reserved);
    g_assert_true(dm_mc02_adc_common_cdr2_data_pending(&state));
    g_assert_cmpuint(cdr2_read_count, ==, 0);

    g_assert_true(dm_mc02_adc_common_cdr2_read_prepare(&state, data, 2));
    g_assert_cmpuint(data[0], ==, 0xef);
    g_assert_cmpuint(data[1], ==, 0xbe);
    dm_mc02_adc_common_cdr2_read_commit(&state);
    g_assert_false(state.cdr2_read_reserved);
    g_assert_false(dm_mc02_adc_common_cdr2_data_pending(&state));
    g_assert_cmpuint(cdr2_read_count, ==, 1);
    g_assert_cmpuint(cdr2_read_source, ==, 1);
    g_assert_false(dm_mc02_adc_common_cdr2_read_prepare(&state, data, 4));
}

static void test_regular_start_admission(void)
{
    DmMc02AdcCommon state;

    init_common(&state);
    regular_start_peer_count = 0;
    regular_start_peer_result = true;
    dm_mc02_adc_common_set_regular_start_peer(
        &state, regular_start_peer, NULL);

    /* Independent mode does not impose a master/slave start rule. */
    g_assert_true(dm_mc02_adc_common_admit_regular_start(&state, 1));
    g_assert_cmpuint(regular_start_peer_count, ==, 0);

    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS, 4);
    g_assert_false(dm_mc02_adc_common_admit_regular_start(&state, 1));
    g_assert_cmpuint(regular_start_peer_count, ==, 0);

    g_assert_true(dm_mc02_adc_common_admit_regular_start(&state, 0));
    g_assert_cmpuint(regular_start_peer_count, ==, 1);
    g_assert_cmpuint(regular_start_peer_source, ==, 1);
    g_assert_cmpuint(regular_start_peer_conversion_id, ==, 1);

    regular_start_peer_result = false;
    g_assert_false(dm_mc02_adc_common_admit_regular_start(&state, 0));
    g_assert_cmpuint(regular_start_peer_count, ==, 2);
    g_assert_cmpuint(regular_start_peer_conversion_id, ==, 2);

    /* Interleaved modes use the same master-only admission boundary, but
     * expose DELAY to the board composition so the ADC kernel can resolve its
     * virtual sampling phase. */
    regular_start_peer_result = true;
    dm_mc02_adc_common_reset(&state);
    dm_mc02_adc_common_set_regular_start_peer(
        &state, regular_start_peer, NULL);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED |
                 (2u << 8), 4);
    g_assert_true(dm_mc02_adc_common_regular_interleaved(&state));
    g_assert_cmpuint(dm_mc02_adc_common_get_dual(&state), ==,
                     DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED);
    g_assert_cmpuint(dm_mc02_adc_common_get_delay_code(&state), ==, 2);
    g_assert_true(dm_mc02_adc_common_admit_regular_start(&state, 0));
    g_assert_cmpuint(regular_start_peer_count, ==, 3);
    g_assert_false(dm_mc02_adc_common_admit_regular_start(&state, 1));

    dm_mc02_adc_common_reset(&state);
    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT,
                 4);
    g_assert_true(dm_mc02_adc_common_regular_interleaved(&state));
    g_assert_true(dm_mc02_adc_common_admit_regular_start(&state, 0));
    g_assert_false(dm_mc02_adc_common_admit_regular_start(&state, 1));
}

static void test_external_trigger_admission(void)
{
    DmMc02AdcCommon state;
    uint64_t conversion_id;

    init_common(&state);
    external_trigger_peer_count = 0;
    external_trigger_peer_result = true;
    dm_mc02_adc_common_set_external_trigger_peer(
        &state, external_trigger_peer, NULL);

    /* Independent modes stay on the ADC-local trigger path. */
    conversion_id = 99;
    g_assert_true(dm_mc02_adc_common_admit_external_trigger(
        &state, 1, 7, true, 1, 100, &conversion_id));
    g_assert_cmpuint(conversion_id, ==, 0);
    g_assert_cmpuint(external_trigger_peer_count, ==, 0);

    common_write(&state, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS, 4);
    conversion_id = 99;
    g_assert_false(dm_mc02_adc_common_admit_external_trigger(
        &state, 1, 7, true, 2, 200, &conversion_id));
    g_assert_cmpuint(external_trigger_peer_count, ==, 0);

    /* The master owns the edge and asks the composition to start the slave. */
    g_assert_true(dm_mc02_adc_common_admit_external_trigger(
        &state, 0, 7, true, 2, 200, &conversion_id));
    g_assert_cmpuint(conversion_id, ==, 1);
    g_assert_cmpuint(external_trigger_peer_count, ==, 1);
    g_assert_cmpuint(external_trigger_peer_source, ==, 1);
    g_assert_cmpuint(external_trigger_peer_trigger_source, ==, 7);
    g_assert_true(external_trigger_peer_rising);
    g_assert_cmpuint(external_trigger_peer_event_count, ==, 2);
    g_assert_cmpuint(external_trigger_peer_timestamp_ns, ==, 200);
    g_assert_cmpuint(external_trigger_peer_conversion_id, ==, 1);

    external_trigger_peer_result = false;
    conversion_id = 99;
    g_assert_false(dm_mc02_adc_common_admit_external_trigger(
        &state, 0, 7, false, 1, 300, &conversion_id));
    g_assert_cmpuint(conversion_id, ==, 0);
    g_assert_cmpuint(external_trigger_peer_count, ==, 2);

    /* A peer admission failure must not publish an ID that the producer could
     * later use as a valid common conversion.  The generator may advance to
     * keep IDs monotonic, but the caller-visible result remains invalid. */
    external_trigger_peer_result = true;
    g_assert_true(dm_mc02_adc_common_admit_external_trigger(
        &state, 0, 7, true, 1, 400, &conversion_id));
    g_assert_cmpuint(conversion_id, ==, 3);
    external_trigger_peer_result = false;
    conversion_id = 99;
    g_assert_false(dm_mc02_adc_common_admit_external_trigger(
        &state, 0, 7, true, 1, 500, &conversion_id));
    g_assert_cmpuint(conversion_id, ==, 0);
    g_assert_cmpuint(external_trigger_peer_count, ==, 4);
}

static void test_vmstate_round_trip_preserves_pending_pair(void)
{
    DmMc02AdcCommon source;
    DmMc02AdcCommon restored;

    init_common(&source);
    common_write(&source, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);
    source.next_conversion_id = 7;
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &source, 0, 7, 2, UINT16_C(0x1234), 700),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    save_state(&source);

    init_common(&restored);
    clock_callback_count = 0;
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_true(restored.master_sample_valid);
    g_assert_false(restored.slave_sample_valid);
    g_assert_cmpuint(restored.master_sample_conversion_id, ==, 7);
    g_assert_cmpuint(restored.master_sample_rank, ==, 2);
    g_assert_cmphex(restored.master_sample_value, ==, 0x1234);
    g_assert_cmpuint(restored.master_sample_timestamp_ns, ==, 700);

    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &restored, 1, 7, 2, UINT16_C(0xabcd), 700),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmphex(common_read(&restored, DM_MC02_ADC_COMMON_CDR_OFFSET, 4),
                    ==, UINT32_C(0xabcd1234));
}

static void test_vmstate_round_trip_and_validation(void)
{
    DmMc02AdcCommon source;
    DmMc02AdcCommon restored;
    QEMUFile *file;
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    init_common(&source);
    source.ccr = UINT32_C(0x002c4005);
    source.cdr = UINT32_C(0x12345678);
    source.cdr_valid = true;
    source.cdr2 = UINT32_C(0x9abcdef0);
    source.ccr_configured = true;
    source.next_conversion_id = 42;
    save_state(&source);

    init_common(&restored);
    clock_callback_count = 0;
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpuint(restored.ccr, ==, source.ccr);
    g_assert_cmpuint(restored.cdr, ==, source.cdr);
    g_assert_true(restored.cdr_valid);
    g_assert_cmpuint(restored.cdr2, ==, source.cdr2);
    g_assert_true(restored.ccr_configured);
    g_assert_cmpuint(restored.next_conversion_id, ==, 42);
    g_assert_cmpuint(clock_callback_count, ==, 1);
    g_assert_cmphex(clock_callback_ccr, ==, source.ccr);

    source.ccr = DM_MC02_ADC_COMMON_CCR_WRITABLE_MASK | UINT32_C(0x80000000);
    save_state(&source);
    init_common(&restored);
    clock_callback_count = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_cmpuint(clock_callback_count, ==, 0);

    file = open_test_file(true);
    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    init_common(&restored);
    clock_callback_count = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_cmpuint(clock_callback_count, ==, 0);
}

static void test_vmstate_round_trip_preserves_cdr2_source(void)
{
    DmMc02AdcCommon source;
    DmMc02AdcCommon restored;

    init_common(&source);
    common_write(&source, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &source, 1, 0, 0, UINT16_C(0xbeef), 900),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_true(source.cdr2_valid);
    save_state(&source);

    init_common(&restored);
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_true(restored.cdr2_valid);
    g_assert_cmpuint(restored.cdr2_source, ==, 1);
    g_assert_cmphex(restored.cdr2, ==, UINT32_C(0x0000beef));
}

static void test_vmstate_round_trip_preserves_damdf8_progress(void)
{
    DmMc02AdcCommon source;
    DmMc02AdcCommon restored;

    init_common(&source);
    common_write(&source, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_8 << 14), 4);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &source, 0, 0, 0, UINT16_C(0x11a1), 100),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &source, 1, 0, 0, UINT16_C(0x22b2), 101),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpuint(source.damdf8_count, ==, 2);
    g_assert_cmphex(source.damdf8_data, ==, UINT32_C(0x0000b2a1));
    g_assert_cmpuint(source.damdf8_timestamp_ns, ==, 101);
    save_state(&source);

    init_common(&restored);
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpuint(restored.damdf8_count, ==, 2);
    g_assert_cmpuint(restored.damdf8_next_source, ==, 0);
    g_assert_cmphex(restored.damdf8_data, ==, UINT32_C(0x0000b2a1));
    g_assert_cmpuint(restored.damdf8_timestamp_ns, ==, 101);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &restored, 0, 1, 0, UINT16_C(0x33c3), 102),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED);
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &restored, 1, 1, 0, UINT16_C(0x44d4), 103),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PACKED);
    g_assert_cmphex(restored.cdr, ==, UINT32_C(0xd4c3b2a1));
}

static void test_vmstate_v1_compatibility(void)
{
    DmMc02AdcCommon source;
    DmMc02AdcCommon restored;

    init_common(&source);
    common_write(&source, DM_MC02_ADC_COMMON_CCR_OFFSET,
                 DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS |
                 (DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 << 14), 4);
    source.next_conversion_id = 99;
    g_assert_cmpint(dm_mc02_adc_common_submit_regular_sample(
                        &source, 0, 7, 2, UINT16_C(0x1234), 700),
                    ==, DM_MC02_ADC_COMMON_SAMPLE_PENDING);
    save_state_with_desc(&source, &vmstate_dm_mc02_adc_common_v1_test);

    init_common(&restored);
    g_assert_cmpint(load_state_version(&restored, 1), ==, 0);
    g_assert_cmpuint(restored.next_conversion_id, ==, 7);
    g_assert_true(restored.master_sample_valid);
    g_assert_cmpuint(restored.master_sample_conversion_id, ==, 7);
    g_assert_cmpuint(restored.master_sample_rank, ==, 2);
    g_assert_cmphex(restored.master_sample_value, ==, 0x1234);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf(
        "%s/dm-adc-common.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-adc-common/ccr-access-widths-and-mask",
                    test_ccr_access_widths_and_mask);
    g_test_add_func("/dm-adc-common/status-and-read-only-data",
                    test_status_projection_and_read_only_data);
    g_test_add_func("/dm-adc-common/reset-keeps-wiring",
                    test_reset_clears_state_but_keeps_wiring);
    g_test_add_func("/dm-adc-common/regular-pair-packing",
                    test_regular_pair_packing_and_rejection);
    g_test_add_func("/dm-adc-common/regular-pair-data-ready",
                    test_regular_pair_data_ready_callback);
    g_test_add_func("/dm-adc-common/consuming-reads",
                    test_common_consuming_reads);
    g_test_add_func("/dm-adc-common/cdr-dma-retry-admission",
                    test_cdr_dma_retry_admission);
    g_test_add_func("/dm-adc-common/regular-pair-dma-overrun-gate",
                    test_regular_pair_dma_overrun_gate);
    g_test_add_func("/dm-adc-common/regular-interleaved-cdr2",
                    test_regular_interleaved_cdr2);
    g_test_add_func("/dm-adc-common/cdr-read-callback",
                    test_cdr_read_callback_boundary);
    g_test_add_func("/dm-adc-common/cdr-read-reservation",
                    test_cdr_read_reservation_boundary);
    g_test_add_func("/dm-adc-common/cdr2-read-reservation",
                    test_cdr2_read_reservation_boundary);
    g_test_add_func("/dm-adc-common/regular-start-admission",
                    test_regular_start_admission);
    g_test_add_func("/dm-adc-common/external-trigger-admission",
                    test_external_trigger_admission);
    g_test_add_func("/dm-adc-common/vmstate-pending-pair",
                    test_vmstate_round_trip_preserves_pending_pair);
    g_test_add_func("/dm-adc-common/vmstate-round-trip-validation",
                    test_vmstate_round_trip_and_validation);
    g_test_add_func("/dm-adc-common/vmstate-cdr2-source",
                    test_vmstate_round_trip_preserves_cdr2_source);
    g_test_add_func("/dm-adc-common/vmstate-damdf8-progress",
                    test_vmstate_round_trip_preserves_damdf8_progress);
    g_test_add_func("/dm-adc-common/vmstate-v1-compatibility",
                    test_vmstate_v1_compatibility);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
