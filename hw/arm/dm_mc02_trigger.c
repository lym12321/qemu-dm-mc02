#include "hw/arm/dm_mc02_trigger.h"

#include <string.h>

void dm_mc02_trigger_bus_init(DmMc02TriggerBus *bus)
{
    memset(bus, 0, sizeof(*bus));
}

bool dm_mc02_trigger_bus_connect(DmMc02TriggerBus *bus,
                                  DmMc02TriggerSink sink, void *opaque)
{
    if (!bus || !sink || bus->connection_count >= DM_MC02_TRIGGER_MAX_SINKS) {
        return false;
    }
    bus->connections[bus->connection_count++] = (DmMc02TriggerConnection){
        .sink = sink,
        .opaque = opaque,
    };
    return true;
}

void dm_mc02_trigger_bus_publish(DmMc02TriggerBus *bus, uint32_t source_id,
                                 bool rising, uint64_t timestamp_ns)
{
    dm_mc02_trigger_bus_publish_batch(bus, source_id, rising, 1,
                                      timestamp_ns);
}

void dm_mc02_trigger_bus_publish_batch(DmMc02TriggerBus *bus,
                                       uint32_t source_id, bool rising,
                                       unsigned event_count,
                                       uint64_t timestamp_ns)
{
    size_t connection_count;
    DmMc02TriggerEvent event = {
        .source_id = source_id,
        .rising = rising,
        .event_count = event_count ? event_count : 1,
        .timestamp_ns = timestamp_ns,
    };

    if (!bus) {
        return;
    }
    connection_count = bus->connection_count;
    for (size_t i = 0; i < connection_count; ++i) {
        bus->connections[i].sink(bus->connections[i].opaque, &event);
    }
}
