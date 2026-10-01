/* Synchronous trigger-event fan-out shared by STM32 peripheral models. */
#ifndef HW_ARM_DM_MC02_TRIGGER_H
#define HW_ARM_DM_MC02_TRIGGER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_MC02_TRIGGER_MAX_SINKS 8
#define DM_MC02_TRIGGER_SOURCE_NONE UINT32_MAX

/* H723 regular-ADC trigger codes used as stable identifiers on the board
 * trigger bus.  The ADC model translates these physical source identifiers
 * to the separate injected-group mux codes where required. */
#define DM_MC02_TRIGGER_SOURCE_TIM2_TRGO 11u
#define DM_MC02_TRIGGER_SOURCE_TIM3_TRGO 4u
#define DM_MC02_TRIGGER_SOURCE_TIM8_TRGO 7u
#define DM_MC02_TRIGGER_SOURCE_TIM8_TRGO2 8u
#define DM_MC02_TRIGGER_SOURCE_TIM1_TRGO 9u
#define DM_MC02_TRIGGER_SOURCE_TIM1_TRGO2 10u
/* Regular ADC EXTSEL=15: TIM3 channel 4 compare/OCREF event. */
#define DM_MC02_TRIGGER_SOURCE_TIM3_CH4 15u

typedef struct DmMc02TriggerEvent {
    uint32_t source_id;
    bool rising;
    /* A value greater than one represents coalesced contiguous edges. */
    unsigned event_count;
    uint64_t timestamp_ns;
} DmMc02TriggerEvent;

typedef void (*DmMc02TriggerSink)(void *opaque,
                                  const DmMc02TriggerEvent *event);

typedef struct DmMc02TriggerConnection {
    DmMc02TriggerSink sink;
    void *opaque;
} DmMc02TriggerConnection;

typedef struct DmMc02TriggerBus {
    DmMc02TriggerConnection connections[DM_MC02_TRIGGER_MAX_SINKS];
    size_t connection_count;
} DmMc02TriggerBus;

/* Connections are established during initialization and must remain fixed
 * before publish is called.  The event pointer is borrowed for the callback
 * duration and must not be retained by a sink. */
void dm_mc02_trigger_bus_init(DmMc02TriggerBus *bus);
bool dm_mc02_trigger_bus_connect(DmMc02TriggerBus *bus,
                                  DmMc02TriggerSink sink, void *opaque);
void dm_mc02_trigger_bus_publish(DmMc02TriggerBus *bus, uint32_t source_id,
                                 bool rising, uint64_t timestamp_ns);
void dm_mc02_trigger_bus_publish_batch(DmMc02TriggerBus *bus,
                                       uint32_t source_id, bool rising,
                                       unsigned event_count,
                                       uint64_t timestamp_ns);

#endif
