"""One-time local QR login for the escrow MTProto account.

Run on a trusted local computer, never inside the public bot chat or a public log.
The script prints a Telethon StringSession once; store it as a private deployment secret.
"""
import asyncio
import getpass
import os

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


async def main():
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    if await client.is_user_authorized():
        print("This local session is already authorized.")
    else:
        qr = await client.qr_login()
        print("Open Telegram → Settings → Devices → Link Desktop Device and scan this QR URL:")
        print(qr.url)
        try:
            await qr.wait()
        except SessionPasswordNeededError:
            password = getpass.getpass("Enter the account's 2FA password locally (input is hidden): ")
            await client.sign_in(password=password)
    me = await client.get_me()
    print(f"Authorized account: id={me.id}, username=@{me.username or 'none'}")
    print("Copy the following value directly into the private USERBOT_SESSION_STRING secret.")
    print("Do not send it to anyone or commit it to Git:")
    print(client.session.save())
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
