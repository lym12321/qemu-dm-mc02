/* Board-independent BMI088 SPI command and data framing. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088_spi.h"

static void dm_mc02_bmi088_spi_notify_consumed(DmMc02Bmi088Spi *adapter,
                                               uint64_t sample_sequence)
{
    if (adapter->consume && sample_sequence) {
        adapter->consume(adapter->consume_opaque, sample_sequence);
    }
}

static uint8_t dm_mc02_bmi088_spi_transfer(void *opaque, uint8_t tx,
                                           uint64_t timestamp_ns)
{
    DmMc02Bmi088Spi *adapter = opaque;
    DmMc02Bmi088 *bmi;
    uint8_t result = 0;
    bool accel;

    if (!adapter || !adapter->bmi) {
        return 0;
    }
    bmi = adapter->bmi;
    accel = dm_mc02_bmi088_is_accel(bmi);
    if (!adapter->command_seen) {
        adapter->command_seen = true;
        adapter->read_transfer = (tx & 0x80) != 0;
        adapter->reg = tx & 0x7f;
        adapter->read_start_reg = adapter->reg;
        adapter->dummy_pending = accel && adapter->read_transfer;
        return 0;
    }
    if (adapter->read_transfer) {
        unsigned read_reg_addr = adapter->reg;
        unsigned raw_start = accel ? 0x12 : 0x02;
        bool fifo_read = (accel && read_reg_addr == 0x26) ||
                         (!accel && read_reg_addr == 0x3f);
        DmMc02Bmi088ReadEvent event;

        if (adapter->dummy_pending) {
            adapter->dummy_pending = false;
            return 0;
        }
        result = dm_mc02_bmi088_read_reg(bmi, read_reg_addr, timestamp_ns,
                                          &event);
        /* FIFO_DATA is streaming: a burst keeps its address while each
         * clock advances the device FIFO. */
        if (!fifo_read) {
            adapter->reg++;
        }
        if (adapter->read_start_reg == raw_start &&
            read_reg_addr >= raw_start && read_reg_addr < raw_start + 6) {
            if (read_reg_addr == raw_start) {
                adapter->raw_read_sequence = event.raw_sample_sequence;
            } else if (adapter->raw_read_sequence != event.raw_sample_sequence) {
                adapter->raw_read_sequence = 0;
            }
            if (read_reg_addr == raw_start + 5) {
                uint64_t sequence = adapter->raw_read_sequence;

                adapter->raw_read_sequence = 0;
                dm_mc02_bmi088_spi_notify_consumed(adapter, sequence);
            }
        } else if (event.fifo_frame_complete) {
            dm_mc02_bmi088_spi_notify_consumed(adapter,
                                               event.fifo_sample_sequence);
        }
        /* Raw reads retain their bytes; complete consume already notifies
         * the observer. Only FIFO reads can retire a stored source here. */
        if (fifo_read) {
            dm_mc02_bmi088_notify_source_change(bmi);
        }
        return result;
    }
    {
        unsigned reg = adapter->reg++;

        dm_mc02_bmi088_write_reg(bmi, reg, tx);
    }
    return 0;
}

static void dm_mc02_bmi088_spi_select(void *opaque, bool selected,
                                      uint32_t selected_mask)
{
    DmMc02Bmi088Spi *adapter = opaque;

    (void)selected;
    (void)selected_mask;
    if (!adapter || !adapter->bmi) {
        return;
    }
    /* A bus mask change ends the current device transaction, including when
     * another active-low CS creates a temporary multi-select condition. */
    dm_mc02_bmi088_end_read(adapter->bmi);
    dm_mc02_bmi088_spi_reset(adapter);
}

void dm_mc02_bmi088_spi_init(DmMc02Bmi088Spi *adapter,
                             DmMc02Bmi088 *bmi)
{
    memset(adapter, 0, sizeof(*adapter));
    adapter->bmi = bmi;
}

void dm_mc02_bmi088_spi_reset(DmMc02Bmi088Spi *adapter)
{
    if (!adapter) {
        return;
    }
    adapter->command_seen = false;
    adapter->read_transfer = false;
    adapter->dummy_pending = false;
    adapter->reg = 0;
    adapter->read_start_reg = 0;
    adapter->raw_read_sequence = 0;
}

void dm_mc02_bmi088_spi_set_consume_callback(
    DmMc02Bmi088Spi *adapter, DmMc02Bmi088SpiConsumeFn consume,
    void *opaque)
{
    if (!adapter) {
        return;
    }
    adapter->consume = consume;
    adapter->consume_opaque = opaque;
}

DmMc02SpiTarget dm_mc02_bmi088_spi_target(DmMc02Bmi088Spi *adapter)
{
    return (DmMc02SpiTarget) {
        .transfer = dm_mc02_bmi088_spi_transfer,
        .select = dm_mc02_bmi088_spi_select,
        .opaque = adapter,
    };
}
