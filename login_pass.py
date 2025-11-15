import aiohttp
import json
import asyncio
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass

from aiogram import types, F, Router
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from storage import (
    upsert_user,
    log_event,
    upsert_account_snapshot,
    save_encrypted_cookie,
)
from util.crypto import encrypt_text
from handlers import L, LL, kb_main_i18n, edit_or_send


# ================== AUTH RESULT ==================

@dataclass
class AuthResult:
    success: bool
    cookie: Optional[str] = None
    user_id: Optional[int] = None
    username: Optional[str] = None
    display_name: Optional[str] = None
    requires_2fa: bool = False
    requires_captcha: bool = False
    challenge_id: Optional[str] = None
    challenge_type: Optional[str] = None
    challenge_metadata: Optional[Dict] = None
    twoStepType: Optional[str] = None
    verification_token: Optional[str] = None
    error: Optional[str] = None
    user_data: Optional[Dict] = None


# ================== ROBLOX LOGIN CLIENT ==================

class RobloxLoginPassword:
    def __init__(self):
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.roblox.com",
            "Referer": "https://www.roblox.com/login",
        }

    async def get_csrf_token(self, session: aiohttp.ClientSession) -> Optional[str]:
        try:
            async with session.post("https://auth.roblox.com/v2/login", headers=self.base_headers) as response:
                token = response.headers.get("x-csrf-token")
                print(f"CSRF token received: {token}")
                return token
        except Exception as e:
            print(f"CSRF token error: {e}")
            return None

    async def validate_and_get_user_data(self, cookie: str) -> Tuple[bool, Optional[str], Optional[Dict]]:
        if not cookie or not cookie.strip():
            return False, None, None

        cleaned = cookie.strip()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.roblox.com/",
        }

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(
                        "https://users.roblox.com/v1/users/authenticated",
                        headers=headers,
                        cookies={".ROBLOSECURITY": cleaned},
                ) as response:
                    if response.status == 200:
                        user_data = await response.json()
                        print(f"Cookie validation successful for user: {user_data.get('name')}")
                        return True, cleaned, user_data
                    else:
                        print(f"Cookie validation failed: {response.status}")
        except Exception as e:
            print(f"Cookie validation error: {e}")

        return False, None, None

    async def extract_cookie_from_session(self, session: aiohttp.ClientSession) -> Optional[str]:
        cookies_dict = {}
        for cookie in session.cookie_jar:
            if cookie.key == ".ROBLOSECURITY":
                cookies_dict[cookie.key] = cookie.value
                print(f"Found cookie: {cookie.key}")
        return cookies_dict.get(".ROBLOSECURITY")

    async def safe_json_response(self, response: aiohttp.ClientResponse) -> Tuple[bool, Any]:
        """Безопасное извлечение JSON из ответа"""
        try:
            text = await response.text()
            if text.strip():
                return True, json.loads(text)
            else:
                return False, None
        except json.JSONDecodeError:
            print(f"JSON decode error. Response text: {text[:200]}...")
            return False, None
        except Exception as e:
            print(f"Error reading response: {e}")
            return False, None

    async def login_with_credentials(
            self,
            username: str,
            password: str,
            captcha_token: Optional[str] = None,
            challenge_id: Optional[str] = None,
            twofa_code: Optional[str] = None,
            verification_token: Optional[str] = None,
    ) -> AuthResult:
        async with aiohttp.ClientSession(headers=self.base_headers) as session:
            csrf_token = await self.get_csrf_token(session)
            if not csrf_token:
                return AuthResult(success=False, error="Не удалось получить CSRF токен")

            if verification_token and twofa_code:
                return await self.verify_2fa_and_login(
                    session, csrf_token, challenge_id, verification_token, twofa_code
                )

            login_data = {
                "ctype": "Username",
                "cvalue": username,
                "password": password
            }

            if captcha_token:
                login_data["captchaToken"] = captcha_token
                login_data["captchaProvider"] = "PROVIDER_RECAPTCHA_V2"
                if challenge_id:
                    login_data["challengeId"] = challenge_id

            print(f"Attempting login for user: {username}")

            try:
                async with session.post(
                        "https://auth.roblox.com/v2/login",
                        json=login_data,
                        headers={"X-CSRF-TOKEN": csrf_token, **self.base_headers},
                ) as response:
                    print(f"Login response status: {response.status}")

                    # Безопасно получаем JSON
                    json_ok, response_data = await self.safe_json_response(response)

                    if response.status == 200:
                        print("Login successful!")
                        return await self.handle_successful_login(session, response_data or {})

                    elif response.status == 403:
                        challenge_type = response.headers.get("rblx-challenge-type")
                        challenge_id = response.headers.get("rblx-challenge-id")

                        if challenge_type == "captcha":
                            print(f"Captcha required: {challenge_id}")
                            challenge_metadata_json = response.headers.get("rblx-challenge-metadata")
                            challenge_metadata = {}
                            if challenge_metadata_json:
                                try:
                                    challenge_metadata = json.loads(challenge_metadata_json)
                                except:
                                    pass

                            return AuthResult(
                                success=False,
                                requires_captcha=True,
                                challenge_id=challenge_id,
                                challenge_type=challenge_type,
                                challenge_metadata=challenge_metadata,
                                error="Требуется пройти капчу"
                            )

                        # Проверяем 2FA
                        if json_ok and response_data:
                            return await self.handle_2fa_response(response_data, csrf_token)
                        else:
                            return AuthResult(success=False, error="Ошибка авторизации (403)")

                    elif response.status == 400:
                        if json_ok and response_data:
                            error_msg = response_data.get('message', 'Неверный логин или пароль')
                            return AuthResult(success=False, error=error_msg)
                        else:
                            return AuthResult(success=False, error="Неверный логин или пароль")

                    elif response.status == 429:
                        return AuthResult(success=False, error="Слишком много запросов. Попробуйте позже.")

                    else:
                        error_msg = f"Ошибка сервера: {response.status}"
                        if json_ok and response_data and response_data.get('errors'):
                            error_msg = response_data['errors'][0].get('message', error_msg)
                        return AuthResult(success=False, error=error_msg)

            except Exception as e:
                print(f"Login exception: {e}")
                return AuthResult(success=False, error=f"Ошибка подключения: {str(e)}")

    async def handle_successful_login(self, session: aiohttp.ClientSession, login_result: Dict) -> AuthResult:
        roblox_cookie = await self.extract_cookie_from_session(session)

        if not roblox_cookie:
            return AuthResult(success=False, error="Кука не получена")

        print("Cookie extracted, validating...")
        is_valid, cleaned_cookie, user_data = await self.validate_and_get_user_data(roblox_cookie)
        if not is_valid:
            return AuthResult(success=False, error="Кука не прошла валидацию")

        print(f"Cookie validated for user: {user_data.get('name')}")
        return AuthResult(
            success=True,
            cookie=cleaned_cookie,
            user_id=user_data.get("id"),
            username=user_data.get("name"),
            display_name=user_data.get("displayName"),
            user_data=user_data,
        )

    async def handle_2fa_response(self, response_data: Dict, csrf_token: str) -> AuthResult:
        if response_data.get("isTwoStepVerificationEnabled"):
            two_step_verification = response_data.get("twoStepVerification", {})
            challenge_id = two_step_verification.get("challengeId")
            verification_token = two_step_verification.get("verificationToken")
            twoStepType = two_step_verification.get("twoStepType", "Email")
            user_id = response_data.get("user", {}).get("id")

            if challenge_id and verification_token:
                print(f"2FA required: {twoStepType}")
                return AuthResult(
                    success=False,
                    requires_2fa=True,
                    twoStepType=twoStepType,
                    challenge_id=challenge_id,
                    verification_token=verification_token,
                    user_id=user_id,
                    error=f"Требуется {twoStepType} 2FA код",
                )

        return AuthResult(success=False, error="Ошибка авторизации")

    async def verify_2fa_and_login(
            self,
            session: aiohttp.ClientSession,
            csrf_token: str,
            challenge_id: str,
            verification_token: str,
            code: str,
    ) -> AuthResult:
        try:
            verify_data = {
                "challengeId": challenge_id,
                "verificationToken": verification_token,
                "rememberDevice": True,
                "code": code
            }

            print("Verifying 2FA code...")

            async with session.post(
                    "https://auth.roblox.com/v2/twostepverification/verify",
                    json=verify_data,
                    headers={"X-CSRF-TOKEN": csrf_token, **self.base_headers},
            ) as response:
                print(f"2FA verification response: {response.status}")

                if response.status == 200:
                    print("2FA successful!")
                    return await self.handle_successful_login(session, {})
                else:
                    return AuthResult(success=False, error="Неверный код 2FA")

        except Exception as e:
            print(f"2FA verification error: {e}")
            return AuthResult(success=False, error=f"Ошибка 2FA: {str(e)}")


