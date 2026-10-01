#include "dm_nor_flash_persistence.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

enum { STORAGE_SIZE = 4096 };

static void assert_size_mismatch_preserves_storage(const char *path,
                                                   size_t image_size,
                                                   uint8_t *storage)
{
    uint8_t *image = malloc(image_size);
    FILE *file;

    assert(image);
    memset(image, 0x37, image_size);
    file = fopen(path, "wb");
    assert(file);
    assert(fwrite(image, 1, image_size, file) == image_size);
    assert(fclose(file) == 0);
    memset(storage, 0xa5, STORAGE_SIZE);
    assert(dm_nor_flash_persistence_load(path, storage, STORAGE_SIZE) ==
           DM_NOR_FLASH_PERSISTENCE_SIZE_MISMATCH);
    for (size_t i = 0; i < STORAGE_SIZE; ++i) {
        assert(storage[i] == 0xa5);
    }
    free(image);
}

static void assert_bytes(const uint8_t *actual, const uint8_t *expected,
                         size_t size)
{
    assert(memcmp(actual, expected, size) == 0);
}

int main(void)
{
    uint8_t storage[STORAGE_SIZE];
    uint8_t expected[STORAGE_SIZE];
    char path[128];
    (void)snprintf(path, sizeof(path),
                   "/tmp/dm-nor-flash-persistence-%ld.bin", (long)getpid());
    (void)remove(path);

    memset(storage, 0xff, sizeof(storage));
    assert(dm_nor_flash_persistence_load(NULL, storage, sizeof(storage)) ==
           DM_NOR_FLASH_PERSISTENCE_DISABLED);
    assert(dm_nor_flash_persistence_load(path, storage, sizeof(storage)) ==
           DM_NOR_FLASH_PERSISTENCE_NOT_FOUND);
    assert(storage[0] == 0xff);

    for (size_t i = 0; i < sizeof(expected); ++i) {
        expected[i] = (uint8_t)(i * 37u + 11u);
    }
    assert(dm_nor_flash_persistence_save(path, expected, sizeof(expected)) ==
           DM_NOR_FLASH_PERSISTENCE_OK);
    memset(storage, 0, sizeof(storage));
    assert(dm_nor_flash_persistence_load(path, storage, sizeof(storage)) ==
           DM_NOR_FLASH_PERSISTENCE_OK);
    assert_bytes(storage, expected, sizeof(storage));

    assert_size_mismatch_preserves_storage(path, STORAGE_SIZE - 1, storage);
    assert_size_mismatch_preserves_storage(path, STORAGE_SIZE + 1, storage);

    assert(dm_nor_flash_persistence_save("/no/such/directory/image.bin",
                                         storage, sizeof(storage)) ==
           DM_NOR_FLASH_PERSISTENCE_IO_ERROR);
    (void)remove(path);
    puts("RESULT: NOR Flash persistence smoke passed");
    return 0;
}
