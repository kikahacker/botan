import requests
import json
import sys
import os
import re
from typing import Optional, Dict, Tuple


class RobloxCookieRefresher:
    def __init__(self):
        self.session = requests.Session()
        self.base_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-CSRF-TOKEN': None
        }

    def debug_cookie(self, cookie: str) -> None:
        """Выводит отладочную информацию о куке"""
        print("\n🔍 Отладочная информация о куке:")
        print(f"Длина куки: {len(cookie)} символов")
        print(f"Начинается с: {cookie[:50]}...")
        print(f"Содержит '::': {'::' in cookie}")


        # Проверяем структуру
        if '::' in cookie:
            parts = cookie.split('::')
            print(f"Количество частей после разделения '::': {len(parts)}")
            for i, part in enumerate(parts[:3]):  # Показываем первые 3 части
                print(f"Часть {i}: {part[:30]}... (длина: {len(part)})")

    def validate_cookie(self, cookie: str) -> Tuple[bool, str]:
        """Проверяем валидность куки с детальной диагностикой"""
        try:
            # Очищаем куку
            clean_cookie = cookie.strip()

            # Проверка 1: Длина
            if len(clean_cookie) < 50:
                return False, "Слишком короткая кука (менее 50 символов)"

            # Проверка 2: Основные паттерны Roblox куки
            if not re.match(r'^[_a-zA-Z0-9\-=]+::', clean_cookie):
                return False, "Неверный формат: должна начинаться с идентификатора и '::'"

            # Проверка 3: Наличие необходимых компонентов
            if '::' not in clean_cookie:
                return False, "Отсутствует разделитель '::'"

            parts = clean_cookie.split('::')
            if len(parts) < 2:
                return False, "Недостаточно частей после разделения"

            # Проверка 4: Первая часть (обычно содержит дату/время)
            first_part = parts[0]
            if len(first_part) < 10:
                return False, "Первая часть куки слишком короткая"

            return True, "Кука выглядит валидной"

        except Exception as e:
            return False, f"Ошибка при валидации: {e}"

    def clean_cookie(self, cookie: str) -> str:
        """Очищает куку от лишних символов"""
        # Удаляем кавычки, пробелы по краям
        cleaned = cookie.strip().replace('"', '').replace("'", "")

        # Удаляем возможные префиксы
        if cleaned.startswith('cookie:'):
            cleaned = cleaned[7:].strip()
        if cleaned.startswith('.ROBLOSECURITY='):
            cleaned = cleaned[15:].strip()

        return cleaned

    def get_csrf_token(self, cookie: str) -> Optional[str]:
        """Получаем CSRF токен от Roblox"""
        try:
            # Создаем временную сессию для получения токена
            temp_session = requests.Session()
            temp_session.cookies.set('.ROBLOSECURITY', cookie, domain='.roblox.com')

            headers = {
                'User-Agent': self.base_headers['User-Agent'],
                'Content-Type': 'application/json'
            }

            response = temp_session.post(
                'https://auth.roblox.com/v2/login',
                headers=headers
            )

            if 'x-csrf-token' in response.headers:
                token = response.headers['x-csrf-token']
                print(f"✅ CSRF токен получен: {token[:20]}...")
                return token
            else:
                print("❌ CSRF токен не найден в заголовках ответа")
                return None

        except Exception as e:
            print(f"❌ Ошибка при получении CSRF токена: {e}")
            return None

    def refresh_cookie(self, old_cookie: str) -> Optional[str]:
        """Обновляем куку .ROBLOSECURITY"""
        try:
            # Очищаем куку
            clean_cookie = self.clean_cookie(old_cookie)
            print(f"🔄 Очищенная кука: {clean_cookie[:50]}...")

            # Получаем CSRF токен
            print("🔄 Получаем CSRF токен...")
            csrf_token = self.get_csrf_token(clean_cookie)

            if not csrf_token:
                print("❌ Не удалось получить CSRF токен - кука может быть невалидной")
                return None

            # Настраиваем сессию с кукой и токеном
            self.session.cookies.set('.ROBLOSECURITY', clean_cookie, domain='.roblox.com')
            self.base_headers['X-CSRF-TOKEN'] = csrf_token

            print("🔄 Отправляем запрос на обновление сессии...")

            # Отправляем запрос на обновление
            response = self.session.post(
                'https://auth.roblox.com/v2/login',  # Альтернативный endpoint
                headers=self.base_headers,
                json={"ctype": "Username"}
            )

            print(f"📊 Статус ответа: {response.status_code}")

            if response.status_code == 200:
                # Проверяем куки в ответе
                if '.ROBLOSECURITY' in self.session.cookies:
                    new_cookie = self.session.cookies['.ROBLOSECURITY']
                    print("✅ Новая кука получена из cookies!")
                    return new_cookie

                # Проверяем заголовки Set-Cookie
                if 'Set-Cookie' in response.headers:
                    set_cookie_header = response.headers['Set-Cookie']
                    if '.ROBLOSECURITY' in set_cookie_header:
                        # Извлекаем куку из заголовка
                        match = re.search(r'\.ROBLOSECURITY=([^;]+)', set_cookie_header)
                        if match:
                            new_cookie = match.group(1)
                            print("✅ Новая кука получена из заголовков!")
                            return new_cookie

                print("ℹ️ Новая кука не найдена в ответе, но запрос успешен")
                print("Возможно, нужен другой метод обновления")
                return clean_cookie  # Возвращаем оригинальную, если новая не найдена

            elif response.status_code == 403:
                print("❌ Доступ запрещен (403)")
                print("Возможные причины:")
                print("  - Кука невалидна или просрочена")
                print("  - Аккаунт заблокирован")
                print("  - Нужна капча")
                return None
            elif response.status_code == 401:
                print("❌ Неавторизован (401) - кука недействительна")
                return None
            else:
                print(f"❌ Неожиданный статус: {response.status_code}")
                print(f"Ответ: {response.text[:200]}...")
                return None

        except requests.exceptions.RequestException as e:
            print(f"❌ Ошибка сети: {e}")
            return None
        except Exception as e:
            print(f"❌ Неожиданная ошибка: {e}")
            return None

    def test_cookie(self, cookie: str) -> bool:
        """Тестируем куку на валидность"""
        try:
            clean_cookie = self.clean_cookie(cookie)
            temp_session = requests.Session()
            temp_session.cookies.set('.ROBLOSECURITY', clean_cookie, domain='.roblox.com')

            response = temp_session.get(
                'https://users.roblox.com/v1/users/authenticated',
                headers={'User-Agent': self.base_headers['User-Agent']}
            )

            if response.status_code == 200:
                user_data = response.json()
                print(f"✅ Кука валидна! Пользователь: {user_data.get('name', 'Unknown')}")
                return True
            else:
                print(f"❌ Кука невалидна. Статус: {response.status_code}")
                return False

        except Exception as e:
            print(f"❌ Ошибка при тестировании куки: {e}")
            return False


