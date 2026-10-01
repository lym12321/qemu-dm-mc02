/* DM-MC02 board wiring profile.  Peripheral models do not depend on this. */
#ifndef HW_ARM_DM_MC02_BOARD_H
#define HW_ARM_DM_MC02_BOARD_H

#include "exec/memory.h"
#include "hw/arm/dm_mc02_soc.h"
#include "hw/arm/dm_mc02_trigger.h"
#include "hw/arm/dm_mc02_uart.h"
#include "qapi/error.h"
#include "qom/object.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_MC02_BOARD_GPIO_BANK_COUNT 8
#define DM_MC02_BOARD_TIMER_COUNT 5
#define DM_MC02_BOARD_UART_COUNT 6
#define DM_MC02_BOARD_FDCAN_COUNT 3
#define DM_MC02_BOARD_DMA_STREAM_COUNT 8
#define DM_MC02_BOARD_EXTI_ROUTE_COUNT 7
#define DM_MC02_BOARD_OSPI_COUNT 2
#define DM_MC02_BOARD_FLASH_MAX_PAGE_SIZE 256u
#define DM_MC02_BOARD_PIN_NONE 0xffu
#define DM_MC02_BOARD_TIMER_EVENT_KIND_COUNT 8

typedef struct DmMc02Adc DmMc02Adc;
typedef struct DmMc02Dma DmMc02Dma;
typedef struct DmMc02Exti DmMc02Exti;
typedef struct DmMc02Fdcan DmMc02Fdcan;
typedef struct DmMc02Rng DmMc02Rng;
typedef struct DmMc02Tim2 DmMc02Tim2;
typedef struct DmMc02Uart DmMc02Uart;
typedef struct DmMc02Usb DmMc02Usb;
typedef struct DmMc02Wwdg DmMc02Wwdg;

typedef struct DmMc02BoardPin {
    uint8_t bank;
    uint8_t pin;
} DmMc02BoardPin;

/* A software-controlled CS route.  The board owns the physical GPIO and
 * polarity; the SPI core only consumes the resulting target mask. */
typedef struct DmMc02BoardSpiCsRoute {
    unsigned controller;
    unsigned target_index;
    DmMc02BoardPin pin;
    bool active_low;
} DmMc02BoardSpiCsRoute;

/* A board-mounted NOR device behind one OCTOSPI instance.  This is profile
 * data only; storage allocation and command semantics stay in the reusable
 * OCTOSPI/NOR layers. */
typedef struct DmMc02BoardFlashProfile {
    unsigned ospi_index;
    const char *name;
    uint32_t storage_size;
    uint32_t page_size;
    uint32_t sector_size;
    uint8_t jedec_id[3];
    bool memory_mapped;
} DmMc02BoardFlashProfile;

/* Board-side devices and their MCU-facing pins.  The chip models consume
 * generic masks/indices; only this profile names physical board signals. */
typedef struct DmMc02BoardDeviceWiring {
    DmMc02BoardPin led;
    DmMc02BoardPin buzzer;
    DmMc02BoardPin user_key;
    DmMc02BoardPin power_out1;
    DmMc02BoardPin power_out2;
    DmMc02BoardPin power_5v;
    unsigned bmi_spi_controller;
    unsigned bmi_spi_tx_dma;
    unsigned bmi_spi_rx_dma;
    unsigned bmi_spi_tx_stream;
    unsigned bmi_spi_rx_stream;
    uint32_t bmi_spi_tx_request;
    uint32_t bmi_spi_rx_request;
    unsigned tim1_index;
    unsigned tim8_index;
    unsigned buzzer_timer_index;
    unsigned buzzer_channel;
    unsigned buzzer_gpio_af;
    unsigned ws2812_dma_controller;
    unsigned ws2812_dma_stream;
    unsigned power_gpio_bank;
    unsigned power_vin_adc_channel;
    unsigned power_key_adc_channel;
} DmMc02BoardDeviceWiring;

/* Snapshot of the generic GPIO state consumed by board wiring.  Keeping this
 * data-only makes board signal decoding testable without constructing QEMU
 * devices or executing machine-side side effects. */
typedef struct DmMc02BoardGpioState {
    uint32_t moder;
    uint32_t odr;
    uint32_t afr0;
    uint32_t afr1;
} DmMc02BoardGpioState;

typedef enum DmMc02Rs485DeMode {
    DM_MC02_RS485_DE_DISCONNECTED,
    DM_MC02_RS485_DE_MANUAL_GPIO,
    DM_MC02_RS485_DE_AUTO_USART,
} DmMc02Rs485DeMode;

typedef struct DmMc02Rs485Signal {
    DmMc02Rs485DeMode mode;
    bool level;
} DmMc02Rs485Signal;

typedef struct DmMc02BoardSignals {
    uint32_t power_gpio_odr;
    bool led_high;
    bool led_dirty;
    bool buzzer_gpio_output;
    bool buzzer_gpio_high;
    bool buzzer_af_active;
    DmMc02Rs485Signal rs485[DM_MC02_BOARD_UART_COUNT];
} DmMc02BoardSignals;

typedef enum DmMc02BoardTimerClockDomain {
    DM_MC02_BOARD_TIMER_CLOCK_APB1,
    DM_MC02_BOARD_TIMER_CLOCK_APB2,
    DM_MC02_BOARD_TIMER_CLOCK_DOMAIN_COUNT,
} DmMc02BoardTimerClockDomain;

