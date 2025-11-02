import os
import logging
from pathlib import Path
from typing import Optional, Dict, List
from aiogram.types import FSInputFile
logger = logging.getLogger(__name__)

class AssetsManager:
    """
    Универсальный менеджер ассетов.
    Ищет картинки по имени независимо от расширения.
    """
    SUPPORTED_EXTS = ('.png', '.jpg', '.jpeg', '.webp')

    def __init__(self, assets_base_path: str='assets'):
        self.assets_base_path = Path(assets_base_path)
        self.assets_base_path.mkdir(exist_ok=True)
        self.assets_map = {'menu': {'main': 'menu_main', 'accounts': 'menu_accounts', 'add': 'menu_add', 'delete': 'menu_delete', 'script': 'menu_script'}, 'backgrounds': {'default': 'background_default', 'profile': 'background_profile', 'success': 'background_success', 'error': 'background_error'}}

    def _find_asset_file(self, base_name: str) -> Optional[Path]:
        """
        Ищет файл с любым допустимым расширением.
        """
        for ext in self.SUPPORTED_EXTS:
            p = self.assets_base_path / f'{base_name}{ext}'
            if p.exists():
                return p
        return None

    def get_asset(self, category: str, asset_name: str) -> Optional[FSInputFile]:
        try:
            base = self.assets_map.get(category, {}).get(asset_name)
            if not base:
                logger.debug(f"[assets] Неизвестный ассет '{asset_name}' в категории '{category}'")
                return None
            path = self._find_asset_file(base)
            if path:
                return FSInputFile(path)
            logger.warning(f"[assets] Файл для '{asset_name}' не найден в {self.assets_base_path}")
            return None
        except Exception as e:
            logger.error(f'[assets] Ошибка получения ассета {asset_name}: {e}')
            return None

    def get_menu_asset(self, menu_type: str) -> Optional[FSInputFile]:
        return self.get_asset('menu', menu_type)

    def get_background(self, bg_type: str) -> Optional[FSInputFile]:
        return self.get_asset('backgrounds', bg_type)

    def list_available_assets(self) -> Dict[str, List[str]]:
        """
        Возвращает доступные ассеты по категориям.
        """
        available: Dict[str, List[str]] = {}
        for category, items in self.assets_map.items():
            available[category] = []
            for name, base in items.items():
                p = self._find_asset_file(base)
                if p:
                    available[category].append(f'{name} ({p.name})')
        return available
assets_manager = AssetsManager(os.getenv('ASSETS_DIR', 'assets'))
if __name__ == '__main__':
    print('📦 Проверка ассетов...')
    assets = assets_manager.list_available_assets()
    for cat, lst in assets.items():
        print(f"[{cat}] -> {(', '.join(lst) if lst else '— пусто —')}")