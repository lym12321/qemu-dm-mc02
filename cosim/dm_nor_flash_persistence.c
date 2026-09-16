/* Host-side persistence for a caller-owned NOR Flash image. */
#include "dm_nor_flash_persistence.h"

#include <errno.h>
#include <stdio.h>

static DmNorFlashPersistenceResult dm_nor_flash_persistence_args(
    const char *path, const uint8_t *storage, size_t storage_size)
{
    if (!path || !path[0]) {
        return DM_NOR_FLASH_PERSISTENCE_DISABLED;
    }
    if (!storage || !storage_size) {
        return DM_NOR_FLASH_PERSISTENCE_INVALID;
    }
    return DM_NOR_FLASH_PERSISTENCE_OK;
}

DmNorFlashPersistenceResult dm_nor_flash_persistence_load(
    const char *path, uint8_t *storage, size_t storage_size)
{
    DmNorFlashPersistenceResult args =
        dm_nor_flash_persistence_args(path, storage, storage_size);
    FILE *file;
    long length;

    if (args != DM_NOR_FLASH_PERSISTENCE_OK) {
        return args;
    }
    file = fopen(path, "rb");
    if (!file) {
        return errno == ENOENT ? DM_NOR_FLASH_PERSISTENCE_NOT_FOUND :
                                 DM_NOR_FLASH_PERSISTENCE_IO_ERROR;
    }
    if (fseek(file, 0, SEEK_END) != 0) {
        fclose(file);
        return DM_NOR_FLASH_PERSISTENCE_IO_ERROR;
    }
    length = ftell(file);
    if (length < 0) {
        fclose(file);
        return DM_NOR_FLASH_PERSISTENCE_IO_ERROR;
    }
    if ((size_t)length != storage_size) {
        fclose(file);
        return DM_NOR_FLASH_PERSISTENCE_SIZE_MISMATCH;
    }
    if (fseek(file, 0, SEEK_SET) != 0) {
        fclose(file);
        return DM_NOR_FLASH_PERSISTENCE_IO_ERROR;
    }
    size_t read_size = fread(storage, 1, storage_size, file);
    int read_error = ferror(file);
    int close_error = fclose(file);

    if (read_size != storage_size || read_error || close_error != 0) {
        return DM_NOR_FLASH_PERSISTENCE_IO_ERROR;
    }
    return DM_NOR_FLASH_PERSISTENCE_OK;
}

DmNorFlashPersistenceResult dm_nor_flash_persistence_save(
    const char *path, const uint8_t *storage, size_t storage_size)
{
    DmNorFlashPersistenceResult args =
        dm_nor_flash_persistence_args(path, storage, storage_size);
    FILE *file;

    if (args != DM_NOR_FLASH_PERSISTENCE_OK) {
        return args;
    }
    file = fopen(path, "wb");
    if (!file) {
        return DM_NOR_FLASH_PERSISTENCE_IO_ERROR;
    }
    int write_error = fwrite(storage, 1, storage_size, file) != storage_size;

    if (fflush(file) != 0) {
        write_error = 1;
    }
    int close_error = fclose(file);

    if (write_error || close_error != 0) {
        return DM_NOR_FLASH_PERSISTENCE_IO_ERROR;
    }
    return DM_NOR_FLASH_PERSISTENCE_OK;
}
