import asyncio
import html
import logging
import os
from zoneinfo import ZoneInfo

import asyncpg
from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("birzha")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
OWNER_ID = int(os.environ["OWNER_ID"])  # старший админ (ваш Telegram ID)
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
PORT = int(os.getenv("PORT", "10000"))

GROUPS = {"group", "supergroup"}

DEFAULT_TOPICS = [
    "предложка", "Чатик общения", "Набор в админы", "Платный пиар",
    "Взаимный пиар", "Поиск админов/совлд и т.д", "Монтажёры",
    "вопросы и правила", "продажа ТГК, ботов", "архив заявок", "Отзывы",
    "Идеи кмбп", "разработка и оформление", "Поиск владельцев ботов",
    "Поиск партнёров", "Нестандартный пиар",
]

# key: (название, значение по умолчанию, подсказка про переменные)
TEXTS = {
    "welcome": (
        "Приветствие (/start)",
        "Привет! 👋 Здесь можно подать заявку на размещение поста в чате "
        "«Лесная биржа кмбп». Заявку проверит админ.",
        "",
    ),
    "choose_topic": ("Выбор темы", "Выберите тему, в которую хотите опубликовать пост:", ""),
    "ask_post": (
        "Просьба прислать пост",
        "Тема: {topic}\n\nОтправьте текст вашего поста одним сообщением.",
        "{topic} — название темы",
    ),
    "sent_ok": (
        "Заявка отправлена",
        "✅ Заявка отправлена админам на проверку. Ожидайте ответа.",
        "",
    ),
    "approved": ("Заявка одобрена", "✅ Вашу заявку одобрили, пост опубликован!", ""),
    "rejected": (
        "Заявка отклонена",
        "❌ Админ отклонил вашу заявку.\nПричина: {reason}",
        "{reason} — причина от админа",
    ),
    "edited": (
        "Админ исправил пост",
        "✏️ Админ подтвердил выкладывание поста, но немного его исправил:\n\n"
        "{text}\n\nХотите отправить так? Или отказаться?",
        "{text} — исправленный пост",
    ),
    "edit_accepted": (
        "Пользователь принял исправление",
        "✅ Отлично, пост опубликован в исправленном виде!",
        "",
    ),
    "edit_declined": (
        "Пользователь отказался от исправления",
        "🚫 Вы отклонили исправление. Пост не опубликован.",
        "",
    ),
    "closed": ("Приём закрыт", "Приём заявок временно закрыт.", ""),
}

pool: asyncpg.Pool
router = Router()


