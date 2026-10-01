/* TEST-ONLY fixture; see dm_mc02_can_medium.h. */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "hw/arm/dm_mc02_can_medium.h"

static int pending_compare(const DmMc02CanMediumPending *a,
                           const DmMc02CanMediumPending *b)
{
    if (a->frame.timestamp_ns != b->frame.timestamp_ns) {
        return a->frame.timestamp_ns < b->frame.timestamp_ns ? -1 : 1;
    }
    if (a->frame.can_id != b->frame.can_id) {
        return a->frame.can_id < b->frame.can_id ? -1 : 1;
    }
    /* For equal identifiers, registration order is the deterministic tie
     * breaker.  It is cached in each pending item, avoiding a node-table
     * scan in the hot comparator path. */
    if (a->registration_order != b->registration_order) {
        return a->registration_order < b->registration_order ? -1 : 1;
    }
    if (a->sequence != b->sequence) {
        return a->sequence < b->sequence ? -1 : 1;
    }
    return 0;
}

static bool pending_before(const DmMc02CanMedium *medium, unsigned a,
                           unsigned b)
{
    return pending_compare(&medium->pending[a], &medium->pending[b]) < 0;
}

static void pending_heap_swap(DmMc02CanMedium *medium, size_t a, size_t b)
{
    uint8_t slot = medium->pending_heap[a];

    medium->pending_heap[a] = medium->pending_heap[b];
    medium->pending_heap[b] = slot;
    medium->pending_heap_pos[medium->pending_heap[a]] = a;
    medium->pending_heap_pos[medium->pending_heap[b]] = b;
}

static void pending_heap_sift_up(DmMc02CanMedium *medium, size_t pos)
{
    while (pos != 0) {
        size_t parent = (pos - 1) / 2;

        if (!pending_before(medium, medium->pending_heap[pos],
                            medium->pending_heap[parent])) {
            break;
        }
        pending_heap_swap(medium, pos, parent);
        pos = parent;
    }
}

static void pending_heap_sift_down(DmMc02CanMedium *medium, size_t pos)
{
    while (true) {
        size_t left = pos * 2 + 1;
        size_t right = left + 1;
        size_t best = pos;

        if (left < medium->pending_heap_count &&
            pending_before(medium, medium->pending_heap[left],
                           medium->pending_heap[best])) {
            best = left;
        }
        if (right < medium->pending_heap_count &&
            pending_before(medium, medium->pending_heap[right],
                           medium->pending_heap[best])) {
            best = right;
        }
        if (best == pos) {
            break;
        }
        pending_heap_swap(medium, pos, best);
        pos = best;
    }
}

static void pending_heap_push(DmMc02CanMedium *medium, unsigned slot)
{
    size_t pos = medium->pending_heap_count++;

    medium->pending_heap[pos] = slot;
    medium->pending_heap_pos[slot] = pos;
    pending_heap_sift_up(medium, pos);
}

static void pending_heap_remove(DmMc02CanMedium *medium, unsigned slot)
{
    int16_t old_pos = medium->pending_heap_pos[slot];
    size_t pos;
    unsigned replacement;

    if (old_pos < 0) {
        return;
    }
    pos = (size_t)old_pos;
    medium->pending_heap_pos[slot] = -1;
    medium->pending_heap_count--;
    if (pos == medium->pending_heap_count) {
        return;
    }

    replacement = medium->pending_heap[medium->pending_heap_count];
    medium->pending_heap[pos] = replacement;
    medium->pending_heap_pos[replacement] = pos;
    if (pos != 0 && pending_before(medium, replacement,
                                   medium->pending_heap[(pos - 1) / 2])) {
        pending_heap_sift_up(medium, pos);
    } else {
        pending_heap_sift_down(medium, pos);
    }
}

static uint64_t next_pending_time(const DmMc02CanMedium *medium)
{
    if (!medium->pending_heap_count) {
        return UINT64_MAX;
    }
    return medium->pending[medium->pending_heap[0]].frame.timestamp_ns;
}

static void medium_schedule(DmMc02CanMedium *medium)
{
    uint64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t next = next_pending_time(medium);

    if (next == UINT64_MAX) {
        timer_del(medium->dispatch_timer);
        return;
    }
    if (medium->busy_until_ns > next) {
        next = medium->busy_until_ns;
    }
    if (next <= now_ns) {
        next = now_ns + 1;
    }
    timer_mod(medium->dispatch_timer, next);
}

