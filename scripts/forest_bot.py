"""
forest_bot.py - Бот для лесного плейса
Запускает игру, активирует окно, делает действия и скриншоты
"""
import json
import sys
import time
import os
import subprocess
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def install_dependencies():
    """Устанавливает необходимые зависимости"""
    try:
        from selenium import webdriver
        import pyautogui
        import psutil
        import keyboard
        print('✅ Все зависимости установлены')
    except ImportError as e:
        print(f'❌ Не хватает зависимостей: {e}')
        print('🔄 Устанавливаю зависимости...')
        if 'selenium' in str(e):
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'selenium', 'webdriver-manager'])
            print('✅ Selenium установлен')
        if 'pyautogui' in str(e):
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pyautogui'])
            print('✅ PyAutoGUI установлен')
        if 'psutil' in str(e):
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'psutil'])
            print('✅ Psutil установлен')
        if 'keyboard' in str(e):
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'keyboard'])
            print('✅ Keyboard установлен')
        import importlib
        importlib.invalidate_caches()
install_dependencies()
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
import pyautogui
import keyboard
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 1.0

def log(message):
    """Функция для логирования"""
    print(message)

def activate_roblox_window():
    """Активирует окно Roblox Player для двух мониторов"""
    log('🖥️ АКТИВАЦИЯ НА ОСНОВНОМ МОНИТОРЕ...')
    time.sleep(3)
    windows = pyautogui.getWindowsWithTitle('Roblox')
    if windows:
        window = windows[0]
        log(f'✅ Найдено окно: {window.title}')
        window.activate()
        time.sleep(2)
        return True
    screen_width, screen_height = pyautogui.size()
    hot_spots = [(screen_width // 2, screen_height // 2), (100, 100), (screen_width - 100, 100), (100, screen_height - 100), (screen_width - 100, screen_height - 100)]
    for i, (x, y) in enumerate(hot_spots, 1):
        log(f'🖱️ Клик {i}/5 в ({x}, {y})')
        pyautogui.click(x, y)
        time.sleep(1)
        windows = pyautogui.getWindowsWithTitle('Roblox')
        if windows:
            window = windows[0]
            log(f'✅ Окно найдено: {window.title}')
            window.activate()
            time.sleep(2)
            return True
    log('⚠️ Окно не найдено автоматически, продолжаем...')
    return True

def close_roblox_player():
    """Закрывает Roblox Player"""
    try:
        log('🔴 Закрываю Roblox Player...')
        if os.name == 'nt':
            subprocess.run(['taskkill', '/f', '/im', 'RobloxPlayerBeta.exe'], check=False)
            log('✅ Roblox Player закрыт')
    except Exception as e:
        log(f'⚠️ Ошибка при закрытии Roblox Player: {e}')

def main():
    if len(sys.argv) != 2:
        log('Использование: python forest_bot.py <config_file>')
        return
    with open(sys.argv[1], 'r') as f:
        config = json.load(f)
    roblox_id = config['roblox_id']
    cookie = config['cookie']
    telegram_id = config['telegram_id']
    log(f'🚀 Запуск бота для Лесного плейса')
    log(f'👤 Пользователь: {roblox_id}')
    chrome_options = Options()
    chrome_options.add_argument('--window-size=1200,800')
    chrome_options.add_argument('--user-agent=Mozilla/5.0')
    chrome_options.add_argument('--disable-blink-features=AutomationControlled')
    chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    wait = WebDriverWait(driver, 15)
    try:
        log('🔐 Авторизация...')
        driver.get('https://www.roblox.com')
        time.sleep(1)
        driver.delete_all_cookies()
        driver.add_cookie({'name': '.ROBLOSECURITY', 'value': cookie, 'domain': '.roblox.com', 'path': '/', 'secure': True})
        driver.refresh()
        time.sleep(2)
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, 'body')))
            log('✅ Страница загружена, продолжаем...')
            time.sleep(2)
        except Exception as e:
            log(f'⚠️ Страница не загрузилась нормально: {e}')
        log('🎮 Захожу на страницу игры...')
        game_url = 'https://www.roblox.com/games/127742093697776/Plants-Vs-Brainrots'
        driver.get(game_url)
        time.sleep(2)
        log('🔄 Нажимаю кнопку Play...')
        play_clicked = False
        play_selectors = ["[data-testid='play-button']", '.btn-common-play-game-lg', '.btn-play-game', "button[class*='play']", "a[class*='play']"]
        try:
            log('🔧 Стратегия 1: JavaScript клик...')
            driver.execute_script('\n                var playBtn = document.querySelector(\'[data-testid="play-button"]\') || \n                             document.querySelector(\'.btn-common-play-game-lg\') ||\n                             document.querySelector(\'.btn-play-game\');\n                if (playBtn) {\n                    playBtn.click();\n                    return true;\n                }\n                return false;\n            ')
            log('✅ JavaScript клик выполнен')
            play_clicked = True
        except Exception as e:
            log(f'❌ JavaScript клик не сработал: {e}')
        if not play_clicked:
            for selector in play_selectors:
                try:
                    log(f'🔧 Ищу кнопку: {selector}')
                    play_btn = driver.find_element(By.CSS_SELECTOR, selector)
                    log(f'✅ Кнопка найдена, кликаю...')
                    play_btn.click()
                    log('✅ Кнопка Play нажата!')
                    play_clicked = True
                    break
                except Exception as e:
                    log(f'❌ Не найдена: {selector}')
        if not play_clicked:
            try:
                log('🔧 Стратегия 3: Ожидание и клик...')
                play_btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='play-button']")))
                play_btn.click()
                log('✅ Кнопка Play нажата с ожиданием!')
                play_clicked = True
            except Exception as e:
                log(f'❌ Ожидание не помогло: {e}')
        if not play_clicked:
            raise Exception('Кнопка Play не найдена')
        log('⏳ Ожидаю диалоговое окно...')
        time.sleep(2)
        pyautogui.press('left')
        time.sleep(0.5)
        pyautogui.press('enter')
        log('✅ Подтверждение отправлено')
        log('⏳ Ожидаю загрузки Roblox Player...')
        time.sleep(10)
        activate_roblox_window()
        log('🎮 ДЕЙСТВИЯ В ИГРЕ С KEYBOARD...')
        time.sleep(5)
        log('🎯 ДАЮ ФОКУС ИГРЕ...')
        windows = pyautogui.getWindowsWithTitle('Roblox')
        if windows:
            window = windows[0]
            center_x = window.left + window.width // 2
            center_y = window.top + window.height // 2
            log(f'🖱️ Кликаю в центр игры: ({center_x}, {center_y})')
            pyautogui.click(center_x, center_y)
            time.sleep(2)
        log('⏳ Жду фокуса игры...')
        time.sleep(3)
        actions = [('ВПЕРЕД', 'w', 3), ('ВЛЕВО', 'a', 2), ('НАЗАД', 's', 2), ('ПРЫЖОК', 'space', 0.5), ('СБОР', 'e', 0.5), ('ВПЕРЕД', 'w', 2)]
        for action_name, key, duration in actions:
            log(f'🎮 ДЕЙСТВИЕ: {action_name}')
            time.sleep(1)
            if duration > 0:
                log(f'⏳ KEYBOARD: Удерживаю {key} {duration}сек...')
                keyboard.press(key)
                time.sleep(duration)
                keyboard.release(key)
                log(f'✅ KEYBOARD: Отпустил {key}')
            else:
                log(f'⏳ KEYBOARD: Нажимаю {key}')
                keyboard.press(key)
                time.sleep(0.1)
                keyboard.release(key)
            log(f'✅ {action_name} ЗАВЕРШЕНО')
            time.sleep(1)
        log('🎮 ВСЕ ДЕЙСТВИЯ ВЫПОЛНЕНЫ')
        time.sleep(2)
        log('📸 Делаю скриншот игры...')
        os.makedirs('temp', exist_ok=True)
        screenshot_path = f'temp/game_screenshot_{telegram_id}_forest.png'
        screenshot = pyautogui.screenshot()
        screenshot.save(screenshot_path)
        log(f'✅ Скриншот сохранен: {screenshot_path}')
        if os.path.exists(screenshot_path):
            file_size = os.path.getsize(screenshot_path)
            log(f'📁 Размер файла: {file_size} байт')
        else:
            log('❌ Скриншот не создался!')
        result = {'success': True, 'message': 'Бот успешно выполнил действия в лесном плейсе', 'actions_performed': ['Авторизация в Roblox', 'Запуск игры', 'Активация окна Roblox Player', 'Движение вперед (3 сек)', 'Движение влево (2 сек)', 'Движение назад (2 сек)', 'Прыжок', 'Сбор ресурсов', 'Движение вперед (2 сек)', 'Скриншот игрового процесса'], 'screenshot': screenshot_path}
        print(json.dumps(result, ensure_ascii=False))
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception as e:
        error_msg = f'Ошибка: {str(e)}'
        log(f'❌ {error_msg}')
        result = {'success': False, 'error': error_msg, 'screenshot': None}
        print(json.dumps(result, ensure_ascii=False))
    finally:
        log('🔚 Закрываю браузер...')
        driver.quit()
        log('🔚 Браузер закрыт')
        close_roblox_player()
if __name__ == '__main__':
    main()