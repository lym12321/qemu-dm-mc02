#ifndef DM_MC02_MACHINE_H
#define DM_MC02_MACHINE_H

#include <stddef.h>

#define DM_MC02_MACHINE_NAME "dm-mc02"
#define DM_MC02_CPU_NAME "cortex-m7"
#define DM_MC02_STATUS_PENDING 2
#define DM_MC02_STATUS_BLOCKED 3

typedef struct DmMc02Capability {
    const char *name;
    const char *state;
    const char *detail;
} DmMc02Capability;

const DmMc02Capability *dm_mc02_capabilities(size_t *count);
int dm_mc02_machine_probe(void);

#endif
