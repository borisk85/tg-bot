"""
Запусти один раз локально для получения TELETHON_SESSION.
После этого скрипт больше не нужен.

Нужны: TELEGRAM_API_ID и TELEGRAM_API_HASH из https://my.telegram.org
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = input("Введи TELEGRAM_API_ID: ").strip()
API_HASH = input("Введи TELEGRAM_API_HASH: ").strip()

async def main():
    client = TelegramClient(StringSession(), int(API_ID), API_HASH)
    await client.start()
    session_str = client.session.save()
    await client.disconnect()
    print("\n=== СКОПИРУЙ В RAILWAY VARIABLES ===")
    print(f"TELEGRAM_API_ID={API_ID}")
    print(f"TELEGRAM_API_HASH={API_HASH}")
    print(f"TELETHON_SESSION={session_str}")

asyncio.run(main())