# ---------------------------------------------------------------- DB
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS texts(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admins(user_id BIGINT PRIMARY KEY, role TEXT NOT NULL, label TEXT);
        CREATE TABLE IF NOT EXISTS topics(
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, thread_id BIGINT, sort INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS requests(
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, username TEXT, full_name TEXT,
            topic_id INT, topic_name TEXT, text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(), status TEXT NOT NULL DEFAULT 'pending',
            edited_text TEXT, handled_by BIGINT, handler_label TEXT,
            mod_chat_id BIGINT, mod_msg_id BIGINT);
        CREATE TABLE IF NOT EXISTS pending(
            admin_id BIGINT PRIMARY KEY, request_id INT NOT NULL, action TEXT NOT NULL);
        """
    )
    if await pool.fetchval("SELECT count(*) FROM topics") == 0:
        for i, name in enumerate(DEFAULT_TOPICS):
            await pool.execute("INSERT INTO topics(name, sort) VALUES($1, $2)", name, i)


async def get_setting(key):
    return await pool.fetchval("SELECT value FROM settings WHERE key=$1", key)


async def set_setting(key, value):
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES($1,$2) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, str(value))


async def get_text(key):
    v = await pool.fetchval("SELECT value FROM texts WHERE key=$1", key)
    return v if v is not None else TEXTS[key][1]


def fill(template, **kw):
    for k, v in kw.items():
        template = template.replace("{" + k + "}", v)
    return template


async def role(uid):
    if uid == OWNER_ID:
        return "owner"
    return await pool.fetchval("SELECT role FROM admins WHERE user_id=$1", uid)


async def is_main(uid):
    return await role(uid) in ("owner", "main")


def label_of(u):
    return f"@{u.username}" if u.username else (u.full_name or str(u.id))


# ---------------------------------------------------------------- helpers
def fmt_dt(dt):
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def sender_link(r):
    if r["username"]:
        return "@" + html.escape(r["username"])
    return f'<a href="tg://user?id={r["user_id"]}">{html.escape(r["full_name"] or "без имени")}</a>'


def render_review(r, footer=""):
    t = (
        f"📩 <b>Заявка #{r['id']}</b>\n"
        f"👤 От: {sender_link(r)} (<code>{r['user_id']}</code>)\n"
        f"🕒 Отправлена: {fmt_dt(r['created_at'])}\n"
        f"📂 Тема: {html.escape(r['topic_name'] or '')}\n\n"
        f"{html.escape(r['text'])}"
    )
    if footer:
        t += f"\n\n{footer}"
    return t


def review_kb(rid):
    b = InlineKeyboardBuilder()
    b.button(text="✅ Одобрить", callback_data=f"r:ok:{rid}")
    b.button(text="❌ Отклонить", callback_data=f"r:no:{rid}")
    b.button(text="✏️ Подправить и одобрить", callback_data=f"r:ed:{rid}")
    b.adjust(2, 1)
    return b.as_markup()


async def refresh_review(bot: Bot, r, footer="", kb=False):
    if not r["mod_msg_id"]:
        return
    try:
        await bot.edit_message_text(
            render_review(r, footer), chat_id=r["mod_chat_id"], message_id=r["mod_msg_id"],
            parse_mode="HTML", reply_markup=review_kb(r["id"]) if kb else None)
    except Exception:
        log.exception("refresh_review failed")


async def notify_user(bot: Bot, uid, text, markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=markup)
    except Exception:
        log.warning("cannot notify user %s", uid)


async def publish(bot: Bot, topic_id, text):
    pub = await get_setting("pub_chat")
    t = await pool.fetchrow("SELECT * FROM topics WHERE id=$1", topic_id)
    if not pub:
        raise RuntimeError("не привязан чат для публикации (/bind в нужной теме)")
    if not t or t["thread_id"] is None:
        raise RuntimeError("тема не привязана (/bind в нужной теме)")
    await bot.send_message(int(pub), text, message_thread_id=t["thread_id"] or None)


async def release_pending(bot: Bot, admin_id):
    row = await pool.fetchrow("DELETE FROM pending WHERE admin_id=$1 RETURNING request_id", admin_id)
    if row:
        r = await pool.fetchrow(
            "UPDATE requests SET status='pending', handled_by=NULL, handler_label=NULL "
            "WHERE id=$1 AND status IN ('awaiting_reason','awaiting_edit') RETURNING *",
            row["request_id"])
        if r:
            await refresh_review(bot, r, kb=True)


# ---------------------------------------------------------------- states
class UserFlow(StatesGroup):
    post = State()


class AdminFlow(StatesGroup):
    edit_text = State()
    add_admin = State()
    add_topic = State()


# ---------------------------------------------------------------- commands
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(await get_text("welcome"))
    rows = await pool.fetch("SELECT * FROM topics WHERE thread_id IS NOT NULL ORDER BY sort, id")
    if not rows:
        await m.answer(await get_text("closed"))
        return
    b = InlineKeyboardBuilder()
    for t in rows:
        b.button(text=t["name"], callback_data=f"t:{t['id']}")
    b.adjust(2)
    await m.answer(await get_text("choose_topic"), reply_markup=b.as_markup())


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await release_pending(bot, m.from_user.id)
    await m.answer("Отменено.")


@router.message(Command("setmod"), F.chat.type.in_(GROUPS))
async def cmd_setmod(m: Message):
    if not await is_main(m.from_user.id):
        return
    await set_setting("mod_chat", m.chat.id)
    await m.reply("✅ Эта группа теперь получает заявки на проверку.")


@router.message(Command("bind"), F.chat.type.in_(GROUPS))
async def cmd_bind(m: Message):
    if not await is_main(m.from_user.id):
        return
    thread = m.message_thread_id if m.is_topic_message else 0
    rows = await pool.fetch("SELECT * FROM topics ORDER BY sort, id")
    b = InlineKeyboardBuilder()
    for t in rows:
        mark = "✅ " if t["thread_id"] is not None else ""
        b.button(text=mark + t["name"], callback_data=f"b:{t['id']}:{thread}")
    b.adjust(2)
    await m.reply("К какой теме привязать ЭТУ тему чата?", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("b:"))
async def cb_bind(cb: CallbackQuery):
    if not await is_main(cb.from_user.id):
        return await cb.answer("Только главные админы", show_alert=True)
    _, tid, thread = cb.data.split(":")
    await pool.execute("UPDATE topics SET thread_id=$2 WHERE id=$1", int(tid), int(thread))
    await set_setting("pub_chat", cb.message.chat.id)
    name = await pool.fetchval("SELECT name FROM topics WHERE id=$1", int(tid))
    await cb.message.edit_text(f"✅ Эта тема привязана к «{name}».")
    await cb.answer()


# ---------------------------------------------------------------- user flow
@router.callback_query(F.data.startswith("t:"))
async def cb_topic(cb: CallbackQuery, state: FSMContext):
    t = await pool.fetchrow("SELECT * FROM topics WHERE id=$1", int(cb.data[2:]))
    if not t or t["thread_id"] is None:
        return await cb.answer("Эта тема недоступна", show_alert=True)
    await state.set_state(UserFlow.post)
    await state.update_data(topic_id=t["id"])
    await cb.message.answer(fill(await get_text("ask_post"), topic=t["name"]))
    await cb.answer()


@router.message(UserFlow.post, F.text, F.chat.type == "private")
async def got_post(m: Message, state: FSMContext, bot: Bot):
    if m.text.startswith("/"):
        return
    if len(m.text) > 3500:
        return await m.answer("Слишком длинный пост (максимум 3500 символов).")
    data = await state.get_data()
    t = await pool.fetchrow("SELECT * FROM topics WHERE id=$1", data.get("topic_id", 0))
    mod = await get_setting("mod_chat")
    if not t or not mod:
        await state.clear()
        return await m.answer(await get_text("closed"))
    r = await pool.fetchrow(
        "INSERT INTO requests(user_id, username, full_name, topic_id, topic_name, text) "
        "VALUES($1,$2,$3,$4,$5,$6) RETURNING *",
        m.from_user.id, m.from_user.username, m.from_user.full_name, t["id"], t["name"], m.text)
    try:
        msg = await bot.send_message(
            int(mod), render_review(r), parse_mode="HTML", reply_markup=review_kb(r["id"]))
    except Exception:
        log.exception("cannot send to mod chat")
        await pool.execute("DELETE FROM requests WHERE id=$1", r["id"])
        await state.clear()
        return await m.answer(await get_text("closed"))
    await pool.execute(
        "UPDATE requests SET mod_chat_id=$2, mod_msg_id=$3 WHERE id=$1",
        r["id"], msg.chat.id, msg.message_id)
    await state.clear()
    await m.answer(await get_text("sent_ok"))


@router.message(UserFlow.post)
async def got_post_bad(m: Message):
    await m.answer("Отправьте пост текстом.")


@router.callback_query(F.data.startswith("u:"))
async def cb_user_choice(cb: CallbackQuery, bot: Bot):
    _, act, rid = cb.data.split(":")
    accept = act == "acc"
    r = await pool.fetchrow(
        "UPDATE requests SET status=$2 WHERE id=$1 AND user_id=$3 AND status='awaiting_user' RETURNING *",
        int(rid), "approved_edited" if accept else "declined_by_user", cb.from_user.id)
    if not r:
        return await cb.answer("Это уже неактуально", show_alert=True)
    if accept:
        try:
            await publish(bot, r["topic_id"], r["edited_text"])
        except Exception as e:
            await pool.execute("UPDATE requests SET status='awaiting_user' WHERE id=$1", r["id"])
            return await cb.answer(f"Не удалось опубликовать, сообщите админам: {e}", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(await get_text("edit_accepted" if accept else "edit_declined"))
    who = html.escape(r["handler_label"] or "админ")
    await refresh_review(
        bot, r,
        f"✅ {who} исправил, пользователь согласился — опубликовано" if accept
        else f"🚫 {who} исправил, пользователь отказался")
    await cb.answer()


# ---------------------------------------------------------------- moderation
@router.callback_query(F.data.startswith("r:"))
async def cb_review(cb: CallbackQuery, bot: Bot):
    _, act, rid = cb.data.split(":")
    rid = int(rid)
    uid = cb.from_user.id
    if await role(uid) is None:
        return await cb.answer("Нет доступа", show_alert=True)
    label = label_of(cb.from_user)
    esc = html.escape(label)

    if act == "ok":
        r = await pool.fetchrow(
            "UPDATE requests SET status='approved', handled_by=$2, handler_label=$3 "
            "WHERE id=$1 AND status='pending' RETURNING *", rid, uid, label)
        if not r:
            return await cb.answer("Заявка уже обрабатывается", show_alert=True)
        try:
            await publish(bot, r["topic_id"], r["text"])
        except Exception as e:
            await pool.execute(
                "UPDATE requests SET status='pending', handled_by=NULL, handler_label=NULL WHERE id=$1", rid)
            return await cb.answer(f"Не удалось опубликовать: {e}", show_alert=True)
        await refresh_review(bot, r, f"✅ Одобрено: {esc}")
        await notify_user(bot, r["user_id"], await get_text("approved"))
        return await cb.answer("Опубликовано")

    await release_pending(bot, uid)
    new_status = "awaiting_reason" if act == "no" else "awaiting_edit"
    r = await pool.fetchrow(
        "UPDATE requests SET status=$2, handled_by=$3, handler_label=$4 "
        "WHERE id=$1 AND status='pending' RETURNING *", rid, new_status, uid, label)
    if not r:
        return await cb.answer("Заявка уже обрабатывается", show_alert=True)
    await pool.execute(
        "INSERT INTO pending(admin_id, request_id, action) VALUES($1,$2,$3) "
        "ON CONFLICT(admin_id) DO UPDATE SET request_id=EXCLUDED.request_id, action=EXCLUDED.action",
        uid, rid, "reason" if act == "no" else "edit")
    if act == "no":
        await refresh_review(bot, r, f"⏳ {esc} пишет причину отклонения…")
        await cb.message.answer(
            f"{label}, напишите причину отклонения заявки #{rid} следующим сообщением. Отмена — /cancel")
    else:
        await refresh_review(bot, r, f"⏳ {esc} исправляет пост…")
        await cb.message.answer(
            f"{label}, отправьте исправленный текст заявки #{rid} следующим сообщением. Отмена — /cancel")
    await cb.answer()


@router.message(F.text, F.chat.type.in_(GROUPS))
async def mod_text(m: Message, bot: Bot):
    mod = await get_setting("mod_chat")
    if not mod or m.chat.id != int(mod) or not m.from_user:
        return
    p = await pool.fetchrow("SELECT * FROM pending WHERE admin_id=$1", m.from_user.id)
    if not p or m.text.startswith("/"):
        return
    await pool.execute("DELETE FROM pending WHERE admin_id=$1", m.from_user.id)
    label = html.escape(label_of(m.from_user))
    if p["action"] == "reason":
        r = await pool.fetchrow(
            "UPDATE requests SET status='rejected' WHERE id=$1 AND status='awaiting_reason' RETURNING *",
            p["request_id"])
        if not r:
            return
        await refresh_review(bot, r, f"❌ Отклонено: {label}\nПричина: {html.escape(m.text)}")
        await notify_user(bot, r["user_id"], fill(await get_text("rejected"), reason=m.text))
        await m.reply("Причина отправлена пользователю.")
    else:
        r = await pool.fetchrow(
            "UPDATE requests SET status='awaiting_user', edited_text=$2 "
            "WHERE id=$1 AND status='awaiting_edit' RETURNING *", p["request_id"], m.text)
        if not r:
            return
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Принять", callback_data=f"u:acc:{r['id']}")
        kb.button(text="🚫 Отказаться", callback_data=f"u:dec:{r['id']}")
        kb.adjust(2)
        await notify_user(bot, r["user_id"], fill(await get_text("edited"), text=m.text), kb.as_markup())
        await refresh_review(bot, r, f"✏️ {label} исправил пост, ждём ответ пользователя")
        await m.reply("Исправленный пост отправлен пользователю на согласование.")


# ---------------------------------------------------------------- admin panel
def panel_kb(main: bool):
    b = InlineKeyboardBuilder()
    if main:
        b.button(text="📝 Тексты", callback_data="a:texts")
        b.button(text="📂 Темы", callback_data="a:topics")
        b.button(text="👮 Админы", callback_data="a:admins")
        b.button(text="ℹ️ Статус", callback_data="a:status")
        b.adjust(2)
    return b.as_markup()


@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    r = await role(m.from_user.id)
    if r is None:
        return await m.answer("Нет доступа.")
    if r == "junior":
        return await m.answer("Вы младший админ. Заявки приходят в группу модерации — "
                              "нажимайте кнопки под ними.")
    await m.answer("⚙️ Админ-панель", reply_markup=panel_kb(True))


def back_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ В панель", callback_data="a:home")
    return b.as_markup()


@router.callback_query(F.data.startswith("a:"))
async def cb_panel(cb: CallbackQuery, state: FSMContext):
    if not await is_main(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await state.clear()
    act = cb.data[2:]
    if act == "home":
        await cb.message.edit_text("⚙️ Админ-панель", reply_markup=panel_kb(True))
    elif act == "texts":
        b = InlineKeyboardBuilder()
        for k, (title, _, _) in TEXTS.items():
            b.button(text=title, callback_data=f"tx:{k}")
        b.button(text="⬅️ В панель", callback_data="a:home")
        b.adjust(1)
        await cb.message.edit_text("Какой текст изменить?", reply_markup=b.as_markup())
    elif act == "topics":
        rows = await pool.fetch("SELECT * FROM topics ORDER BY sort, id")
        lines = [("✅ " if t["thread_id"] is not None else "⚠️ ") + t["name"] for t in rows]
        b = InlineKeyboardBuilder()
        b.button(text="➕ Добавить тему", callback_data="tp:add")
        for t in rows:
            b.button(text="🗑 " + t["name"], callback_data=f"tp:del:{t['id']}")
        b.button(text="⬅️ В панель", callback_data="a:home")
        b.adjust(1)
        await cb.message.edit_text(
            "Темы:\n" + "\n".join(lines) +
            "\n\n✅ — привязана, ⚠️ — не привязана (пользователи её не видят).\n"
            "Чтобы привязать: зайдите в нужную тему чата и отправьте /bind.",
            reply_markup=b.as_markup())
    elif act == "admins":
        rows = await pool.fetch("SELECT * FROM admins ORDER BY role, user_id")
        owner = cb.from_user.id == OWNER_ID
        lines = [f"👑 владелец: {OWNER_ID}"]
        b = InlineKeyboardBuilder()
        for a in rows:
            tag = "главный" if a["role"] == "main" else "младший"
            lines.append(f"• {tag}: {a['label'] or ''} {a['user_id']}")
            if owner or a["role"] == "junior":
                b.button(text=f"🗑 {a['label'] or a['user_id']}", callback_data=f"ad:del:{a['user_id']}")
        if owner:
            b.button(text="➕ Главный админ", callback_data="ad:add:main")
        b.button(text="➕ Младший админ", callback_data="ad:add:junior")
        b.button(text="⬅️ В панель", callback_data="a:home")
        b.adjust(1)
        await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    elif act == "status":
        mod = await get_setting("mod_chat")
        pub = await get_setting("pub_chat")
        unbound = await pool.fetchval("SELECT count(*) FROM topics WHERE thread_id IS NULL")
        await cb.message.edit_text(
            f"Группа модерации: {mod or 'не задана (/setmod в группе)'}\n"
            f"Чат публикации: {pub or 'не задан (/bind в теме)'}\n"
            f"Непривязанных тем: {unbound}", reply_markup=back_kb())
    await cb.answer()


@router.callback_query(F.data.startswith("tx:"))
async def cb_text_pick(cb: CallbackQuery, state: FSMContext):
    if not await is_main(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    key = cb.data[3:]
    if key not in TEXTS:
        return await cb.answer()
    await state.set_state(AdminFlow.edit_text)
    await state.update_data(key=key)
    title, _, hint = TEXTS[key]
    b = InlineKeyboardBuilder()
    b.button(text="↩️ Сбросить к стандартному", callback_data=f"txr:{key}")
    b.button(text="⬅️ Назад", callback_data="a:texts")
    b.adjust(1)
    await cb.message.edit_text(
        f"{title}\n{('Переменные: ' + hint) if hint else ''}\n\n"
        f"Сейчас:\n{await get_text(key)}\n\nОтправьте новый текст (отмена — /cancel).",
        reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("txr:"))
async def cb_text_reset(cb: CallbackQuery, state: FSMContext):
    if not await is_main(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    await pool.execute("DELETE FROM texts WHERE key=$1", cb.data[4:])
    await state.clear()
    await cb.message.edit_text("Текст сброшен ✅", reply_markup=back_kb())
    await cb.answer()


@router.message(AdminFlow.edit_text, F.text, F.chat.type == "private")
async def save_text(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        return
    key = (await state.get_data())["key"]
    await pool.execute(
        "INSERT INTO texts(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        key, m.text)
    await state.clear()
    await m.answer("Сохранено ✅", reply_markup=back_kb())


@router.callback_query(F.data.startswith("tp:"))
async def cb_topic_admin(cb: CallbackQuery, state: FSMContext):
    if not await is_main(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    parts = cb.data.split(":")
    if parts[1] == "add":
        await state.set_state(AdminFlow.add_topic)
        await cb.message.edit_text("Отправьте название новой темы (отмена — /cancel).")
    else:
        await pool.execute("DELETE FROM topics WHERE id=$1", int(parts[2]))
        await cb.message.edit_text("Тема удалена ✅", reply_markup=back_kb())
    await cb.answer()


@router.message(AdminFlow.add_topic, F.text, F.chat.type == "private")
async def save_topic(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        return
    await pool.execute(
        "INSERT INTO topics(name, sort) VALUES($1, COALESCE((SELECT max(sort)+1 FROM topics),0))",
        m.text.strip()[:60])
    await state.clear()
    await m.answer("Тема добавлена ✅ Теперь зайдите в неё в чате и отправьте /bind.",
                   reply_markup=back_kb())


@router.callback_query(F.data.startswith("ad:"))
async def cb_admins(cb: CallbackQuery, state: FSMContext):
    if not await is_main(cb.from_user.id):
        return await cb.answer("Нет доступа", show_alert=True)
    parts = cb.data.split(":")
    owner = cb.from_user.id == OWNER_ID
    if parts[1] == "add":
        if parts[2] == "main" and not owner:
            return await cb.answer("Главных админов добавляет только владелец", show_alert=True)
        await state.set_state(AdminFlow.add_admin)
        await state.update_data(role=parts[2])
        await cb.message.edit_text(
            "Перешлите сюда любое сообщение этого человека или отправьте его числовой ID "
            "(узнать можно, например, в @userinfobot). Отмена — /cancel.")
    else:
        uid = int(parts[2])
        target = await pool.fetchval("SELECT role FROM admins WHERE user_id=$1", uid)
        if target == "main" and not owner:
            return await cb.answer("Главных админов убирает только владелец", show_alert=True)
        await pool.execute("DELETE FROM admins WHERE user_id=$1", uid)
        await cb.message.edit_text("Админ удалён ✅", reply_markup=back_kb())
    await cb.answer()


@router.message(AdminFlow.add_admin, F.chat.type == "private")
async def save_admin(m: Message, state: FSMContext):
    if m.text and m.text.startswith("/"):
        return
    r = (await state.get_data())["role"]
    if r == "main" and m.from_user.id != OWNER_ID:
        return await m.answer("Главных админов добавляет только владелец.")
    uid, label = None, ""
    fo = m.forward_origin
    if fo is not None and getattr(fo, "type", None) == "user":
        uid = fo.sender_user.id
        label = label_of(fo.sender_user)
    elif m.text and m.text.strip().isdigit():
        uid = int(m.text.strip())
    if uid is None:
        return await m.answer("Не удалось определить пользователя. Пришлите ID цифрами "
                              "или перешлите сообщение (если у человека скрыта пересылка — нужен ID).")
    if uid == OWNER_ID:
        return await m.answer("Это владелец, он и так главный.")
    await pool.execute(
        "INSERT INTO admins(user_id, role, label) VALUES($1,$2,$3) "
        "ON CONFLICT(user_id) DO UPDATE SET role=EXCLUDED.role, label=COALESCE(NULLIF(EXCLUDED.label,''), admins.label)",
        uid, r, label)
    await state.clear()
    await m.answer(f"Готово ✅ {uid} теперь {'главный' if r == 'main' else 'младший'} админ.",
                   reply_markup=back_kb())


# ---------------------------------------------------------------- web + keep-alive
async def health(_):
    return web.Response(text="ok")


async def keepalive():
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        log.info("RENDER_EXTERNAL_URL not set, keep-alive disabled")
        return
    async with ClientSession(timeout=ClientTimeout(total=20)) as s:
        while True:
            await asyncio.sleep(600)  # Render усыпляет через 15 минут тишины
            try:
                async with s.get(url.rstrip("/") + "/health") as resp:
                    log.info("keep-alive %s", resp.status)
            except Exception as e:
                log.warning("keep-alive failed: %s", e)


async def main():
    await init_db()
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    asyncio.create_task(keepalive())

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
