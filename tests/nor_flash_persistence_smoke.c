#include "dm_nor_flash_persistence.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

enum { STORAGE_SIZE = 4096 };

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
    FILE *file;

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

    file = fopen(path, "wb");
    assert(file);
    assert(fputc(0, file) != EOF);
    assert(fclose(file) == 0);
    memset(storage, 0xa5, sizeof(storage));
    assert(dm_nor_flash_persistence_load(path, storage, sizeof(storage)) ==
           DM_NOR_FLASH_PERSISTENCE_SIZE_MISMATCH);
    assert(storage[0] == 0xa5);

    assert(dm_nor_flash_persistence_save("/no/such/directory/image.bin",
                                         storage, sizeof(storage)) ==
           DM_NOR_FLASH_PERSISTENCE_IO_ERROR);
    (void)remove(path);
    puts("RESULT: NOR Flash persistence smoke passed");
    return 0;
}
