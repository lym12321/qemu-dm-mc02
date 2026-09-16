/* Host-side persistence for a caller-owned NOR Flash image. */
#ifndef DM_NOR_FLASH_PERSISTENCE_H
#define DM_NOR_FLASH_PERSISTENCE_H

#include <stddef.h>
#include <stdint.h>

typedef enum DmNorFlashPersistenceResult {
    DM_NOR_FLASH_PERSISTENCE_OK = 0,
    DM_NOR_FLASH_PERSISTENCE_DISABLED,
    DM_NOR_FLASH_PERSISTENCE_NOT_FOUND,
    DM_NOR_FLASH_PERSISTENCE_IO_ERROR,
    DM_NOR_FLASH_PERSISTENCE_SIZE_MISMATCH,
    DM_NOR_FLASH_PERSISTENCE_INVALID,
} DmNorFlashPersistenceResult;

/* Load an exact-size raw image into storage.  A missing file leaves storage
 * unchanged; callers normally initialize it to the erased value first. */
DmNorFlashPersistenceResult dm_nor_flash_persistence_load(
    const char *path, uint8_t *storage, size_t storage_size);

/* Save the complete raw image.  The operation is intended for initialization
 * or shutdown, never for a Flash command hot path. */
DmNorFlashPersistenceResult dm_nor_flash_persistence_save(
    const char *path, const uint8_t *storage, size_t storage_size);

#endif /* DM_NOR_FLASH_PERSISTENCE_H */
