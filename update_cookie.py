import requests
import time
import random
import hashlib

from http_shared import PROXY_POOL  # добавили импорт пула проксей


class RobloxCookieRefresher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        })

    def _apply_random_proxy(self):
        """
        Вешает рандомный прокси на requests-сессию.
        Если прокси не заданы (нет файла proxies.txt и переменной окружения),
        то работаем без прокси, как раньше.
        """
        proxy = PROXY_POOL.any()
        if proxy:
            # proxy строка вида: http://user:pass@host:port или http://host:port
            self.session.proxies = {
                "http": proxy,
                "https": proxy,
            }
            print(f"🌐 Использую прокси: {proxy}")
        else:
            # очищаем прокси, чтобы не мешали
            self.session.proxies = {}
            # print("🌐 Прокси не заданы, работаю напрямую")  # можно раскомментить для дебага

    def check_cookie_validity(self, cookie):
        """Проверка валидности куки"""
        try:
            # перед каждым сетевым чеком вешаем рандомный прокси
            self._apply_random_proxy()

            response = self.session.get(
                'https://users.roblox.com/v1/users/authenticated',
                cookies={'.ROBLOSECURITY': cookie},
                timeout=10
            )
            if response.status_code == 200:
                user_data = response.json()
                return True, user_data
            return False, None
        except Exception as e:

            return False, None

    def generate_device_id(self):
        """Генерация уникального device ID"""
        timestamp = str(int(time.time() * 1000))
        random_str = str(random.randint(100000, 999999))
        device_string = f"WEB{timestamp}{random_str}"
        return hashlib.md5(device_string.encode()).hexdigest()

    def refresh_cookie(self, cookie):
        """РАБОЧИЙ МЕТОД обновления куки через session refresh"""


        try:
            # ВАЖНО: весь refresh (CSRF + session/refresh) лучше делать с ОДНОГО IP
            # поэтому один раз ставим прокси в начале метода
            self._apply_random_proxy()

            # Шаг 1: Получаем CSRF токен
            csrf_response = self.session.post(
                'https://auth.roblox.com/v2/login',
                cookies={'.ROBLOSECURITY': cookie}
            )
            csrf_token = csrf_response.headers.get('x-csrf-token')

            if not csrf_token:

                if csrf_response.text:
                    pass
                return None

            # Шаг 2: Используем специальные headers для session refresh
            refresh_headers = {
                'X-CSRF-TOKEN': csrf_token,
                'Roblox-Device-Id': self.generate_device_id(),
                'Roblox-Client-Version': '2024.11.0',
                'Referer': 'https://www.roblox.com/',
                'Origin': 'https://www.roblox.com',
                'Content-Type': 'application/json'
            }

            # Шаг 3: Делаем запрос к session refresh endpoint
            response = self.session.post(
                'https://auth.roblox.com/v1/session/refresh',
                headers=refresh_headers,
                cookies={'.ROBLOSECURITY': cookie},
                json={}  # Пустой JSON body
            )



            if response.status_code == 200:
                # Получаем новый куки из cookies ответа
                new_cookie = response.cookies.get('.ROBLOSECURITY')
                if new_cookie:

                    return new_cookie

                # Если куки нет в cookies, проверяем тело ответа
                try:
                    response_data = response.json()
                    if 'cookie' in response_data:
                        new_cookie = response_data['cookie']

                        return new_cookie
                except Exception:
                    pass

                return None
            else:
                if response.text:
                    pass
                return None

        except Exception as e:

            return None

    def comprehensive_refresh(self, cookie):
        """Комплексное обновление с проверками"""

        original_cookie = cookie

        # Проверяем исходную валидность
        is_valid, user_data = self.check_cookie_validity(cookie)
        if not is_valid:

            return None


        if user_data:
            pass

        # Запускаем рабочий метод
        new_cookie = self.refresh_cookie(cookie)

        if not new_cookie:

            return None

        # Проверяем новый куки
        is_valid, user_data = self.check_cookie_validity(new_cookie)

        if is_valid:
            if user_data:
                pass

            # Сравниваем куки
            if new_cookie != original_cookie:
                pass
            else:
                pass

            return new_cookie
        else:
            return None


def main():

    # Ввод куки
    cookie = input("Введите ваш .ROBLOSECURITY куки: ").strip()

    if not cookie:
        return

    # Создаем экземпляр
    refresher = RobloxCookieRefresher()

    # Запускаем обновление
    start_time = time.time()
    new_cookie = refresher.comprehensive_refresh(cookie)
    end_time = time.time()


    if new_cookie:

        # Дополнительная проверка
        is_valid, user_data = refresher.check_cookie_validity(new_cookie)
        if is_valid:
            pass
        else:
            pass
    else:
        pass


if __name__ == "__main__":
    main()
