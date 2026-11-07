"""MTProto gift watcher and restricted collectible-gift release worker.

This worker deliberately does NOT convert ordinary gifts automatically: conversion
permanently destroys the gift, and Telegram does not provide a generic Stars transfer
from a user account to an arbitrary user. Non-transferable/paid-transfer gifts remain
held for an administrator's manual resolution.
"""
import asyncio
import json
import logging
import html
import os
import re
import signal
import sys
from datetime import datetime, timezone

import asyncpg
from aiohttp import ClientSession
from telethon import TelegramClient, functions, types
from telethon.errors import AuthKeyError, AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedError
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kmbp_escrow_userbot")

DATABASE_URL = os.environ["DATABASE_URL"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
EXIT_UNAUTHORIZED = 3  # supervisor в bot.py удалит отозванную сессию и уведомит владельца
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))


async def notify(http, user_id, text, reply_markup=None):
    if not BOT_TOKEN or not user_id:
        return
    try:
        payload = {"chat_id": int(user_id), "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with http.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                             json=payload) as response:
            if response.status >= 300:
                log.warning("Bot notification failed: %s", response.status)
    except Exception:
        log.exception("notification failed")


def peer_user_id(peer):
    if isinstance(peer, types.PeerUser):
        return int(peer.user_id)
    return None


def saved_gift_ref(item):
    if getattr(item, "saved_id", None):
        return f"saved:{int(item.saved_id)}"
    if getattr(item, "msg_id", None):
        return f"message:{int(item.msg_id)}"
    return None


def gift_snapshot(item):
    gift = getattr(item, "gift", None)
    return {
        "gift_type": type(gift).__name__ if gift else "unknown",
        "gift_id": getattr(gift, "id", None),
        "stars": getattr(gift, "stars", None),
        "convert_stars": int(getattr(item, "convert_stars", 0) or 0),
        "transfer_stars": int(getattr(item, "transfer_stars", 0) or 0),
        "is_collectible": bool(gift and "Unique" in type(gift).__name__),
        "saved_id": getattr(item, "saved_id", None),
        "msg_id": getattr(item, "msg_id", None),
        "date": str(getattr(item, "date", "")),
    }


def stars_to_int(value):
    """balance в новых слоях Telegram — объект StarsAmount(amount, nanos) / StarsTonAmount, а не число.
    Раньше int(StarsAmount) падал, ошибка глоталась и баланс всегда становился 0."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    amount = getattr(value, "amount", None)
    if amount is not None:
        return int(amount)
    return int(value)


async def refresh_balance(client):
    """Баланс Stars юзербота или None, если прочитать не удалось (тогда прежнее значение в БД не затираем)."""
    try:
        status = await client(functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf()))
        bal = stars_to_int(getattr(status, "balance", None))
        log.info("Userbot Stars balance: %s (raw: %r)", bal, getattr(status, "balance", None))
        return bal
    except Exception:
        log.exception("Could not read userbot Stars balance")
        return None


async def scan_incoming_gifts(client, pool, http):
    result = await client(functions.payments.GetSavedStarGiftsRequest(
        peer=types.InputPeerSelf(), offset="", limit=100))
    for item in getattr(result, "gifts", []) or []:
        sender_id = peer_user_id(getattr(item, "from_id", None))
        ref = saved_gift_ref(item)
        if not sender_id or not ref:
            continue
        snapshot = gift_snapshot(item)
        async with pool.acquire() as conn:
            async with conn.transaction():
                value = snapshot["convert_stars"] or int(snapshot.get("stars") or 0)
                deal = await conn.fetchrow("""SELECT * FROM escrow_deals WHERE buyer_id=$1 AND status='awaiting_asset'
                    AND created_at <= now() ORDER BY created_at ASC LIMIT 1 FOR UPDATE""", sender_id)
                if not deal or value < int(deal["amount"]):
                    log.info("Unmatched incoming gift ref=%s sender=%s value=%s", ref, sender_id, value)
                    continue
                inserted = await conn.fetchval(
                    "INSERT INTO escrow_processed_gifts(telegram_ref,sender_id,gift_json) VALUES($1,$2,$3::jsonb) "
                    "ON CONFLICT(telegram_ref) DO NOTHING RETURNING telegram_ref",
                    ref, sender_id, json.dumps(snapshot))
                if not inserted:
                    continue
                await conn.execute("""INSERT INTO escrow_assets(deal_id,sender_id,kind,telegram_ref,gift_json,convert_stars,is_collectible,transfer_stars)
                    VALUES($1,$2,'telegram_gift',$3,$4::jsonb,$5,$6,$7) ON CONFLICT(deal_id) DO NOTHING""",
                    deal["id"], sender_id, ref, json.dumps(snapshot), snapshot["convert_stars"],
                    snapshot["is_collectible"], snapshot["transfer_stars"])
                await conn.execute("UPDATE escrow_deals SET status='in_progress',asset_kind='gift',asset_ref=$2,updated_at=now() WHERE id=$1",
                                   deal["id"], ref)
                await conn.execute("INSERT INTO escrow_events(deal_id,actor_id,event,details) VALUES($1,$2,'gift_received',$3::jsonb)",
                                   deal["id"], sender_id, json.dumps(snapshot))
                await conn.execute("INSERT INTO escrow_messages(deal_id,sender_id,body,system) VALUES($1,$2,$3,TRUE)",
                                   deal["id"], sender_id, "Юзербот обнаружил входящий подарок и связал его со сделкой. Подарок остаётся на аккаунте гаранта.")
        await notify(http, deal["buyer_id"], f"Подарок по сделке #{deal['id']} обнаружен на аккаунте гаранта.")
        await notify(http, deal["seller_id"], f"Подарок по сделке #{deal['id']} поступил гаранту. Можно приступать к выполнению условий.")


async def try_release_collectible(client, pool, http):
    rows = await pool.fetch("""SELECT d.*,a.telegram_ref,a.gift_json FROM escrow_deals d
        JOIN escrow_assets a ON a.deal_id=d.id
        WHERE d.status IN ('payout_pending','awaiting_seller','refund_pending') AND d.status<>'disputed'
        AND (d.seller_confirmed_at IS NOT NULL OR (d.release_deadline IS NOT NULL AND d.release_deadline<=now()))
        AND a.transferred_at IS NULL ORDER BY d.id LIMIT 10""")
    for row in rows:
        snap = row["gift_json"]
        if not snap.get("is_collectible") or int(snap.get("transfer_stars") or 0) > 0:
            code = "gift_not_eligible_for_free_transfer"
            if row["last_error"] != code:
                await pool.execute("UPDATE escrow_deals SET last_error=$2,updated_at=now() WHERE id=$1", row["id"], code)
                await notify(http, OWNER_ID, f"Сделка #{row['id']}: подарок нельзя автоматически передать бесплатно. Нужна ручная проверка.")
            continue
        target_user = row["buyer_id"] if row["status"] == "refund_pending" else row["seller_id"]
        try:
            if row["telegram_ref"].startswith("saved:"):
                saved_id = int(row["telegram_ref"].split(":", 1)[1])
                gift_input = types.InputSavedStarGiftChat(peer=types.InputPeerSelf(), saved_id=saved_id)
            else:
                msg_id = int(row["telegram_ref"].split(":", 1)[1])
                gift_input = types.InputSavedStarGiftUser(msg_id=msg_id)
            target_peer = await client.get_input_entity(int(target_user))
            await client(functions.payments.TransferStarGiftRequest(stargift=gift_input, to_id=target_peer))
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("UPDATE escrow_assets SET transferred_at=now() WHERE deal_id=$1", row["id"])
                    final_status = "refunded" if row["status"] == "refund_pending" else "completed"
                    outcome = final_status
                    await conn.execute("UPDATE escrow_deals SET status=$2,closed_at=now(),updated_at=now(),last_error=NULL WHERE id=$1", row["id"], final_status)
                    await conn.execute("INSERT INTO escrow_events(deal_id,event,details) VALUES($1,$2,$3::jsonb)",
                                       row["id"], "gift_returned" if final_status == "refunded" else "gift_transferred",
                                       json.dumps({"to_user": target_user, "telegram_ref": row["telegram_ref"]}))
                    await conn.execute("INSERT INTO escrow_receipts(deal_id,outcome,data) VALUES($1,$2,$3::jsonb) ON CONFLICT DO NOTHING",
                                       row["id"], outcome, json.dumps({"status": final_status, "telegram_ref": row["telegram_ref"]}))
                    await conn.execute("INSERT INTO escrow_messages(deal_id,sender_id,body,system) VALUES($1,$2,$3,TRUE)",
                                       row["id"], target_user, "Telegram подтвердил возврат подарка покупателю." if final_status == "refunded" else "Telegram подтвердил передачу коллекционного подарка продавцу. Сделка завершена.")
            if final_status == "refunded":
                await notify(http, row["buyer_id"], f"Подарок по сделке #{row['id']} возвращён.")
                await notify(http, row["seller_id"], f"По сделке #{row['id']} оформлен возврат покупателю.")
            else:
                await notify(http, row["buyer_id"], f"Сделка #{row['id']} завершена. Подарок передан продавцу.")
                await notify(http, row["seller_id"], f"Сделка #{row['id']} завершена. Подарок поступил вам.")
                await offer_delete_listing(pool, http, row["seller_id"], row["id"])
        except Exception as exc:
            log.exception("Gift transfer failed for deal %s", row["id"])
            error_text = str(exc)[:500]
            if row["last_error"] != error_text:
                await pool.execute("UPDATE escrow_deals SET last_error=$2,updated_at=now() WHERE id=$1",
                                   row["id"], error_text)
                await notify(http, OWNER_ID, f"Сделка #{row['id']}: Telegram не разрешил автоматическую передачу. Требуется ручное решение.")


def _ad_title(text):
    t = re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()
    return (t.split("\n")[0] if t else "Объявление")[:60]


async def offer_delete_listing(pool, http, seller_id, deal_id):
    """После успешной сделки предлагает продавцу удалить объявление (кнопки обрабатывает bot.py: pd:y:<id>)."""
    try:
        listing = await pool.fetchval("SELECT listing_post_id FROM escrow_deals WHERE id=$1", deal_id)
        rows = []
        if listing:
            rows = await pool.fetch("SELECT id,text FROM posts WHERE id=$1 AND user_id=$2 AND active", listing, int(seller_id))
        if not rows:
            rows = await pool.fetch(
                "SELECT id,text FROM posts WHERE user_id=$1 AND active AND expires_at>now() ORDER BY id DESC LIMIT 5", int(seller_id))
        if not rows:
            return
        kb = [[{"text": f"🗑 Удалить «{_ad_title(r['text'])[:28]}»", "callback_data": f"pd:y:{r['id']}"}] for r in rows]
        kb.append([{"text": "Оставить", "callback_data": "pd:n:0"}])
        text = ("🎉 Сделка успешно завершена!\nОбъявление уже неактуально? Удалите его, чтобы оно не висело в ленте и в группе."
                if listing and len(rows) == 1 else
                "🎉 Сделка успешно завершена!\nПродали товар? Выберите объявление, которое нужно удалить (из ленты и из группы).")
        await notify(http, seller_id, text, {"inline_keyboard": kb})
    except Exception:
        log.exception("offer_delete_listing failed")


async def expire_unpaid(pool, http):
    rows = await pool.fetch("""UPDATE escrow_deals SET status='cancelled',closed_at=now(),updated_at=now(),last_error='payment_timeout'
        WHERE status IN ('awaiting_fee','awaiting_asset') AND payment_deadline<=now()
        RETURNING id,buyer_id,seller_id,fee_paid,fee_charge_id""")
    for row in rows:
        fee_refunded = False
        if row["fee_paid"] and row["fee_charge_id"] and BOT_TOKEN:
            try:
                async with http.post(f"https://api.telegram.org/bot{BOT_TOKEN}/refundStarPayment",
                                     json={"user_id": int(row["buyer_id"]),
                                           "telegram_payment_charge_id": row["fee_charge_id"]}) as resp:
                    result = await resp.json()
                    fee_refunded = bool(result.get("ok"))
            except Exception:
                log.exception("fee refund failed for deal %s", row["id"])
        await pool.execute("INSERT INTO escrow_receipts(deal_id,outcome,data) VALUES($1,'cancelled',$2::jsonb) ON CONFLICT DO NOTHING",
                           row["id"], json.dumps({"reason": "payment_timeout", "fee_refunded": fee_refunded}))
        await pool.execute("INSERT INTO escrow_events(deal_id,event,details) VALUES($1,'payment_timeout',$2::jsonb)",
                           row["id"], json.dumps({"fee_refunded": fee_refunded}))
        await notify(http, row["buyer_id"], f"Сделка #{row['id']} отменена: время ожидания оплаты/подарка истекло. Комиссия { 'возвращена' if fee_refunded else 'не была возвращена автоматически' }.")
        await notify(http, row["seller_id"], f"Сделка #{row['id']} отменена: покупатель не завершил оплату.")


async def _init_conn(conn):
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def load_session(pool):
    """Сессия берётся из БД (подключается в админке бота); USERBOT_SESSION_STRING — запасной вариант."""
    row = await pool.fetchval("SELECT session FROM userbot_session WHERE id=1")
    return row or os.environ.get("USERBOT_SESSION_STRING", "")


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3, statement_cache_size=0, init=_init_conn)
    http = ClientSession()
    client = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    exit_code = 0
    try:
        await pool.execute("UPDATE escrow_config SET userbot_online=FALSE,updated_at=now() WHERE id=1")
        session = await load_session(pool)
        if not session:
            log.error("Нет сессии юзербота: подключите аккаунт в админке бота")
            return 2
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            log.error("Сессия юзербота не авторизована (отозвана?)")
            return EXIT_UNAUTHORIZED
        me = await client.get_me()
        log.info("Escrow MTProto worker connected as id=%s username=%s", me.id, me.username)
        low_alerted = False
        while not stop.is_set():
            try:
                balance = await refresh_balance(client)
                if balance is None:
                    await pool.execute("UPDATE escrow_config SET userbot_online=TRUE,updated_at=now() WHERE id=1")
                    balance = int(await pool.fetchval("SELECT userbot_balance FROM escrow_config WHERE id=1") or 0)
                else:
                    await pool.execute("UPDATE escrow_config SET userbot_online=TRUE,userbot_balance=$1,updated_at=now() WHERE id=1", balance)
                cfg = await pool.fetchrow("SELECT reserve_min FROM escrow_config WHERE id=1")
                if cfg and balance < cfg["reserve_min"] and not low_alerted:
                    await notify(http, OWNER_ID, f"⚠️ Резерв Stars юзербота низкий: {balance} ⭐. Проверка резерва: некоторые платные операции могут быть недоступны.")
                    low_alerted = True
                elif cfg and balance >= cfg["reserve_min"]:
                    low_alerted = False
                await scan_incoming_gifts(client, pool, http)
                await try_release_collectible(client, pool, http)
                await expire_unpaid(pool, http)
            except (AuthKeyError, AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedError):
                log.error("Сессия юзербота отозвана в Telegram")
                exit_code = EXIT_UNAUTHORIZED
                break
            except Exception:
                log.exception("worker iteration failed")
            try:
                await asyncio.wait_for(stop.wait(), 20)
            except asyncio.TimeoutError:
                pass
        return exit_code
    finally:
        try:
            await pool.execute("UPDATE escrow_config SET userbot_online=FALSE,updated_at=now() WHERE id=1")
        except Exception:
            pass
        if client:
            await client.disconnect()
        await http.close()
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