# ================== FSM ==================

class AuthStates(StatesGroup):
    waiting_username = State()
    waiting_password = State()
    waiting_2fa = State()
    waiting_captcha = State()


# ================== БИЗНЕС-ЛОГИКА ==================

async def login_password_chain_callback(
        telegram_id: int,
        username: str,
        password: str,
        twofa_code: str | None = None,
        challenge_id: str | None = None,
        verification_token: str | None = None,
        captcha_token: str | None = None,
) -> Dict[str, Any]:
    try:
        print(f"Starting login chain for user: {username}")

        roblox_login = RobloxLoginPassword()

        auth_result = await roblox_login.login_with_credentials(
            username=username,
            password=password,
            twofa_code=twofa_code,
            challenge_id=challenge_id,
            verification_token=verification_token,
            captcha_token=captcha_token,
        )

        print(f"Login result: success={auth_result.success}, error={auth_result.error}")

        if not auth_result.success:
            if auth_result.requires_2fa:
                return {
                    "success": False,
                    "requires_2fa": True,
                    "twoStepType": auth_result.twoStepType,
                    "challenge_id": auth_result.challenge_id,
                    "verification_token": auth_result.verification_token,
                    "user_id": auth_result.user_id,
                    "message": f"🔐 Требуется {auth_result.twoStepType} 2FA код. Отправь код:",
                }
            elif auth_result.requires_captcha:
                return {
                    "success": False,
                    "requires_captcha": True,
                    "challenge_id": auth_result.challenge_id,
                    "challenge_type": auth_result.challenge_type,
                    "challenge_metadata": auth_result.challenge_metadata,
                    "message": "🛡️ Обнаружена капча!",
                }
            else:
                return {
                    "success": False,
                    "error": auth_result.error,
                    "message": f"❌ Ошибка авторизации: {auth_result.error}",
                }

        # Успешная авторизация
        rid = int(auth_result.user_data["id"])
        uname = auth_result.user_data.get("name") or ""
        created_at = auth_result.user_data.get("created")

        enc = encrypt_text(auth_result.cookie)

        await save_encrypted_cookie(telegram_id, rid, enc)
        await upsert_user(telegram_id, rid, uname, created_at)
        await log_event("user_linked", telegram_id=telegram_id, roblox_id=rid)

        try:
            await upsert_account_snapshot(rid, username=uname)
        except Exception:
            pass

        return {
            "success": True,
            "message": f"✅ Аккаунт {uname} успешно добавлен!",
            "user_id": rid,
            "username": uname,
            "cookie": auth_result.cookie,
        }

    except Exception as e:
        print(f"Login chain exception: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": f"❌ Ошибка: {str(e)}",
        }


