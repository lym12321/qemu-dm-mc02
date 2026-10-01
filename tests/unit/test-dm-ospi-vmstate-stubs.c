/* Narrow runtime stubs for the isolated OCTOSPI VMState test. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_ssi_nor.h"

unsigned dm_mc02_ospi_select_calls;
bool dm_mc02_ospi_selected;

void dm_mc02_ssi_nor_select(DmMc02SsiNor *adapter, bool selected)
{
    dm_mc02_ospi_select_calls++;
    dm_mc02_ospi_selected = selected;
    if (adapter) {
        adapter->selected = selected;
    }
}
