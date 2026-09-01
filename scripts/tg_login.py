"""
Резервный способ авторизации Telethon — через консоль сервера, если по
каким-то причинам не хочется/нельзя использовать /login прямо в боте.

Использование:
    python scripts/tg_login.py
"""
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()


async def main():
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_name = os.getenv("TG_SESSION_NAME", "triage_userbot")

    client = TelegramClient(session_name, api_id, api_hash)
    await client.start()  # спросит номер телефона + код в консоли
    me = await client.get_me()
    print(f"Успешно авторизован как: {me.first_name} (id={me.id})")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
