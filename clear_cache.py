import shutil, os
CACHE_DIRS = ['cache', 'cache/imgs', 'cache/tmp', 'cache/locks']

def clear_cache():
    removed = 0
    for d in CACHE_DIRS:
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
            removed += 1
    print(f'✅ Кэш очищен. Папок пересоздано: {removed}')
if __name__ == '__main__':
    clear_cache()