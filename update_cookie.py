import requests
import uuid
import json
import time
import random


class AdvancedRobloxRefresher:
    def __init__(self):
        self.session = requests.Session()
        self.device_id = str(uuid.uuid4())
        self.setup_advanced_headers()

    def setup_advanced_headers(self):
        """Полная эмуляция браузера Roblox"""
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Dnt": "1",
            "Priority": "u=1, i"
        })

    def get_browser_tracker_headers(self):
        """Headers для имитации браузерного трекера"""
        return {
            "RBXEventTracker": f"browserid={self.device_id}",
            "RBXID": self.device_id,
            "RobloxBrowserId": self.device_id,
        }

    def get_csrf_token(self, cookie):
        """Получаем CSRF токен с правильными заголовками"""
        try:
            temp_session = requests.Session()
            temp_session.cookies.set('.ROBLOSECURITY', cookie)
            temp_session.headers.update(self.session.headers)

            response = temp_session.post(
                'https://auth.roblox.com/v2/login',
                headers=self.get_browser_tracker_headers()
            )
            return response.headers.get('x-csrf-token')
        except Exception as e:
            print(f"❌ CSRF Error: {e}")
            return None

    def full_browser_simulation(self, cookie):
        """Полная симуляция поведения браузера"""
        print("🖥️ Запуск полной браузерной симуляции...")

        # Устанавливаем куку
        self.session.cookies.set('.ROBLOSECURITY', cookie)

        # Получаем CSRF
        csrf_token = self.get_csrf_token(cookie)
        if csrf_token:
            self.session.headers['X-CSRF-TOKEN'] = csrf_token
            print("✅ CSRF токен получен")

        # 1. Начальная навигация
        print("🔹 Шаг 1: Начальная навигация...")
        self.session.get("https://www.roblox.com/", headers=self.get_browser_tracker_headers())
        time.sleep(1)

        # 2. Auth metadata
        print("🔹 Шаг 2: Auth metadata...")
        self.session.get("https://apis.roblox.com/authentication-service/v1/login/metadata")
        time.sleep(0.5)

        # 3. User info
        print("🔹 Шаг 3: User information...")
        self.session.get("https://users.roblox.com/v1/users/authenticated")
        time.sleep(0.5)

        # 4. Economy и транзакции
        print("🔹 Шаг 4: Economy endpoints...")
        endpoints = [
            "https://economy.roblox.com/v1/user/currency",
            "https://economy.roblox.com/v1/transactions",
            "https://inventory.roblox.com/v1/users/1/items/1",
        ]
        for endpoint in endpoints:
            self.session.get(endpoint)
            time.sleep(0.3)

        # 5. Settings (часто триггерит обновление)
        print("🔹 Шаг 5: Account settings...")
        settings_endpoints = [
            "https://accountsettings.roblox.com/v1/email",
            "https://accountsettings.roblox.com/v1/account",
            "https://billing.roblox.com/v1/paymentmethods",
        ]
        for endpoint in settings_endpoints:
            self.session.get(endpoint)
            time.sleep(0.3)

        # 6. Game APIs
        print("🔹 Шаг 6: Game APIs...")
        game_endpoints = [
            "https://games.roblox.com/v1/games",
            "https://catalog.roblox.com/v1/search/items",
            "https://avatar.roblox.com/v1/avatar",
        ]
        for endpoint in game_endpoints:
            self.session.get(endpoint)
            time.sleep(0.3)

        # 7. Заключительные запросы
        print("🔹 Шаг 7: Финальные запросы...")
        final_endpoints = [
            "https://chat.roblox.com/v2/get-conversations",
            "https://friends.roblox.com/v1/my/friends",
            "https://notifications.roblox.com/v1/notifications",
        ]
        for endpoint in final_endpoints:
            self.session.get(endpoint)
            time.sleep(0.3)

        return self.session.cookies.get('.ROBLOSECURITY')

    def validate_cookie(self, cookie):
        """Проверяем валидность куки"""
        try:
            temp_session = requests.Session()
            temp_session.cookies.set('.ROBLOSECURITY', cookie)
            response = temp_session.get(
                'https://users.roblox.com/v1/users/authenticated',
                timeout=10
            )
            if response.status_code == 200:
                user_data = response.json()
                print(f"✅ Кука валидна. User: {user_data.get('name')}")
                return True
            return False
        except:
            return False


def main():
    refresher = AdvancedRobloxRefresher()

    print("🔮 Roblox Cookie Refresher (Продвинутая браузерная эмуляция)")
    print("=" * 60)

    while True:
        print("\n" + "=" * 40)
        old_cookie = input("Введите куку .ROBLOSECURITY (или 'quit' для выхода): ").strip()

        if old_cookie.lower() == 'quit':
            break

        if not old_cookie:
            print("❌ Пустая кука!")
            continue

        # Проверяем исходную куку
        print("\n🔍 Проверяем исходную куку...")
        if not refresher.validate_cookie(old_cookie):
            print("❌ Исходная кука невалидна!")
            continue

        # Запускаем полную симуляцию
        print("\n🚀 Запускаем полную браузерную эмуляцию...")
        print("⏳ Это займет ~10 секунд...")

        start_time = time.time()
        new_cookie = refresher.full_browser_simulation(old_cookie)
        end_time = time.time()

        print(f"\n⏱️ Время выполнения: {end_time - start_time:.2f} сек")

        print("\n" + "=" * 70)
        if new_cookie and new_cookie != old_cookie:
            print("🎉 КУКА УСПЕШНО ОБНОВЛЕНА!")
            print("=" * 70)
            print(f"Старая: {old_cookie[:80]}...")
            print(f"Новая:  {new_cookie[:80]}...")
            print("=" * 70)

            # Проверяем новую куку
            print("🔍 Проверяем новую куку...")
            if refresher.validate_cookie(new_cookie):
                print("✅ Новая кука валидна!")
            else:
                print("❌ Новая кука невалидна!")

        else:
            print("😞 Кука не изменилась после полной эмуляции")
            print("\n💡 Вывод: Тот бот использует:")
            print("  • Selenium/Playwright с реальным браузером")
            print("  • Приватные API endpoints")
            print("  • Специфичную последовательность действий")
            print("  • Или механизм, недоступный через requests")

        print("\n" + "=" * 70)


if __name__ == "__main__":
    main()