# ================== UI КЛАВИАТУРЫ ==================

def kb_only_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:home")],
        ]
    )


def kb_captcha_options() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Решить капчу в браузере", callback_data="captcha:solve_browser")],
            [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="captcha:retry")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:home")],
        ]
    )


def kb_after_captcha() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я прошел капчу", callback_data="captcha:done")],
            [InlineKeyboardButton(text="🔄 Еще раз", callback_data="captcha:solve_browser")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:home")],
        ]
    )


# ================== ROUTER + HANDLERS ==================

router = Router()


@router.callback_query(F.data == "menu:login_pass")
async def cb_start_login_pass(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AuthStates.waiting_username)
    await edit_or_send(
        call.message,
        "🔐 Введите логин Roblox:",
        reply_markup=kb_only_back(),
    )


@router.message(AuthStates.waiting_username)
async def handle_username(message: types.Message, state: FSMContext):
    await state.update_data(username=message.text)
    await state.set_state(AuthStates.waiting_password)
    await message.answer("🔑 Введите пароль:", reply_markup=kb_only_back())


@router.message(AuthStates.waiting_password)
async def handle_password(message: types.Message, state: FSMContext):
    data = await state.get_data()
    username = data["username"]
    password = message.text

    # Показываем что идет процесс
    processing_msg = await message.answer("⏳ Проверяю данные...")

    result = await login_password_chain_callback(
        telegram_id=message.from_user.id,
        username=username,
        password=password,
    )

    # Удаляем сообщение о процессе
    try:
        await processing_msg.delete()
    except:
        pass

    if result.get("success"):
        await state.clear()
        await message.answer(result["message"], reply_markup=await kb_main_i18n(message.from_user.id))
    elif result.get("requires_2fa"):
        await state.update_data(
            challenge_id=result["challenge_id"],
            verification_token=result["verification_token"],
            twoStepType=result["twoStepType"],
            user_id=result["user_id"],
        )
        await state.set_state(AuthStates.waiting_2fa)
        await message.answer(result["message"])
    elif result.get("requires_captcha"):
        await state.update_data(
            challenge_id=result["challenge_id"],
            challenge_metadata=result.get("challenge_metadata"),
        )
        await state.set_state(AuthStates.waiting_captcha)

        captcha_message = (
            "🛡️ *Обнаружена капча!*\n\n"
            "Roblox требует подтверждение, что вы не бот.\n\n"
            "📋 *Что делать:*\n"
            "1. Нажмите кнопку '🌐 Решить капчу в браузере'\n"
            "2. Войдите в свой аккаунт Roblox\n"
            "3. Пройдите капчу\n"
            "4. Вернитесь сюда и нажмите '✅ Я прошел капчу'\n\n"
            "После этого бот попробует завершить авторизацию."
        )

        await message.answer(
            captcha_message,
            reply_markup=kb_captcha_options(),
            parse_mode="Markdown"
        )
    else:
        await state.clear()
        await message.answer(result["message"], reply_markup=await kb_main_i18n(message.from_user.id))


