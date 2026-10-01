/* Focused VMState contract test for the board-independent DWC2 device core. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_dwc2_device.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

typedef struct Dwc2VmstateFixture {
    DmUsbControlDevice control;
    DmUsbDwc2Device device;
    unsigned irq_calls;
    bool irq_level;
} Dwc2VmstateFixture;

static int temp_fd;

static void fixture_irq(void *opaque, bool level)
{
    Dwc2VmstateFixture *fixture = opaque;

    fixture->irq_calls++;
    fixture->irq_level = level;
}

static void fixture_init(Dwc2VmstateFixture *fixture)
{
    memset(fixture, 0, sizeof(*fixture));
    dm_usb_control_init(&fixture->control, NULL, fixture, 4);
    dm_usb_dwc2_init(&fixture->device, &fixture->control, fixture_irq,
                     fixture, 4);
}

static uint32_t endpoint_offset(uint32_t base, unsigned endpoint)
{
    return base + endpoint * DM_USB_DWC2_EP_STRIDE;
}

static uint32_t endpoint_control(void)
{
    return DM_USB_DWC2_DXEPCTL_USBAEP |
           (2u << 18) |
           4u |
           DM_USB_DWC2_DXEPCTL_EPENA;
}

static uint32_t endpoint_transfer_size(size_t length, unsigned packets)
{
    return (uint32_t)length |
           ((uint32_t)packets << DM_USB_DXEPTSIZ_PKTCNT_SHIFT);
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

static void save_state(const DmUsbDwc2Device *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_usb_dwc2_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmUsbDwc2Device *state, int version_id)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_usb_dwc2_vmstate(), state,
                                 version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void assert_runtime_wiring_unchanged(
    const Dwc2VmstateFixture *fixture,
    DmUsbControlDevice *control,
    DmUsbTransactionOps ops,
    void *transaction_opaque,
    DmUsbDwc2Irq *irq,
    void *irq_opaque,
    bool irq_level)
{
    g_assert_true(fixture->device.control == control);
    g_assert_true(fixture->device.transaction.control == control);
    g_assert_true(fixture->device.transaction.ops.in == ops.in);
    g_assert_true(fixture->device.transaction.ops.out == ops.out);
    g_assert_true(fixture->device.transaction.opaque == transaction_opaque);
    g_assert_true(fixture->device.irq == irq);
    g_assert_true(fixture->device.irq_opaque == irq_opaque);
    g_assert_cmpint(fixture->device.irq_level, ==, irq_level);
}

static void prepare_source(Dwc2VmstateFixture *fixture)
{
    static const uint8_t out_data[] = { 'x', 'y', 'z' };
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_OUT,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = 2,
        .out_data = out_data,
        .length = sizeof(out_data),
        .timestamp_ns = 1234,
    };
    DmUsbTransactionResult result;

    fixture_init(fixture);
    dm_usb_dwc2_write(&fixture->device,
                      endpoint_offset(DM_USB_DWC2_DIEPCTL0, 1),
                      endpoint_control(), 4);
    dm_usb_dwc2_write(&fixture->device,
                      endpoint_offset(DM_USB_DWC2_DIEPTSIZ0, 1),
                      endpoint_transfer_size(3, 1), 4);
    dm_usb_dwc2_write(&fixture->device, DM_USB_DWC2_FIFO0 +
                      DM_USB_DWC2_FIFO_STRIDE, 0x00004241, 2);
    dm_usb_dwc2_write(&fixture->device, DM_USB_DWC2_FIFO0 +
                      DM_USB_DWC2_FIFO_STRIDE, 0x00000043, 1);

    dm_usb_dwc2_write(&fixture->device,
                      endpoint_offset(DM_USB_DWC2_DOEPCTL0, 2),
                      endpoint_control(), 4);
    dm_usb_dwc2_write(&fixture->device,
                      endpoint_offset(DM_USB_DWC2_DOEPTSIZ0, 2),
                      endpoint_transfer_size(8, 2), 4);
    result = dm_usb_dwc2_submit(&fixture->device, &transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(out_data));
    g_assert_cmpint(dm_usb_transaction_next_pid(
                        &fixture->device.transaction, 2,
                        DM_USB_ENDPOINT_OUT), ==,
                    DM_USB_TRANSACTION_PID_DATA1);

    dm_usb_dwc2_write(&fixture->device, DM_USB_DWC2_GAHBCFG,
                      DM_USB_DWC2_GAHBCFG_GINT, 4);
    dm_usb_dwc2_write(&fixture->device, DM_USB_DWC2_GINTMSK,
                      DM_USB_DWC2_GINTSTS_IEPINT |
                      DM_USB_DWC2_GINTSTS_OEPINT, 4);
    dm_usb_dwc2_write(&fixture->device, DM_USB_DWC2_DIEPMSK,
                      DM_USB_DXEPINT_XFRC, 4);
    dm_usb_dwc2_write(&fixture->device, DM_USB_DWC2_DOEPMSK,
                      DM_USB_DXEPINT_XFRC, 4);
    dm_usb_dwc2_write(&fixture->device, DM_USB_DWC2_DAINTMSK,
                      (1u << 1) | (1u << (16 + 2)), 4);
    fixture->device.dcfg = UINT32_C(0x00000152);
    fixture->device.dctl = UINT32_C(0x00000011);
    fixture->device.dsts = UINT32_C(0x00000044);
    fixture->device.dieptxf[0] = UINT32_C(0x00100020);
    fixture->device.fifo_overflow = 7;
}

static void test_round_trip_preserves_transfer_and_runtime(void)
{
    Dwc2VmstateFixture source;
    Dwc2VmstateFixture restored;
    DmUsbTransaction in_transaction = {
        .token = DM_USB_TRANSACTION_IN,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = 1,
        .in_data = NULL,
        .capacity = 3,
        .timestamp_ns = 2000,
    };
    DmUsbTransaction out_transaction = {
        .token = DM_USB_TRANSACTION_OUT,
        .pid = DM_USB_TRANSACTION_PID_DATA1,
        .endpoint = 2,
        .out_data = (const uint8_t *)"DE",
        .length = 2,
        .timestamp_ns = 2001,
    };
    uint8_t in_data[3] = { 0 };
    size_t count;
    DmUsbTransactionResult result;

    prepare_source(&source);
    save_state(&source.device);

    fixture_init(&restored);
    restored.device.irq_level = true;
    g_assert_cmpint(load_state(&restored.device, 1), ==, 0);

    g_assert_true(restored.device.control == &restored.control);
    g_assert_true(restored.device.transaction.control == &restored.control);
    g_assert_true(restored.device.transaction.opaque == &restored.device);
    g_assert_cmpuint(restored.device.dcfg, ==, source.device.dcfg);
    g_assert_cmpuint(restored.device.dieptxf[0], ==,
                     source.device.dieptxf[0]);
    g_assert_cmpuint(restored.device.fifo_overflow, ==,
                     source.device.fifo_overflow);
    g_assert_true(dm_usb_dwc2_endpoint_fifo_count(&restored.device, 1, true,
                                                  &count));
    g_assert_cmpuint(count, ==, 3);
    g_assert_true(dm_usb_dwc2_endpoint_fifo_count(&restored.device, 2, false,
                                                  &count));
    g_assert_cmpuint(count, ==, 3);
    g_assert_cmpint(dm_usb_transaction_next_pid(
                        &restored.device.transaction, 2,
                        DM_USB_ENDPOINT_OUT), ==,
                    DM_USB_TRANSACTION_PID_DATA1);
    g_assert_cmpuint(restored.irq_calls, ==, 1);
    g_assert_false(restored.irq_level);

    in_transaction.in_data = in_data;
    result = dm_usb_dwc2_submit(&restored.device, &in_transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 3);
    g_assert_cmpmem(in_data, sizeof(in_data), "ABC", 3);
    g_assert_true(restored.irq_level);

    result = dm_usb_dwc2_submit(&restored.device, &out_transaction);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 2);
    g_assert_cmpint(dm_usb_transaction_next_pid(
                        &restored.device.transaction, 2,
                        DM_USB_ENDPOINT_OUT), ==,
                    DM_USB_TRANSACTION_PID_DATA0);
    g_assert_true(dm_usb_dwc2_endpoint_fifo_count(&restored.device, 2, false,
                                                  &count));
    g_assert_cmpuint(count, ==, 5);
}

static void test_rejects_invalid_fifo_without_runtime_sync(void)
{
    Dwc2VmstateFixture source;
    Dwc2VmstateFixture restored;
    DmUsbControlDevice *control;
    DmUsbTransactionOps ops;
    void *transaction_opaque;
    DmUsbDwc2Irq *irq;
    void *irq_opaque;

    prepare_source(&source);
    source.device.endpoint[1].in_fifo_count = DM_USB_DWC2_FIFO_BYTES + 1;
    save_state(&source.device);

    fixture_init(&restored);
    restored.device.irq_level = true;
    control = restored.device.control;
    ops = restored.device.transaction.ops;
    transaction_opaque = restored.device.transaction.opaque;
    irq = restored.device.irq;
    irq_opaque = restored.device.irq_opaque;
    g_assert_cmpint(load_state(&restored.device, 1), !=, 0);
    assert_runtime_wiring_unchanged(&restored, control, ops,
                                    transaction_opaque, irq, irq_opaque,
                                    true);
    g_assert_cmpuint(restored.irq_calls, ==, 0);
}

static void test_rejects_invalid_pid_without_runtime_sync(void)
{
    Dwc2VmstateFixture source;
    Dwc2VmstateFixture restored;
    DmUsbControlDevice *control;
    DmUsbTransactionOps ops;
    void *transaction_opaque;
    DmUsbDwc2Irq *irq;
    void *irq_opaque;

    prepare_source(&source);
    source.device.transaction.endpoint[2].next_out_pid =
        DM_USB_TRANSACTION_PID_AUTO;
    save_state(&source.device);

    fixture_init(&restored);
    control = restored.device.control;
    ops = restored.device.transaction.ops;
    transaction_opaque = restored.device.transaction.opaque;
    irq = restored.device.irq;
    irq_opaque = restored.device.irq_opaque;
    g_assert_cmpint(load_state(&restored.device, 1), !=, 0);
    assert_runtime_wiring_unchanged(&restored, control, ops,
                                    transaction_opaque, irq, irq_opaque,
                                    false);
    g_assert_cmpuint(restored.irq_calls, ==, 0);
}

static void test_rejects_unsupported_version_without_runtime_sync(void)
{
    Dwc2VmstateFixture source;
    Dwc2VmstateFixture restored;
    DmUsbControlDevice *control;
    DmUsbTransactionOps ops;
    void *transaction_opaque;
    DmUsbDwc2Irq *irq;
    void *irq_opaque;

    prepare_source(&source);
    save_state(&source.device);
    fixture_init(&restored);
    control = restored.device.control;
    ops = restored.device.transaction.ops;
    transaction_opaque = restored.device.transaction.opaque;
    irq = restored.device.irq;
    irq_opaque = restored.device.irq_opaque;
    g_assert_cmpint(load_state(&restored.device, 2), !=, 0);
    assert_runtime_wiring_unchanged(&restored, control, ops,
                                    transaction_opaque, irq, irq_opaque,
                                    false);
    g_assert_cmpuint(restored.irq_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file =
        g_strdup_printf("%s/dm-usb-dwc2-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/dwc2-vmstate/round-trip-runtime",
                    test_round_trip_preserves_transfer_and_runtime);
    g_test_add_func("/dm-usb/dwc2-vmstate/reject-invalid-fifo",
                    test_rejects_invalid_fifo_without_runtime_sync);
    g_test_add_func("/dm-usb/dwc2-vmstate/reject-invalid-pid",
                    test_rejects_invalid_pid_without_runtime_sync);
    g_test_add_func("/dm-usb/dwc2-vmstate/reject-version",
                    test_rejects_unsupported_version_without_runtime_sync);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