static unsigned pending_ready_slot(const DmMc02CanMedium *medium,
                                   uint64_t now_ns)
{
    unsigned best = medium->pending_heap[0];
    bool timed = false;

    /* The heap remains the fast legacy path.  Once a duration-aware frame is
     * ready, however, every frame that arrived while the bus was busy can
     * participate in the next arbitration round; timestamp order alone
     * would incorrectly let an older high-ID frame beat a newer low-ID one. */
    for (unsigned i = 0; i < ARRAY_SIZE(medium->pending); ++i) {
        const DmMc02CanMediumPending *pending = &medium->pending[i];

        if (pending->used && pending->frame.timestamp_ns <= now_ns &&
            pending->frame.duration_ns) {
            timed = true;
            break;
        }
    }
    if (!timed) {
        return best;
    }
    for (unsigned i = 0; i < ARRAY_SIZE(medium->pending); ++i) {
        const DmMc02CanMediumPending *candidate = &medium->pending[i];
        const DmMc02CanMediumPending *current = &medium->pending[best];

        if (!candidate->used || candidate->frame.timestamp_ns > now_ns) {
            continue;
        }
        if (candidate->frame.can_id < current->frame.can_id ||
            (candidate->frame.can_id == current->frame.can_id &&
             (candidate->registration_order < current->registration_order ||
              (candidate->registration_order == current->registration_order &&
               candidate->sequence < current->sequence)))) {
            best = i;
        }
    }
    return best;
}

static void dispatch_one(DmMc02CanMedium *medium,
                         DmMc02CanMediumPending *pending)
{
    bool acknowledged = false;

    /* A receiver is considered enabled by its controller-specific callback.
     * Delivery is still attempted when its FIFO is full: that lets the
     * controller record a hardware-visible drop/full condition while this
     * minimal medium reports the presence of a listening node as ACK. */
    for (unsigned i = 0; i < ARRAY_SIZE(medium->nodes); ++i) {
        DmMc02CanMediumNode *node = &medium->nodes[i];

        if (!node->registered || node->opaque == pending->sender ||
            !node->enabled || !node->enabled(node->opaque)) {
            continue;
        }
        acknowledged = true;
        if (node->deliver) {
            node->deliver(node->opaque, &pending->frame);
        }
    }

    for (unsigned i = 0; i < ARRAY_SIZE(medium->nodes); ++i) {
        DmMc02CanMediumNode *node = &medium->nodes[i];

        if (node->registered && node->opaque == pending->sender) {
            if (node->complete) {
                node->complete(node->opaque, &pending->frame, acknowledged);
            }
            break;
        }
    }
}

static void medium_dispatch(void *opaque)
{
    DmMc02CanMedium *medium = opaque;
    uint64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    unsigned slot;

    if (medium->busy_until_ns > now_ns) {
        medium_schedule(medium);
        return;
    }
    while (medium->pending_heap_count &&
           medium->pending[medium->pending_heap[0]].frame.timestamp_ns <=
               now_ns) {
        DmMc02CanMediumPending pending;

        slot = pending_ready_slot(medium, now_ns);
        pending_heap_remove(medium, slot);
        pending = medium->pending[slot];
        medium->pending[slot].used = false;
        medium->pending_count--;
        /* Copy before the callback: a receiver may synchronously submit a
         * new frame and reuse the freed storage slot. */
        dispatch_one(medium, &pending);
        if (pending.frame.duration_ns) {
            /* A non-zero duration opts this frame into the serialized bus
             * path.  The next ready frame cannot start before this virtual
             * bus interval ends. */
            medium->busy_until_ns = now_ns + pending.frame.duration_ns;
            break;
        }
    }
    medium_schedule(medium);
}

void dm_mc02_can_medium_init(DmMc02CanMedium *medium)
{
    memset(medium, 0, sizeof(*medium));
    for (unsigned i = 0; i < ARRAY_SIZE(medium->pending_heap_pos); ++i) {
        medium->pending_heap_pos[i] = -1;
    }
    medium->dispatch_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                          medium_dispatch, medium);
}

void dm_mc02_can_medium_reset(DmMc02CanMedium *medium)
{
    if (!medium) {
        return;
    }
    if (medium->dispatch_timer) {
        timer_del(medium->dispatch_timer);
    }
    memset(medium->pending, 0, sizeof(medium->pending));
    memset(medium->pending_heap, 0, sizeof(medium->pending_heap));
    for (unsigned i = 0; i < ARRAY_SIZE(medium->pending_heap_pos); ++i) {
        medium->pending_heap_pos[i] = -1;
    }
    medium->pending_heap_count = 0;
    medium->pending_count = 0;
    medium->next_sequence = 0;
    medium->busy_until_ns = 0;
}

