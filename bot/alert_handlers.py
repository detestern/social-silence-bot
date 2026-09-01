"""
Alert-бот отдельный от settings-бота: только присылает уведомления, никаких
команд настройки тут нет и не будет — чтобы не путать её кнопками/командами
там, где должны быть только пуши. Единственная команда — /start, чтобы
зафиксировать, в какой chat_id слать уведомления (User.tg_notify_chat_id).
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from db.models import User
from db.session import get_session

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    async with get_session() as session:
        user = (await session.execute(select(User))).scalars().first()
        if user is None:
            user = User(display_name="Она", tg_notify_chat_id=message.chat.id)
            session.add(user)
        else:
            user.tg_notify_chat_id = message.chat.id
        await session.commit()

    await message.answer("Готово! 🌸 Все важные сообщения теперь будут прилетать сюда.")
