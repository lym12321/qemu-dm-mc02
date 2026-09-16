#include "hw/arm/dm_mc02_trigger.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

enum {
    REQUIRED_SINKS = 3,
    MAX_EVENTS = 4,
    MAX_SINKS = 256,
};

typedef struct Capture {
    unsigned id;
    unsigned count;
    DmMc02TriggerEvent events[MAX_EVENTS];
    unsigned order[MAX_EVENTS];
} Capture;

static unsigned callback_count;
static Capture *expected_captures;

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "trigger smoke: line %u: %s\n", line, expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static void capture_sink(void *opaque, const DmMc02TriggerEvent *event)
{
    Capture *capture = opaque;

    CHECK(capture != NULL);
    CHECK(event != NULL);
    CHECK(capture->id < REQUIRED_SINKS);
    CHECK(capture == &expected_captures[capture->id]);
    CHECK(capture->count < MAX_EVENTS);
    capture->events[capture->count] = *event;
    capture->order[capture->count] = callback_count++;
    capture->count++;
}

static void check_event(const DmMc02TriggerEvent *event, uint32_t source_id,
                        bool rising, unsigned event_count,
                        uint64_t timestamp_ns)
{
    CHECK(event->source_id == source_id);
    CHECK(event->rising == rising);
    CHECK(event->event_count == event_count);
    CHECK(event->timestamp_ns == timestamp_ns);
}

int main(void)
{
    DmMc02TriggerBus bus;
    Capture captures[REQUIRED_SINKS] = { 0 };
    unsigned connected = 0;
    unsigned i;

    expected_captures = captures;
    dm_mc02_trigger_bus_init(&bus);

    for (i = 0; i < REQUIRED_SINKS; ++i) {
        captures[i].id = i;
        CHECK(dm_mc02_trigger_bus_connect(&bus, capture_sink, &captures[i]));
    }
    CHECK(bus.connection_count == REQUIRED_SINKS);
    CHECK(callback_count == 0);

    dm_mc02_trigger_bus_publish(&bus, UINT32_C(0x12345678), true,
                                UINT64_C(9876543210));
    dm_mc02_trigger_bus_publish(&bus, UINT32_C(0x9abcdef0), false,
                                UINT64_C(112233445566));

    for (i = 0; i < REQUIRED_SINKS; ++i) {
        CHECK(captures[i].count == 2);
        check_event(&captures[i].events[0], UINT32_C(0x12345678), true,
                    1, UINT64_C(9876543210));
        check_event(&captures[i].events[1], UINT32_C(0x9abcdef0), false,
                    1, UINT64_C(112233445566));
        CHECK(captures[i].order[0] == i);
        CHECK(captures[i].order[1] == REQUIRED_SINKS + i);
    }
    CHECK(callback_count == 2 * REQUIRED_SINKS);

    dm_mc02_trigger_bus_publish_batch(&bus, UINT32_C(0x13579bdf), true, 3,
                                      UINT64_C(223344556677));
    for (i = 0; i < REQUIRED_SINKS; ++i) {
        CHECK(captures[i].count == 3);
        check_event(&captures[i].events[2], UINT32_C(0x13579bdf), true,
                    3, UINT64_C(223344556677));
    }
    CHECK(callback_count == 3 * REQUIRED_SINKS);

    /* Keep adding valid sinks until the implementation reports exhaustion. */
    while (connected < MAX_SINKS &&
           dm_mc02_trigger_bus_connect(&bus, capture_sink, &captures[0])) {
        ++connected;
    }
    CHECK(connected == DM_MC02_TRIGGER_MAX_SINKS - REQUIRED_SINKS);
    CHECK(bus.connection_count == DM_MC02_TRIGGER_MAX_SINKS);
    CHECK(!dm_mc02_trigger_bus_connect(&bus, capture_sink, &captures[0]));

    puts("trigger smoke: PASS");
    return 0;
}
