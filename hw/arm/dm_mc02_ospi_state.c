/* Shared state validation and runtime projection for the OCTOSPI wrapper. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_ospi.h"

#define DM_MC02_OSPI_MAX_RX_SIZE (1u << 20)

bool dm_mc02_ospi_state_valid(const DmMc02Ospi *state)
{
    bool invalid_program_length;
    bool active_read;
    bool active_program;

    if (!state || state->rx_size > DM_MC02_OSPI_MAX_RX_SIZE ||
        state->rx_pos > state->rx_size ||
        (state->rx_size && !state->rx_data) ||
        state->tx_size > DM_MC02_OSPI_MAX_PAGE_SIZE) {
        return false;
    }

    invalid_program_length = state->tx_expected == UINT32_MAX;
    active_read = state->command == 0x03 || state->command == 0x0b ||
                  state->command == 0x6b || state->command == 0xeb ||
                  state->command == 0x05 || state->command == 0x9f;
    active_program = state->command == 0x02 || state->command == 0x32;
    if ((!invalid_program_length &&
         state->tx_expected > DM_MC02_OSPI_MAX_PAGE_SIZE) ||
        (invalid_program_length &&
         (!state->command_valid || state->command_started ||
          !active_program ||
          state->tx_size != 0)) ||
        (!invalid_program_length && state->tx_size > state->tx_expected) ||
        (!state->command_valid &&
         (state->command_started || state->rx_size || state->rx_pos ||
          state->tx_size || state->tx_expected)) ||
        (state->command_started && !active_read && !active_program) ||
        (state->command_started && active_read && !state->rx_size) ||
        (state->command_started && active_program &&
         (!state->tx_expected || invalid_program_length))) {
        return false;
    }

    return true;
}

void dm_mc02_ospi_sync_runtime(DmMc02Ospi *state)
{
    uint32_t cr;

    if (!state) {
        return;
    }

    /* FMODE is the guest-visible source of truth for the memory-mapped
     * alias.  The SSI CS level is a destination-side projection: only an
     * interrupted page-program transaction must remain selected. */
    cr = ldl_le_p((const uint8_t *)state->regs);
    state->memory_mapped = (cr & (3u << 28)) == (3u << 28);
    if (state->has_flash) {
        bool program_active = state->command_started &&
                              state->command_valid &&
                              (state->command == 0x02 ||
                               state->command == 0x32);
        dm_mc02_ssi_nor_select(&state->ssi_nor, program_active);
    }
}
