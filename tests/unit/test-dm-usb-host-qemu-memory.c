/* Tests for the QEMU AddressSpace USB host DMA binding. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_qemu_memory.h"

typedef struct TestQemuMemory {
    AddressSpace address_space;
    DmUsbHostQemuMemory memory;
    uint8_t storage[32];
    MemTxResult result;
    hwaddr address;
    hwaddr length;
    bool is_write;
} TestQemuMemory;

static TestQemuMemory *current_test;

MemTxResult address_space_rw(AddressSpace *as, hwaddr address,
                             MemTxAttrs attrs, void *buffer,
                             hwaddr length, bool is_write)
{
    TestQemuMemory *test = current_test;

    g_assert_true(as == &test->address_space);
    test->address = address;
    test->length = length;
    test->is_write = is_write;
    if (test->result != MEMTX_OK) {
        return test->result;
    }
    if (address > sizeof(test->storage) ||
        length > sizeof(test->storage) - address) {
        return MEMTX_DECODE_ERROR;
    }
    if (is_write) {
        memcpy(&test->storage[address], buffer, length);
    } else {
        memcpy(buffer, &test->storage[address], length);
    }
    return MEMTX_OK;
}

static void test_init(TestQemuMemory *test)
{
    dm_usb_host_qemu_memory_init(&test->memory, &test->address_space);
}

static void test_read_write_and_failure_mapping(void)
{
    TestQemuMemory test = { 0 };
    const uint8_t input[] = { 1, 2, 3, 4 };
    uint8_t output[sizeof(input)] = { 0 };

    current_test = &test;
    test_init(&test);
    g_assert_true(dm_usb_host_qemu_memory_write(&test.memory, 8, input,
                                                sizeof(input)));
    g_assert_true(test.is_write);
    g_assert_cmpuint(test.address, ==, 8);
    g_assert_cmpuint(test.length, ==, sizeof(input));
    g_assert_true(dm_usb_host_qemu_memory_read(&test.memory, 8, output,
                                               sizeof(output)));
    g_assert_cmpmem(output, sizeof(output), input, sizeof(input));
    g_assert_false(test.is_write);
    g_assert_cmpuint(test.address, ==, 8);
    g_assert_cmpuint(test.length, ==, sizeof(output));
    test.result = MEMTX_ERROR;
    g_assert_false(dm_usb_host_qemu_memory_read(&test.memory, 8, output,
                                                sizeof(output)));
    g_assert_false(dm_usb_host_qemu_memory_write(&test.memory, 8, input,
                                                 sizeof(input)));
    current_test = NULL;
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/host-qemu-memory/read-write-failure",
                    test_read_write_and_failure_mapping);
    return g_test_run();
}