@router.callback_query(F.data == "captcha:solve_browser")
async def handle_captcha_solve_browser(call: types.CallbackQuery, state: FSMContext):
    """Отправляем пользователю ссылку для решения капчи"""
    data = await state.get_data()
    challenge_id = data.get("challenge_id")

    # Создаем ссылку для логина
    login_url = "https://www.roblox.com/login"
    if challenge_id:
        login_url += f"?challengeId={challenge_id}"

    instructions = (
        "🌐 *Решите капчу в браузере:*\n\n"
        f"🔗 [Нажмите чтобы открыть Roblox Login]({login_url})\n\n"
        "📋 *Пошаговая инструкция:*\n"
        "1. Нажмите на ссылку выше\n"
        "2. Введите ваш логин и пароль\n"
        "3. Пройдите капчу (отметьте 'Я не робот')\n"
        "4. Убедитесь что успешно вошли в аккаунт\n"
        "5. Вернитесь сюда и нажмите кнопку ниже\n\n"
        "💡 *Совет:* Не закрывайте этот чат пока решаете капчу!"
    )

    await edit_or_send(
        call.message,
        instructions,
        reply_markup=kb_after_captcha(),
        parse_mode="Markdown",
        disable_web_page_preview=False
    )


@router.callback_query(F.data == "captcha:retry")
async def handle_captcha_retry(call: types.CallbackQuery, state: FSMContext):
    """Пробуем авторизоваться снова"""
    await call.answer("Пробую авторизоваться...")

    data = await state.get_data()
    username = data["username"]
    password = data["password"]

    result = await login_password_chain_callback(
        telegram_id=call.from_user.id,
        username=username,
        password=password,
    )

    if result.get("success"):
        await state.clear()
        await edit_or_send(call.message, result["message"], reply_markup=await kb_main_i18n(call.from_user.id))
    elif result.get("requires_captcha"):
        await edit_or_send(
            call.message,
            "❌ Капча все еще требуется. Попробуйте решить ее в браузере.",
            reply_markup=kb_captcha_options()
        )
    else:
        await edit_or_send(
            call.message,
            f"❌ {result['message']}",
            reply_markup=kb_captcha_options()
        )


@router.callback_query(F.data == "captcha:done")
async def handle_captcha_done(call: types.CallbackQuery, state: FSMContext):
    """Пользователь прошел капчу и готов к повторной попытке"""
    await call.answer("Проверяю авторизацию...")

    data = await state.get_data()
    username = data["username"]
    password = data["password"]

    # Ждем немного чтобы капча точно прошла
    await asyncio.sleep(3)

    result = await login_password_chain_callback(
        telegram_id=call.from_user.id,
        username=username,
        password=password,
    )

    if result.get("success"):
        await state.clear()
        await edit_or_send(call.message, result["message"], reply_markup=await kb_main_i18n(call.from_user.id))
    else:
        await edit_or_send(
            call.message,
            "❌ Не удалось авторизоваться. Возможно:\n"
            "• Капча не была пройдена\n"
            "• Вы не вошли в аккаунт\n"
            "• Сессия истекла\n\n"
            "Попробуйте снова или используйте загрузку куки.",
            reply_markup=kb_after_captcha()
        )


@router.message(AuthStates.waiting_2fa)
async def handle_2fa_code(message: types.Message, state: FSMContext):
    data = await state.get_data()
    challenge_id = data.get("challenge_id")
    verification_token = data.get("verification_token")
    code = message.text

    result = await login_password_chain_callback(
        telegram_id=message.from_user.id,
        username="",
        password="",
        twofa_code=code,
        challenge_id=challenge_id,
        verification_token=verification_token,
    )

    await state.clear()
    await message.answer(result["message"], reply_markup=await kb_main_i18n(message.from_user.id))