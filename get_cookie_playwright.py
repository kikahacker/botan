"""
get_cookie_playwright.py

Открывает браузер через Playwright, ждёт входа в Roblox, извлекает .ROBLOSECURITY
и сохраняет её в файл cookies.txt на Рабочем столе (и копирует в буфер обмена, если available).

Требования:
    pip install playwright pyperclip
    python -m playwright install chromium

Использование:
    python get_cookie_playwright.py
"""
import os
import sys
import time
try:
    from playwright.sync_api import sync_playwright
except Exception as e:
    print('Ошибка: Playwright не установлен или не найден.')
    print('Установи его: pip install playwright')
    print('Затем: python -m playwright install chromium')
    sys.exit(1)
try:
    import pyperclip
    HAVE_PYPERCLIP = True
except Exception:
    HAVE_PYPERCLIP = False

def desktop_cookies_path(filename: str='cookies.txt') -> str:
    """Возвращает путь к файлу на Рабочем столе для текущего пользователя (кроссплатформенно)."""
    home = os.path.expanduser('~')
    desktop = os.path.join(home, 'Desktop')
    if not os.path.isdir(desktop):
        desktop = home
    return os.path.join(desktop, filename)

def write_cookie_to_desktop(cookie_value: str, filename: str='cookies.txt') -> str:
    path = desktop_cookies_path(filename)
    mode = 'a' if os.path.exists(path) else 'w'
    try:
        with open(path, mode, encoding='utf-8') as f:
            f.write(cookie_value.strip() + '\n')
    except Exception as e:
        raise RuntimeError(f'Не удалось записать файл {path}: {e}')
    return path

def find_roblosecurity_from_cookies(cookies: list) -> str:
    for c in cookies:
        name = c.get('name') or ''
        if name == '.ROBLOSECURITY':
            return c.get('value')
    return None

def main():
    print('=== Roblox .ROBLOSECURITY helper (Playwright) ===')
    print('Откроется браузер. В нём нужно войти в аккаунт Roblox.')
    print('После входа вернись сюда и нажми Enter, чтобы скрипт прочитал cookie.')
    print()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto('https://www.roblox.com/')
        except Exception as e:
            print('Не удалось открыть https://www.roblox.com/:', e)
            browser.close()
            sys.exit(1)
        input('После входа в аккаунт в браузере нажмите Enter в этой консоли...')
        time.sleep(1.0)
        try:
            cookies = context.cookies()
        except Exception as e:
            print('Ошибка при получении cookies из контекста браузера:', e)
            browser.close()
            sys.exit(1)
        roblosec = find_roblosecurity_from_cookies(cookies)
        browser.close()
    if not roblosec:
        print('❌ .ROBLOSECURITY не найдена.')
        print('Возможные причины:')
        print('- вы не вошли в аккаунт в только что открывшемся окне')
        print('- сайт использует другую область cookie (редко)')
        print('- страница блокирует доступ к cookie')
        sys.exit(1)
    try:
        out_path = write_cookie_to_desktop(roblosec, filename='cookies.txt')
    except Exception as e:
        print('Ошибка записи cookie в файл:', e)
        sys.exit(1)
    if HAVE_PYPERCLIP:
        try:
            pyperclip.copy(roblosec)
            clipboard_msg = ' (скопировано в буфер обмена)'
        except Exception:
            clipboard_msg = ' (не удалось скопировать в буфер обмена)'
    else:
        clipboard_msg = ' (pyperclip не установлен — копирование в буфер недоступно)'
    print('✅ Найдена .ROBLOSECURITY и сохранена в:', out_path)
    print('Тебе нужно безопасно хранить этот файл. Не отправляй его третьим лицам.')
    print('Cookie (первые 6 символов):', roblosec[:6] + '...' if len(roblosec) > 6 else roblosec, clipboard_msg)
    print()
    print('Далее можно отправить файл bоту или использовать команду:')
    print('/setcookie <вставь_сюда_cookie> --confirm')
    print()
    print('Если хочешь, запускай этот же скрипт снова — он добавит следующую cookie в новую строку.')
    return
if __name__ == '__main__':
    main()