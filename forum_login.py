# -*- coding: utf-8 -*-
"""
Мост бота с форумом (сам форум живёт в отдельном репозитории jins-forum и подключается к той же БД).

Здесь осталось ровно две вещи, которым нужен бот-процесс:
  1) обработчик «/start forum_КОД» — подтверждение входа в форум через Telegram (пишет в таблицу forum_login);
  2) таблицы forum_login и forum_chat_prefs (бот читает forum_chat_prefs при отправке push о ЛС: «чат заглушен в форуме»).
Таблицы создаются «IF NOT EXISTS» — форум создаёт те же самые, конфликтов нет.
"""
from __future__ import annotations

import logging
import re

try:
    from aiogram import F, Router
    from aiogram.types import Message
    router = Router(name="forum_login")
except Exception:  # pragma: no cover
    router = None

log = logging.getLogger("jins.forum_login")
bm = None

DDL = """
CREATE TABLE IF NOT EXISTS forum_login(code TEXT PRIMARY KEY, user_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS forum_chat_prefs(
    user_id BIGINT NOT NULL, peer_id BIGINT NOT NULL, pinned_at TIMESTAMPTZ, muted BOOLEAN NOT NULL DEFAULT FALSE, archived BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY(user_id, peer_id));
"""

if router is not None:
    @router.message(F.text.startswith("/start forum_"), F.chat.type == "private")
    async def bot_forum_login(m: Message):
        code = m.text.split("forum_", 1)[1].strip()[:8]
        if not re.fullmatch(r"[0-9a-f]{8}", code):
            await m.answer("Код входа некорректный. Вернитесь в приложение и нажмите «Войти через Telegram» ещё раз.")
            return
        await bm.register_user(m.from_user)
        ok = await bm.pool.fetchval(
            "UPDATE forum_login SET user_id=$2 WHERE code=$1 AND user_id IS NULL AND created_at > now() - interval '15 minutes' RETURNING code", code, m.from_user.id)
        if ok:
            await m.answer("✅ <b>Вход в KMBP FORUM подтверждён.</b>\nВозвращайтесь в приложение — оно откроется само.")
        else:
            await m.answer("Код входа устарел или уже использован. Вернитесь в приложение и нажмите «Войти через Telegram» ещё раз.")


async def start(bot_module):
    """Вызывается в main() после init_db()."""
    global bm
    bm = bot_module
    await bm.pool.execute(DDL)
    log.info("forum_login ready")
