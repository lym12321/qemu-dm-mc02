/* Tests for the board-independent STM32H7 host PIO/DMA data path. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_channel_data_path.h"

#define TEST_DMA_BASE 0x24000000u

typedef struct TestDataPath {
    DmStm32H7OtgHost host;
    DmUsbHostChannelDataPath path;
    uint8_t memory[32];
} TestDataPath;

static bool test_memory_read(void *opaque, uint32_t address, uint8_t *data,
                             uint32_t length)
{
    TestDataPath *test = opaque;
    uint32_t offset = address - TEST_DMA_BASE;

    if (address < TEST_DMA_BASE || offset > sizeof(test->memory) ||
        length > sizeof(test->memory) - offset) {
        return false;
    }
    memcpy(data, &test->memory[offset], length);
    return true;
}

static bool test_memory_write(void *opaque, uint32_t address,
                              const uint8_t *data, uint32_t length)
{
    TestDataPath *test = opaque;
    uint32_t offset = address - TEST_DMA_BASE;

    if (address < TEST_DMA_BASE || offset > sizeof(test->memory) ||
        length > sizeof(test->memory) - offset) {
        return false;
    }
    memcpy(&test->memory[offset], data, length);
    return true;
}

static void test_init(TestDataPath *test)
{
    memset(test, 0, sizeof(*test));
    dm_stm32h7_otg_host_init(&test->host, NULL, NULL, NULL, NULL);
    dm_usb_host_channel_data_path_init(
        &test->path, &test->host, test_memory_read, test_memory_write, test);
}

static void test_pio_path(void)
{
    TestDataPath test;
    uint8_t out[3] = { 0 };
    const uint8_t in[] = { 4, 5 };

    test_init(&test);
    dm_stm32h7_otg_host_fifo_write(&test.host, 0, 0x00030201, 3);
    g_assert_true(dm_usb_host_channel_data_path_read_out(&test.path, 0, out,
                                                         sizeof(out)));
    g_assert_cmpmem(out, sizeof(out), "\x01\x02\x03", 3);
    g_assert_true(dm_usb_host_channel_data_path_write_in(&test.path, 0, in,
                                                          sizeof(in)));
    g_assert_cmphex(dm_stm32h7_otg_host_fifo_read(&test.host, 0, 2), ==,
                    0x0504);
}

static void test_dma_path_and_memory_failure(void)
{
    TestDataPath test;
    uint8_t out[3] = { 0 };
    const uint8_t in[] = { 4, 5 };

    test_init(&test);
    memcpy(test.memory, "DMA", sizeof(out));
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_DMAEN, 0);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCDMA(0),
                               TEST_DMA_BASE, 0);
    g_assert_true(dm_usb_host_channel_data_path_read_out(&test.path, 0, out,
                                                         sizeof(out)));
    g_assert_cmpmem(out, sizeof(out), "DMA", 3);
    g_assert_true(dm_usb_host_channel_data_path_write_in(&test.path, 0, in,
                                                          sizeof(in)));
    g_assert_cmpmem(test.memory, sizeof(in), in, sizeof(in));

    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_HCDMA(0),
                               TEST_DMA_BASE + sizeof(test.memory) - 1, 0);
    g_assert_false(dm_usb_host_channel_data_path_read_out(&test.path, 0, out,
                                                          sizeof(out)));
    g_assert_false(dm_usb_host_channel_data_path_write_in(&test.path, 0, in,
                                                           sizeof(in)));
}

static void test_dma_requires_memory_binding(void)
{
    TestDataPath test;
    uint8_t data = 0;

    test_init(&test);
    dm_usb_host_channel_data_path_init(&test.path, &test.host, NULL, NULL,
                                       &test);
    dm_stm32h7_otg_host_write(&test.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_DMAEN, 0);
    g_assert_false(dm_usb_host_channel_data_path_read_out(&test.path, 0,
                                                          &data, sizeof(data)));
    g_assert_false(dm_usb_host_channel_data_path_write_in(&test.path, 0,
                                                           &data, sizeof(data)));
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/host-channel-data-path/pio", test_pio_path);
    g_test_add_func("/dm-usb/host-channel-data-path/dma-memory-failure",
                    test_dma_path_and_memory_failure);
    g_test_add_func("/dm-usb/host-channel-data-path/dma-requires-memory",
                    test_dma_requires_memory_binding);
    return g_test_run();
}