typedef struct DmMc02BoardTimerRoute {
    hwaddr base;
    unsigned irq;
    DmMc02BoardTimerClockDomain clock_domain;
    uint32_t trgo_source_id;
    uint32_t trgo2_source_id;
    /* Optional event-specific source IDs.  The index matches
     * DmMc02TimMasterEventKind; a clear valid bit retains the legacy source
     * ID above for that output. */
    uint32_t master_event_source_id[2][DM_MC02_BOARD_TIMER_EVENT_KIND_COUNT];
    uint8_t master_event_source_valid[2];
    bool has_repetition_counter;
    bool has_break_output;
    bool has_complementary_output;
} DmMc02BoardTimerRoute;

typedef struct DmMc02BoardUartRoute {
    const char *name;
    hwaddr base;
    DmMc02UartKernelClockGroup kernel_clock_group;
    unsigned serial_slot;
    bool has_rx;
    bool has_tx;
    bool rx_dma2;
    bool tx_dma2;
    uint32_t rx_request;
    uint32_t tx_request;
    bool rs485;
    unsigned rs485_gpio_bank;
    unsigned rs485_gpio_pin;
    unsigned rs485_gpio_af;
    unsigned irq;
} DmMc02BoardUartRoute;

typedef struct DmMc02BoardFdcanRoute {
    const char *name;
    hwaddr base;
    uint32_t region_size;
    unsigned serial_slot;
    unsigned irq[2];
} DmMc02BoardFdcanRoute;

typedef struct DmMc02BoardDmaUartRoute {
    unsigned stream;
    unsigned uart_index;
    bool tx;
} DmMc02BoardDmaUartRoute;

/* Board-level interrupt wiring.  Peripheral models expose generic interrupt
 * outputs; the profile owns the MCU vector chosen by this board. */
typedef struct DmMc02BoardIrqMap {
    unsigned exti[DM_MC02_BOARD_EXTI_ROUTE_COUNT];
    unsigned adc;
    unsigned rng;
    unsigned usb;
    unsigned wwdg;
    unsigned dma1[DM_MC02_BOARD_DMA_STREAM_COUNT];
    unsigned dma2[DM_MC02_BOARD_DMA_STREAM_COUNT];
} DmMc02BoardIrqMap;

typedef struct DmMc02BoardProfile {
    const char *name;
    const DmMc02SocProfile *soc;
    DmMc02BoardDeviceWiring devices;
    const DmMc02BoardSpiCsRoute *spi_cs_routes;
    size_t spi_cs_route_count;
    const DmMc02BoardFlashProfile *flash_profiles;
    size_t flash_profile_count;
    DmMc02BoardTimerRoute tim2;
    DmMc02BoardTimerRoute timers[DM_MC02_BOARD_TIMER_COUNT];
    size_t timer_count;
    DmMc02BoardUartRoute uarts[DM_MC02_BOARD_UART_COUNT];
    size_t uart_count;
    DmMc02BoardFdcanRoute fdcans[DM_MC02_BOARD_FDCAN_COUNT];
    size_t fdcan_count;
    hwaddr adc1_dr;
    uint32_t adc1_request;
    hwaddr tim8_ccr1;
    uint32_t tim8_request;
    const DmMc02BoardDmaUartRoute *dma1_uart_routes;
    size_t dma1_uart_route_count;
    const DmMc02BoardDmaUartRoute *dma2_uart_routes;
    size_t dma2_uart_route_count;
    DmMc02BoardIrqMap irqs;
} DmMc02BoardProfile;

const DmMc02BoardProfile *dm_mc02_board_dm_mc02(void);
const DmMc02BoardProfile *dm_mc02_board_lookup(const char *name);
bool dm_mc02_board_validate(const DmMc02BoardProfile *board,
                            Error **errp);

bool dm_mc02_board_decode_gpio_outputs(
    const DmMc02BoardProfile *board,
    const DmMc02BoardGpioState *gpio,
    size_t gpio_count,
    unsigned changed_bank,
    DmMc02BoardSignals *signals);

/* Convert the current GPIO output latches into the target selection mask for
 * one SPI controller.  target_capacity is supplied by the immediate SPI
 * consumer so this board layer does not depend on its implementation size. */
bool dm_mc02_board_decode_spi_selected_mask(
    const DmMc02BoardProfile *board,
    const DmMc02BoardGpioState *gpio,
    size_t gpio_count,
    unsigned controller,
    size_t target_capacity,
    uint32_t *selected_mask);

/* Connect reusable peripheral IRQ outputs to the profile-selected MCU
 * vectors.  This is board wiring, not SoC peripheral behavior. */
void dm_mc02_board_connect_irqs(const DmMc02BoardProfile *board,
                                Object *owner, DeviceState *armv7m,
                                DmMc02Exti *exti, DmMc02Adc *adc1,
                                DmMc02Adc *adc2, DeviceState **adc_irq_or,
                                DmMc02Tim2 *tim2, DmMc02Tim2 *tim_aux,
                                DmMc02Fdcan *fdcan, DmMc02Uart *uart,
                                DmMc02Rng *rng,
                                DmMc02Dma *dma1, DmMc02Dma *dma2,
                                DmMc02Usb *usb, DmMc02Wwdg *wwdg,
                                Error **errp);

#endif