def main():
    print("=" * 60)
    print("       Roblox Cookie Refresher (Улучшенная версия)")
    print("=" * 60)
    print()

    refresher = RobloxCookieRefresher()

    while True:
        print("\nВыберите вариант:")
        print("1 - Ввести куку вручную")
        print("2 - Загрузить из файла")
        print("3 - Протестировать куку (проверить валидность)")
        print("4 - Выход")

        choice = input("\nВаш выбор (1-4): ").strip()

        if choice == '1':
            print("\n" + "=" * 40)
            print("ВВОД КУКИ:")
            print("=" * 40)
            old_cookie = input("Введите куку .ROBLOSECURITY: ").strip()

            if not old_cookie:
                print("❌ Пустая кука!")
                continue

            # Показываем отладочную информацию
            refresher.debug_cookie(old_cookie)

            # Проверяем валидность
            is_valid, message = refresher.validate_cookie(old_cookie)
            print(f"\n🔍 Результат валидации: {message}")

            if not is_valid:
                print("\n❌ Кука не прошла валидацию!")
                print("Попробуйте:")
                print("  - Скопировать куку заново")
                print("  - Убедиться, что скопирована вся кука")
                print("  - Проверить, нет ли лишних пробелов")
                continue

            # Тестируем куку
            print("\n🧪 Тестируем куку...")
            if refresher.test_cookie(old_cookie):
                print("🔄 Кука валидна, начинаем обновление...")
                new_cookie = refresher.refresh_cookie(old_cookie)
            else:
                print("❌ Кука невалидна, невозможно обновить")
                continue

        elif choice == '2':
            filename = input("Введите имя файла (по умолчанию: cookie.txt): ").strip()
            if not filename:
                filename = "cookie.txt"

            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    old_cookie = f.read().strip()

                if not old_cookie:
                    print("❌ Файл пустой!")
                    continue

                print(f"✅ Кука загружена из файла: {filename}")
                refresher.debug_cookie(old_cookie)

                # Проверяем валидность
                is_valid, message = refresher.validate_cookie(old_cookie)
                print(f"🔍 Результат валидации: {message}")

                if not is_valid:
                    continue

                new_cookie = refresher.refresh_cookie(old_cookie)

            except FileNotFoundError:
                print("❌ Файл не найден!")
                continue
            except Exception as e:
                print(f"❌ Ошибка при чтении файла: {e}")
                continue

        elif choice == '3':
            print("\n🧪 ТЕСТИРОВАНИЕ КУКИ")
            test_cookie = input("Введите куку для тестирования: ").strip()
            if test_cookie:
                refresher.test_cookie(test_cookie)
            continue

        elif choice == '4':
            print("👋 Выход из программы...")
            break
        else:
            print("❌ Неверный выбор!")
            continue

        # Обрабатываем результат обновления
        if new_cookie and new_cookie != old_cookie:
            print("\n" + "=" * 60)
            print("✅ НОВАЯ КУКА УСПЕШНО ПОЛУЧЕНА!")
            print("=" * 60)
            print(new_cookie)
            print("=" * 60)

            # Сохраняем в файл
            save_choice = input("\n💾 Сохранить новую куку в файл? (y/n): ").strip().lower()
            if save_choice == 'y':
                filename = input("Имя файла (по умолчанию: new_cookie.txt): ").strip()
                if not filename:
                    filename = "new_cookie.txt"
                try:
                    with open(filename, 'w', encoding='utf-8') as f:
                        f.write(new_cookie)
                    print(f"✅ Кука сохранена в {filename}")
                except Exception as e:
                    print(f"❌ Ошибка при сохранении: {e}")

        elif new_cookie and new_cookie == old_cookie:
            print("\nℹ️ Кука не изменилась (возможно, уже актуальна)")
        else:
            print("\n❌ Не удалось обновить куку")

        # Продолжить?
        continue_choice = input("\n🔄 Продолжить работу? (y/n): ").strip().lower()
        if continue_choice != 'y':
            print("👋 Выход из программы...")
            break


if __name__ == "__main__":
    main()