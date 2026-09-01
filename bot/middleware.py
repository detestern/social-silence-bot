"""
Ограничивает и settings-, и alert-бота списком конкретных telegram user_id
из .env (ALLOWED_TG_USER_IDS=id1,id2 через запятую без пробелов — как и с
ключами Gemini). Не в списке -> апдейт молча игнорируется: бот не отвечает
вообще, чтобы не подтверждать посторонним, что он вообще что-то делает.

Если ALLOWED_TG_USER_IDS не задан или пуст — пропускает всех (удобно на
самом первом шаге, пока список ещё не завели; но в проде стоит задать).
"""
import os

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class AllowedUsersMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        raw = os.environ.get("ALLOWED_TG_USER_IDS", "")
        self.allowed = {int(x.strip()) for x in raw.split(",") if x.strip()}

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = getattr(event, "from_user", None)
        if self.allowed and (user is None or user.id not in self.allowed):
            return  # молча игнорируем — без ответа, без исключения
        return await handler(event, data)
