import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from dotenv import load_dotenv

from adapters.telegram import TelegramAdapter
from bot.alert_handlers import router as alert_router
from bot.handlers import ADAPTERS, router as settings_router, set_alert_bot
from bot.middleware import AllowedUsersMiddleware
from core.listener import run_listener
from core.scheduler import run_hourly_scheduler
from core.digest import run_daily_digest_scheduler
from db.session import init_db

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Отключаем шумный INFO-лог aiogram на каждый апдейт — остальные логгеры без изменений.
logging.getLogger("aiogram.event").setLevel(logging.WARNING)

SETTINGS_BOT_COMMANDS = [
    BotCommand(command="start", description="Как пользоваться этим ботом"),
    BotCommand(command="login", description="Авторизовать чтение чатов"),
    BotCommand(command="platform", description="Выбрать платформу для настроек"),
    BotCommand(command="chats", description="Выбрать чаты для мониторинга"),
    BotCommand(command="find", description="Найти чат по названию"),
    BotCommand(command="monitored", description="Снять мониторинг с чата"),
    BotCommand(command="school", description="Создать школу и привязать к ней чаты"),
    BotCommand(command="schools", description="Список школ и их чатов"),
    BotCommand(command="profile", description="Посмотреть/изменить описание себя"),
    BotCommand(command="add_rule", description="Добавить приоритетное правило"),
    BotCommand(command="rules", description="Список активных правил"),
    BotCommand(command="stats", description="Сводка за сегодня"),
    BotCommand(command="settings", description="Настройки расписания разбора и сводки"),
    BotCommand(command="api", description="Добавить резервные ключи Gemini"),
    BotCommand(command="fresh", description="Тест: разобрать все свежие сообщения сейчас"),
    BotCommand(command="daily", description="Тест: составить дневную сводку сейчас"),
]


def _make_dispatcher(router) -> Dispatcher:
    dp = Dispatcher()
    mw = AllowedUsersMiddleware()
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    dp.include_router(router)
    return dp


async def main():
    await init_db()

    # Пока один источник — telegram. Другие адаптеры добавляются сюда же.
    telegram_adapter = TelegramAdapter()
    ADAPTERS["telegram"] = telegram_adapter

    # settings — команды и настройка, alert — только уведомления.
    settings_bot = Bot(token=os.environ["TG_BOT_TOKEN"])
    alert_bot = Bot(token=os.environ["TG_ALERT_BOT_TOKEN"])

    settings_dp = _make_dispatcher(settings_router)
    alert_dp = _make_dispatcher(alert_router)

    await settings_bot.set_my_commands(SETTINGS_BOT_COMMANDS)
    await settings_bot.set_my_name("Фемида")
    await alert_bot.set_my_name("Ирида")

    alert_bot_id = (await alert_bot.get_me()).id
    # Нужен для пересылки через Telethon прямо в чат с alert-ботом — это
    # другой id, чем aiogram-овский chat_id.
    set_alert_bot(alert_bot, alert_bot_id)

    logger.info("Запускаю оба бота и слушатель сообщений параллельно.")

    # aiogram сам ловит SIGINT и гасит только polling — остальные бесконечные
    # задачи об этом не узнают. Поэтому ждём FIRST_COMPLETED и явно гасим
    # всё остальное сами.
    settings_polling_task = asyncio.create_task(settings_dp.start_polling(settings_bot))
    alert_polling_task = asyncio.create_task(alert_dp.start_polling(alert_bot))
    listener_task = asyncio.create_task(run_listener(telegram_adapter, alert_bot, alert_bot_id))
    scheduler_task = asyncio.create_task(run_hourly_scheduler(alert_bot, telegram_adapter, alert_bot_id))
    digest_task = asyncio.create_task(run_daily_digest_scheduler(alert_bot, telegram_adapter, alert_bot_id))
    tasks = {settings_polling_task, alert_polling_task, listener_task, scheduler_task, digest_task}

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # dp.start_polling() крутит свой внутренний цикл — просто отменить
    # обёртку снаружи недостаточно (гонка с закрытием сессии бота ниже),
    # поэтому явно зовём stop_polling(). Если диспетчер уже остановился
    # сам (поймал Ctrl+C раньше нас) — RuntimeError тут ожидаемый, гасим.
    for dp in (settings_dp, alert_dp):
        try:
            await dp.stop_polling()
        except RuntimeError:
            pass

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    await settings_bot.session.close()
    await alert_bot.session.close()
    await telegram_adapter.client.disconnect()

    logger.info("Остановлено.")

    # Пробрасываем ошибку, если что-то завершилось из-за исключения, а не штатно.
    for task in done:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc is not None:
            raise exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
