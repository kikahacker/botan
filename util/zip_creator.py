import zipfile
import os
from pathlib import Path

async def create_cookie_zip(user_id: int) -> str:
    """Создает ZIP архив с get_cookie_playwright.py и batnik.bat"""
    zip_path = f'temp/cookie_kit_{user_id}.zip'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists('get_cookie_playwright.py'):
            zipf.write('get_cookie_playwright.py', 'get_cookie_playwright.py')
        if os.path.exists('batnik.bat'):
            zipf.write('batnik.bat', 'batnik.bat')
    return zip_path