void dm_mc02_can_medium_cleanup(DmMc02CanMedium *medium)
{
    if (!medium) {
        return;
    }
    timer_free(medium->dispatch_timer);
    medium->dispatch_timer = NULL;
    memset(medium->nodes, 0, sizeof(medium->nodes));
    memset(medium->pending, 0, sizeof(medium->pending));
    medium->pending_count = 0;
}

bool dm_mc02_can_medium_register(DmMc02CanMedium *medium, void *opaque,
                                 DmMc02CanMediumEnabled enabled,
                                 DmMc02CanMediumDeliver deliver,
                                 DmMc02CanMediumComplete complete)
{
    for (unsigned i = 0; i < ARRAY_SIZE(medium->nodes); ++i) {
        DmMc02CanMediumNode *node = &medium->nodes[i];

        if (node->registered && node->opaque == opaque) {
            return true;
        }
    }
    for (unsigned i = 0; i < ARRAY_SIZE(medium->nodes); ++i) {
        DmMc02CanMediumNode *node = &medium->nodes[i];

        if (!node->registered) {
            node->registered = true;
            node->opaque = opaque;
            node->registration_order = medium->next_registration_order++;
            node->enabled = enabled;
            node->deliver = deliver;
            node->complete = complete;
            return true;
        }
    }
    return false;
}

void dm_mc02_can_medium_unregister(DmMc02CanMedium *medium, void *opaque)
{
    for (unsigned i = 0; i < ARRAY_SIZE(medium->pending); ++i) {
        if (medium->pending[i].used && medium->pending[i].sender == opaque) {
            pending_heap_remove(medium, i);
            medium->pending[i].used = false;
            medium->pending_count--;
        }
    }
    for (unsigned i = 0; i < ARRAY_SIZE(medium->nodes); ++i) {
        if (medium->nodes[i].registered && medium->nodes[i].opaque == opaque) {
            memset(&medium->nodes[i], 0, sizeof(medium->nodes[i]));
        }
    }
    medium_schedule(medium);
}

void dm_mc02_can_medium_cancel_sender(DmMc02CanMedium *medium, void *sender)
{
    if (!medium || !sender) {
        return;
    }
    for (unsigned i = 0; i < ARRAY_SIZE(medium->pending); ++i) {
        if (medium->pending[i].used && medium->pending[i].sender == sender) {
            pending_heap_remove(medium, i);
            medium->pending[i].used = false;
            medium->pending_count--;
        }
    }
    medium_schedule(medium);
}

bool dm_mc02_can_medium_submit(DmMc02CanMedium *medium, void *sender,
                               const DmMc02CanFrame *frame)
{
    if (!medium || !frame || medium->pending_count >=
        DM_MC02_CAN_MEDIUM_QUEUE_SIZE) {
        return false;
    }
    /* sender == NULL is the host-wire ingress path and is intentionally
     * allowed without a registered controller node. */
    if (!sender) {
        for (unsigned j = 0; j < ARRAY_SIZE(medium->pending); ++j) {
            if (!medium->pending[j].used) {
                DmMc02CanMediumPending *pending = &medium->pending[j];

                pending->used = true;
                pending->sender = NULL;
                pending->frame = *frame;
                pending->registration_order = UINT_MAX;
                pending->sequence = medium->next_sequence++;
                medium->pending_count++;
                pending_heap_push(medium, j);
                /* Defer an already-due frame by one virtual nanosecond so
                 * same-timestamp submissions in one guest turn share the
                 * arbitration batch.  medium_schedule() also preserves an
                 * active bus interval. */
                medium_schedule(medium);
                return true;
            }
        }
        return false;
    }
    for (unsigned i = 0; i < ARRAY_SIZE(medium->nodes); ++i) {
        if (!medium->nodes[i].registered ||
            medium->nodes[i].opaque != sender) {
            continue;
        }
        for (unsigned j = 0; j < ARRAY_SIZE(medium->pending); ++j) {
            if (!medium->pending[j].used) {
                DmMc02CanMediumPending *pending = &medium->pending[j];

                pending->used = true;
                pending->sender = sender;
                pending->frame = *frame;
                pending->registration_order = medium->nodes[i].registration_order;
                pending->sequence = medium->next_sequence++;
                medium->pending_count++;
                pending_heap_push(medium, j);
                medium_schedule(medium);
                return true;
            }
        }
        break;
    }
    return false;
}
