
# lang_guard.py — enforce per-user language on every outgoing message & at update entry.
from typing import Any, Callable, Awaitable, Optional
import asyncio

def _import_i18n():
    import i18n
    return i18n

def _import_storage():
    import storage
    return storage

async def _get_user_lang(user_id: int) -> str:
    lang = None
    try:
        storage = _import_storage()
        if hasattr(storage, "get_user_lang"):
            fn = storage.get_user_lang
        elif hasattr(storage, "user_get_lang"):
            fn = storage.user_get_lang
        elif hasattr(storage, "get_lang"):
            fn = storage.get_lang
        else:
            fn = None
        if fn is not None:
            if asyncio.iscoroutinefunction(fn):
                lang = await fn(user_id)
            else:
                lang = fn(user_id)
    except Exception:
        pass
    lang = (lang or "en").lower()
    if lang.startswith("ru"):
        return "ru"
    if lang.startswith("en"):
        return "en"
    if lang.startswith("uk") or lang.startswith("ua"):
        return "uk"
    return lang or "en"

async def _ensure_lang_for_user_id(user_id: Optional[int]) -> None:
    if not user_id:
        return
    i18n = _import_i18n()
    try:
        lang = await _get_user_lang(int(user_id))
        i18n.set_current_lang(lang)
    except Exception:
        try:
            i18n.set_current_lang(getattr(i18n, "DEFAULT_LANG", "en"))
        except Exception:
            pass

async def use_lang_from_message(message) -> None:
    uid = getattr(getattr(message, "from_user", None), "id", None)
    await _ensure_lang_for_user_id(uid)

async def use_lang_from_call(call) -> None:
    uid = getattr(getattr(call, "from_user", None), "id", None)
    await _ensure_lang_for_user_id(uid)

def init_lang_guard() -> None:
    try:
        from aiogram.types import Message
    except Exception:
        return

    if getattr(Message, "_rbx_lang_patch_done", False):
        return

    async def _ensure(self):
        uid = getattr(getattr(self, "from_user", None), "id", None)
        await _ensure_lang_for_user_id(uid)

    def _wrap_method(meth_name: str):
        orig = getattr(Message, meth_name, None)
        if not orig or getattr(Message, f"__orig_{meth_name}", None):
            return
        async def _wrapped(self, *args, **kwargs):
            await _ensure(self)
            return await orig(self, *args, **kwargs)
        setattr(Message, f"__orig_{meth_name}", orig)
        setattr(Message, meth_name, _wrapped)

    to_patch = [
        "answer", "reply",
        "edit_text", "edit_caption", "edit_media", "edit_reply_markup",
        "answer_photo", "answer_document", "answer_video", "answer_animation",
        "answer_audio", "answer_voice", "answer_location", "answer_contact",
        "answer_sticker"
    ]

    for name in to_patch:
        if hasattr(Message, name):
            _wrap_method(name)

    Message._rbx_lang_patch_done = True

class EnsureLangMiddleware:
    def __init__(self) -> None:
        pass

    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]], event: Any, data: dict):
        uid = None
        user = getattr(event, "from_user", None) or data.get("event_from_user")
        if user:
            uid = getattr(user, "id", None)
        await _ensure_lang_for_user_id(uid)
        return await handler(event, data)

def attach_lang_middlewares(router) -> None:
    try:
        router.message.middleware(EnsureLangMiddleware())
        router.callback_query.middleware(EnsureLangMiddleware())
    except Exception:
        pass
