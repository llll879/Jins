# -*- coding: utf-8 -*-
"""
Публичный API Jins — v1.

Подключается к bot.py тремя строками (см. README.txt, раздел v34) и добавляет:
  • API-ключи (Bearer, хранятся только хэшем, права по областям — scopes, лимиты, срок, белый список IP);
  • REST `/api/v1/*` с единым форматом ответов, пагинацией, Idempotency-Key, CORS и заголовками лимитов;
  • вебхуки с HMAC-подписью, повторами и защитой от SSRF;
  • OpenAPI 3 (`/api/v1/openapi.json`) и страницу документации (`/api/v1/docs`, она же `/developers`);
  • управление ключами: команда бота `/api` и эндпоинты мини-аппа `/api/apikeys` (через initData).

Существующая логика (гарант, прямые сделки, чаты, объявления) НЕ дублируется: v1 вызывает прежние обработчики
через тонкий адаптер, поэтому правила, проверки и уведомления остаются теми же, что и в мини-аппе.
Админские методы через API-ключи недоступны намеренно.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html as _html
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout, web
from multidict import MultiDict

try:  # aiogram нужен только для команды /api; без него модуль работает как чистый HTTP API
    from aiogram import Bot, F, Router
    from aiogram.filters import Command
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
    router = Router(name="public_api")

    class ApiFlow(StatesGroup):
        purpose = State()   # ждём от пользователя ответ «зачем нужен ключ»
except Exception:  # pragma: no cover
    router = None

log = logging.getLogger("jins.api")

VERSION = "1.0.0"
PREFIX = "/api/v1"
KEY_RE = re.compile(r"^jns_[A-Za-z0-9_-]{32,64}$")
DEFAULT_RATE = 120          # запросов в минуту на ключ
MAX_KEYS = 5                # активных ключей на пользователя (у владельца — 20)
MAX_HOOKS = 5               # вебхуков на ключ
RETRY_DELAYS = [10, 60, 300, 1800, 7200, 21600]   # секунды между повторами доставки
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1

bm = None  # модуль bot.py (подставляется в init)

SCOPES = OrderedDict([
    ("read", "Публичные данные: объявления, темы, проекты, профили, база репутации и проверка пользователей"),
    ("deals:read", "Чтение своих сделок (гарант и прямые) и чеков"),
    ("deals:write", "Создание сделок, сообщения в них и действия (подтвердить, спор и т. д.) от имени владельца ключа"),
    ("messages:read", "Чтение личных чатов владельца ключа"),
    ("messages:write", "Отправка личных сообщений от имени владельца ключа"),
    ("posts:write", "Подача объявлений на модерацию от имени владельца ключа"),
    ("webhooks", "Управление вебхуками этого ключа"),
])

EVENTS = OrderedDict([
    ("ping", (None, "Тестовое событие (кнопка/запрос «test»)")),
    ("post.published", ("read", "Опубликовано новое объявление")),
    ("deal.created", ("deals:read", "Создана сделка с участием владельца ключа")),
    ("deal.updated", ("deals:read", "Изменился статус или ход сделки")),
    ("deal.message", ("deals:read", "Новое сообщение в чате сделки")),
    ("deal.closed", ("deals:read", "Сделка закрыта (завершена, отменена, возврат) — чек готов")),
    ("message.received", ("messages:read", "Владельцу ключа написали в личные сообщения")),
])

HUMAN = {
    "bad_status": "Действие недоступно в текущем статусе сделки",
    "forbidden": "Нет доступа",
    "subscribe": "Владелец ключа не подписан на обязательные каналы",
    "bad_input": "Некорректные параметры запроса",
    "seller_not_found": "Продавец не найден",
    "escrow_offline": "Гарант сейчас недоступен",
    "deal_not_found": "Сделка не найдена",
    "direct_deal_not_found": "Сделка не найдена",
    "user_not_found": "Пользователь не найден",
    "listing_seller_mismatch": "Объявление не принадлежит указанному продавцу или неактивно",
    "message_not_allowed": "Сообщение нельзя отправить (пустое, слишком длинное или сделка закрыта)",
    "bad_message": "Некорректное сообщение",
    "empty": "Пустой текст",
    "too_long": "Слишком длинный текст",
    "project_not_found": "Проект не найден",
    "bad_image": "Некорректное изображение",
    "receipt_not_found": "Чек не найден",
    "bad_id": "Некорректный идентификатор",
    "blocked": "Один из участников заблокировал другого",
}


# ====================================================================== ошибки и ответы
class E(Exception):
    def __init__(self, status, code, message=None, headers=None, **extra):
        self.status, self.code = status, code
        self.message = message or HUMAN.get(code) or code
        self.headers = headers or {}
        self.extra = extra


class R:
    """Результат обработчика с метаданными (пагинация) или нестандартным статусом."""
    def __init__(self, data, meta=None, status=200):
        self.data, self.meta, self.status = data, meta, status


def _default(x):
    if hasattr(x, "isoformat"):
        return x.isoformat()
    if x.__class__.__name__ == "Decimal":
        return float(x)
    if isinstance(x, (bytes, bytearray, memoryview)):
        return None
    return str(x)


def _dumps(o):
    return json.dumps(o, ensure_ascii=False, default=_default)


def _resp(payload, status=200, headers=None):
    return web.Response(text=_dumps(payload), status=status, content_type="application/json",
                        charset="utf-8", headers=headers)


def _ok(data, meta=None, status=200):
    p = {"ok": True, "data": data}
    if meta is not None:
        p["meta"] = meta
    return _resp(p, status)


def _err(e):
    err = {"code": e.code, "message": e.message, "status": e.status}
    err.update(e.extra)
    return _resp({"ok": False, "error": err}, e.status, e.headers or None)


def P():
    return bm.pool


def _int(v, name, default=None, lo=None, hi=None):
    if v in (None, ""):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise E(400, "invalid_param", f"Параметр «{name}» должен быть целым числом")
    if lo is not None and n < lo or hi is not None and n > hi:
        raise E(400, "invalid_param", f"Параметр «{name}» вне диапазона [{lo}; {hi}]")
    return n


def _page(req, default=20, maxi=100):
    return _int(req.query.get("limit"), "limit", default, 1, maxi), _int(req.query.get("offset"), "offset", 0, 0, 10_000_000)


async def _json(req):
    if not req.can_read_body:
        return {}
    try:
        d = await req.json()
    except Exception:
        raise E(400, "invalid_json", "Тело запроса должно быть корректным JSON-объектом")
    if not isinstance(d, dict):
        raise E(400, "invalid_json", "Тело запроса должно быть JSON-объектом")
    return d


def _base_url(req=None):
    b = (getattr(bm, "WEBAPP_URL", "") or "").rstrip("/")
    if b:
        return b
    if req is not None:
        proto = req.headers.get("X-Forwarded-Proto") or req.scheme
        return f"{proto}://{req.headers.get('X-Forwarded-Host') or req.host}"
    return ""


def _abs(url, req=None):
    return _base_url(req) + url if isinstance(url, str) and url.startswith("/") else url


# ====================================================================== адаптер к прежним обработчикам
class Shim:
    """Минимальный двойник aiohttp-запроса для прежних api_* функций bot.py."""
    def __init__(self, req, query=None, data=None, match=None):
        self.app = req.app
        self.headers = req.headers
        self.method = req.method
        self.query = MultiDict({k: str(v) for k, v in (query or {}).items() if v is not None})
        self.match_info = {k: str(v) for k, v in (match or {}).items()}
        self.content_type = "application/json"
        self.can_read_body = True
        self._d = data or {}
        self._s = {"u": req["u"]}

    def __getitem__(self, k):
        return self._s[k]

    def __setitem__(self, k, v):
        self._s[k] = v

    def get(self, k, d=None):
        return self._s.get(k, d)

    async def json(self):
        return self._d

    async def post(self):
        return MultiDict()


async def legacy(req, name, query=None, data=None, match=None):
    fn = getattr(bm, name)
    try:
        resp = await fn(Shim(req, query, data, match))
    except bm.ApiError as e:
        raise E(e.status, str(e.msg))
    if isinstance(resp, web.Response) and (resp.content_type or "") == "application/json":
        try:
            payload = json.loads(resp.text)
        except Exception:
            payload = None
        if resp.status >= 400:
            code = str((payload or {}).get("error") or "error")
            raise E(resp.status, code)
        return payload
    return resp


# ====================================================================== реестр маршрутов
ROUTES: list = []


def route(method, path, scope=None, tag="Разное", summary="", q=(), body=(), public=False):
    def deco(fn):
        async def wrapper(req):
            out = await fn(req)
            if isinstance(out, web.StreamResponse):
                return out
            if isinstance(out, R):
                return _ok(out.data, out.meta, out.status)
            return _ok(out)
        spec = {"method": method, "path": path, "scope": scope, "tag": tag, "summary": summary, "q": list(q),
                "body": list(body), "public": public, "id": fn.__name__.replace("h_", ""), "handler": wrapper}
        wrapper.spec = spec
        ROUTES.append(spec)
        return fn
    return deco


# ====================================================================== ключи
def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def key_json(k):
    return {"id": k["id"], "name": k["name"], "prefix": k["prefix"], "scopes": list(k["scopes"] or []),
            "rate_limit": k["rate_limit"], "allowed_ips": list(k["allowed_ips"]) if k["allowed_ips"] else [],
            "expires_at": k["expires_at"], "created_at": k["created_at"], "last_used_at": k["last_used_at"],
            "requests": k["requests"], "revoked_at": k["revoked_at"]}


def _norm_scopes(raw):
    if raw in (None, "", []):
        return ["read"]
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    if "all" in raw or "*" in raw:
        return list(SCOPES)
    bad = [s for s in raw if s not in SCOPES]
    if bad:
        raise E(400, "invalid_scope", f"Неизвестные области: {', '.join(map(str, bad))}. Доступные: {', '.join(SCOPES)}")
    return [s for s in SCOPES if s in raw]


def _norm_ips(raw):
    if not raw:
        return None
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    out = []
    for x in raw[:10]:
        try:
            out.append(str(ipaddress.ip_network(str(x).strip(), strict=False)))
        except ValueError:
            raise E(400, "invalid_ip", f"Некорректный IP или подсеть: {x}")
    return out or None


async def create_key(uid, name, scopes=None, rate_limit=None, expires_days=None, allowed_ips=None):
    is_owner = uid == bm.OWNER_ID
    name = str(name or "").strip()[:40]
    if not name:
        raise E(400, "invalid_name", "Укажите название ключа (до 40 символов)")
    scopes = _norm_scopes(scopes)
    limit = _int(rate_limit, "rate_limit", DEFAULT_RATE, 10, 6000 if is_owner else 600)
    days = _int(expires_days, "expires_days", None, 1, 3650)
    ips = _norm_ips(allowed_ips)
    active = await P().fetchval("SELECT count(*) FROM api_keys WHERE user_id=$1 AND revoked_at IS NULL", uid)
    if active >= (20 if is_owner else MAX_KEYS):
        raise E(409, "too_many_keys", f"Достигнут лимит активных ключей ({20 if is_owner else MAX_KEYS}). Отзовите ненужные.")
    token = "jns_" + secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + timedelta(days=days) if days else None
    row = await P().fetchrow(
        """INSERT INTO api_keys(user_id,name,prefix,key_hash,scopes,rate_limit,allowed_ips,expires_at)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""",
        uid, name, token[:12], _hash(token), scopes, limit, ips, exp)
    return row, token


async def list_keys(uid):
    rows = await P().fetch("SELECT * FROM api_keys WHERE user_id=$1 ORDER BY revoked_at IS NOT NULL, id DESC LIMIT 50", uid)
    return [key_json(r) for r in rows]


async def revoke_key(uid, key_id, force=False):
    row = await P().fetchrow(
        "UPDATE api_keys SET revoked_at=now() WHERE id=$1 AND revoked_at IS NULL AND ($3 OR user_id=$2) RETURNING id",
        key_id, uid, force)
    if not row:
        raise E(404, "key_not_found", "Ключ не найден или уже отозван")
    await P().execute("UPDATE api_webhooks SET active=FALSE WHERE key_id=$1", key_id)
    _kcache.clear()
    return True


_kcache: dict = {}
_hits: dict = {}
_usage: dict = {}
_idem: "OrderedDict[tuple, tuple]" = OrderedDict()


def _client_ip(req):
    if os.getenv("API_TRUST_PROXY", "1") != "0":
        xf = req.headers.get("X-Forwarded-For")
        if xf:
            return xf.split(",")[0].strip()
    return req.remote or ""


def _ip_allowed(ip, nets):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in ipaddress.ip_network(n, strict=False) for n in nets)


def _rate(key):
    now = time.monotonic()
    dq = _hits.setdefault(key["id"], deque())
    lim = key["rate_limit"]
    while dq and now - dq[0] >= 60:
        dq.popleft()
    if len(dq) >= lim:
        retry = int(60 - (now - dq[0])) + 1
        raise E(429, "rate_limited", f"Превышен лимит: {lim} запросов в минуту. Повторите через {retry} с.",
                headers={"Retry-After": str(retry), "X-RateLimit-Limit": str(lim), "X-RateLimit-Remaining": "0",
                         "X-RateLimit-Reset": str(retry)})
    dq.append(now)
    return lim, lim - len(dq), int(60 - (now - dq[0])) + 1


async def _touch(key, ip):
    u = _usage.setdefault(key["id"], [0, ip, 0.0])
    u[0] += 1
    u[1] = ip
    if time.monotonic() - u[2] > 30:
        n, u[0], u[2] = u[0], 0, time.monotonic()
        try:
            await P().execute("UPDATE api_keys SET last_used_at=now(), last_ip=$2, requests=requests+$3 WHERE id=$1",
                              key["id"], ip, n)
        except Exception:
            log.exception("api key touch failed")


def _token(req):
    h = req.headers.get("Authorization", "")
    if h[:7].lower() == "bearer ":
        return h[7:].strip()
    return (req.headers.get("X-API-Key") or "").strip()


async def _authenticate(req, spec):
    tok = _token(req)
    if not tok:
        raise E(401, "missing_api_key", "Передайте ключ в заголовке Authorization: Bearer jns_…  (или X-API-Key)")
    if not KEY_RE.match(tok):
        raise E(401, "invalid_api_key", "Неверный формат ключа")
    h = _hash(tok)
    hit = _kcache.get(h)
    if hit and time.monotonic() - hit[0] < 15:
        key = hit[1]
    else:
        key = await P().fetchrow("SELECT * FROM api_keys WHERE key_hash=$1", h)
        if len(_kcache) > 2000:
            _kcache.clear()
        _kcache[h] = (time.monotonic(), key)
    if not key:
        raise E(401, "invalid_api_key", "Ключ не найден")
    if key["revoked_at"]:
        raise E(401, "key_revoked", "Ключ отозван")
    if key["expires_at"] and key["expires_at"] <= datetime.now(timezone.utc):
        raise E(401, "key_expired", "Срок действия ключа истёк")
    ip = _client_ip(req)
    if key["allowed_ips"] and not _ip_allowed(ip, key["allowed_ips"]):
        raise E(403, "ip_not_allowed", f"Запросы с адреса {ip} для этого ключа запрещены")
    req["key"] = key
    try:
        req["rl"] = _rate(key)
    except E as e:
        req["rl_err"] = e.headers
        raise
    if spec.get("scope") and spec["scope"] not in (key["scopes"] or []):
        raise E(403, "insufficient_scope", f"Для этого метода нужна область «{spec['scope']}»", required_scope=spec["scope"])
    u = await P().fetchrow("SELECT user_id,username,first_name,last_name,photo_url FROM users WHERE user_id=$1", key["user_id"])
    req["u"] = ({"id": u["user_id"], "username": u["username"], "first_name": u["first_name"],
                 "last_name": u["last_name"], "photo_url": u["photo_url"]} if u
                else {"id": key["user_id"], "username": None, "first_name": None, "last_name": None, "photo_url": None})
    await _touch(key, ip)


def _finish(req, resp, rid):
    h = resp.headers
    h["X-Request-Id"] = rid
    h["Access-Control-Allow-Origin"] = os.getenv("API_CORS_ORIGINS", "*")
    h["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-API-Key, Idempotency-Key"
    h["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    h["Access-Control-Expose-Headers"] = "X-Request-Id, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset, Retry-After, Idempotent-Replay"
    h["Access-Control-Max-Age"] = "86400"
    h["Cache-Control"] = h.get("Cache-Control", "no-store")
    rl = req.get("rl")
    if rl:
        h["X-RateLimit-Limit"], h["X-RateLimit-Remaining"], h["X-RateLimit-Reset"] = str(rl[0]), str(rl[1]), str(rl[2])
    return resp


@web.middleware
async def mw(request, handler):
    p = request.path
    if p != PREFIX and not p.startswith(PREFIX + "/"):
        return await handler(request)
    rid = "req_" + uuid.uuid4().hex[:16]
    request["rid"] = rid
    if request.method == "OPTIONS":
        return _finish(request, web.Response(status=204), rid)
    spec = getattr(handler, "spec", None) or getattr(getattr(request.match_info, "handler", None), "spec", None)
    try:
        if spec is not None and not spec["public"]:
            await _authenticate(request, spec)
        idem = None
        if request.method == "POST" and request.get("key") and request.headers.get("Idempotency-Key"):
            idem = (request["key"]["id"], request.headers["Idempotency-Key"][:100], request.path)
            hit = _idem.get(idem)
            if hit and time.time() - hit[0] < 86400:
                resp = web.Response(text=hit[2], status=hit[1], content_type="application/json", charset="utf-8")
                resp.headers["Idempotent-Replay"] = "true"
                return _finish(request, resp, rid)
        resp = await handler(request)
        if idem and resp.status < 500 and isinstance(resp, web.Response) and resp.text is not None:
            _idem[idem] = (time.time(), resp.status, resp.text)
            while len(_idem) > 5000:
                _idem.popitem(last=False)
    except E as e:
        resp = _err(e)
    except bm.ApiError as e:
        resp = _err(E(e.status, str(e.msg)))
    except web.HTTPException as e:
        code = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}.get(e.status, "http_error")
        msg = {404: "Эндпоинт не найден", 405: "Метод не поддерживается этим адресом"}.get(e.status, e.reason)
        resp = _err(E(e.status, code, msg))
    except Exception:
        log.exception("api v1 error %s %s", request.method, p)
        resp = _err(E(500, "server", "Внутренняя ошибка сервера"))
    return _finish(request, resp, rid)


# ====================================================================== служебные
@route("GET", "", public=True, tag="Служебные", summary="Индекс API: версия и ссылки")
async def h_index(req):
    b = _base_url(req)
    return {"name": "Jins API", "version": VERSION, "docs": b + PREFIX + "/docs", "openapi": b + PREFIX + "/openapi.json",
            "endpoints": len(ROUTES)}


@route("GET", "/openapi.json", public=True, tag="Служебные", summary="Спецификация OpenAPI 3")
async def h_openapi(req):
    return web.Response(text=_dumps(build_openapi(_base_url(req))), content_type="application/json", charset="utf-8")


@route("GET", "/docs", public=True, tag="Служебные", summary="Страница документации")
async def h_docs(req):
    return web.Response(text=build_docs(_base_url(req)), content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "public, max-age=300"})


@route("GET", "/ping", tag="Служебные", summary="Проверка ключа и связи (любая область)")
async def h_ping(req):
    return {"pong": True, "time": datetime.now(timezone.utc), "version": VERSION}


@route("GET", "/me", tag="Служебные", summary="Информация о ключе: владелец, области, лимиты")
async def h_me(req):
    k, u = req["key"], req["u"]
    rl = req.get("rl") or (k["rate_limit"], k["rate_limit"], 60)
    return {"key": key_json(k),
            "user": {"id": u["id"], "username": u["username"],
                     "name": " ".join(x for x in [u["first_name"], u["last_name"]] if x) or u["username"]},
            "rate_limit": {"limit": rl[0], "remaining": rl[1], "reset_in": rl[2]},
            "scopes": {s: SCOPES[s] for s in (k["scopes"] or [])}}


@route("GET", "/scopes", public=True, tag="Служебные", summary="Список областей доступа (scopes)")
async def h_scopes(req):
    return {"scopes": [{"name": k, "description": v} for k, v in SCOPES.items()]}


@route("GET", "/stats", scope="read", tag="Служебные", summary="Публичная статистика сервиса")
async def h_stats(req):
    p = P()
    r = await p.fetchrow(
        """SELECT (SELECT count(*) FROM users)::int users,
                  (SELECT count(*) FROM posts WHERE active AND expires_at>now())::int posts,
                  (SELECT count(*) FROM projects)::int projects,
                  ((SELECT count(*) FROM escrow_deals WHERE status='completed')
                   +(SELECT count(*) FROM direct_deals WHERE status='completed'))::int deals_completed""")
    return dict(r)


# ====================================================================== объявления и темы
@route("GET", "/topics", scope="read", tag="Объявления", summary="Темы (разделы) объявлений с числом активных")
async def h_topics(req):
    rows = await bm.topics_with_counts()
    return {"topics": [{"id": t["id"], "name": t["name"], "count": t["c"]} for t in rows]}


@route("GET", "/posts", scope="read", tag="Объявления", summary="Лента активных объявлений",
       q=[("topic", "integer", "ID темы"), ("q", "string", "Поиск по тексту"), ("project_id", "integer", "Только объявления проекта"),
          ("author_id", "integer", "Только объявления автора (Telegram ID)"),
          ("limit", "integer", "1–100, по умолчанию 20"), ("offset", "integer", "Смещение")])
async def h_posts(req):
    qy = req.query
    limit, offset = _page(req)
    where, args = ["active", "expires_at>now()"], []

    def add(cond, v):
        args.append(v)
        where.append(cond.replace("?", f"${len(args)}"))
    if qy.get("topic"):
        add("topic_id=?", _int(qy["topic"], "topic"))
    if qy.get("project_id"):
        add("project_id=?", _int(qy["project_id"], "project_id"))
    if qy.get("author_id"):
        add("user_id=?", _int(qy["author_id"], "author_id"))
    if qy.get("q", "").strip():
        s = qy["q"].strip()[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        add("text ILIKE ?", f"%{s}%")
    args += [limit + 1, offset]
    rows = await P().fetch(
        f"SELECT * FROM posts WHERE {' AND '.join(where)} ORDER BY bumped_at DESC NULLS LAST, id DESC "
        f"LIMIT ${len(args) - 1} OFFSET ${len(args)}", *args)
    items = await _posts(rows[:limit], req)
    return R({"posts": items}, {"limit": limit, "offset": offset, "has_more": len(rows) > limit})


async def _posts(rows, req):
    items = await bm.posts_json(rows, req["u"]["id"])
    for it in items:
        for k in ("photo", "author_photo", "contact", "author_url"):
            if k in it:
                it[k] = _abs(it[k], req)
    return items


@route("GET", "/posts/{id}", scope="read", tag="Объявления", summary="Одно объявление")
async def h_post(req):
    pid = _int(req.match_info["id"], "id")
    row = await P().fetchrow("SELECT * FROM posts WHERE id=$1 AND active AND expires_at>now()", pid)
    if not row:
        raise E(404, "post_not_found", "Объявление не найдено или снято с публикации")
    return {"post": (await _posts([row], req))[0]}


@route("POST", "/posts", scope="posts:write", tag="Объявления", summary="Подать объявление на модерацию",
       body=[("text", "string", "Текст объявления", True), ("topic_id", "integer", "ID темы", True),
             ("project_id", "integer", "ID вашего проекта (публиковать от его имени)", False)])
async def h_post_create(req):
    d = await _json(req)
    if not d.get("text") or not d.get("topic_id"):
        raise E(400, "invalid_param", "Нужны поля text и topic_id")
    return await legacy(req, "api_submit", data={"text": d["text"], "topic_id": d["topic_id"], "project_id": d.get("project_id")})


# ====================================================================== пользователи и база репутации
async def _resolve_user(ref):
    ref = str(ref).strip()
    if re.fullmatch(r"\d{1,15}", ref):
        uid = int(ref)
        return uid if await P().fetchval("SELECT 1 FROM users WHERE user_id=$1", uid) else None
    if len(ref) > 64:
        return None
    return (await bm.base_find(ref))[0]


def _card_out(c):
    c = dict(c)
    c.pop("via", None)
    c["url"] = f"https://t.me/{c['username']}" if c.get("username") else None
    return c


@route("GET", "/users/{ref}", scope="read", tag="Пользователи и база", summary="Профиль пользователя из базы (по Telegram ID или @username)")
async def h_user(req):
    uid = await _resolve_user(req.match_info["ref"])
    c = (await bm.base_cards([uid])).get(uid) if uid else None
    if not c:
        raise E(404, "user_not_found")
    return {"user": _card_out(c)}


@route("GET", "/users/{ref}/reviews", scope="read", tag="Пользователи и база", summary="Отзывы о пользователе",
       q=[("limit", "integer", "1–100"), ("offset", "integer", "Смещение")])
async def h_user_reviews(req):
    uid = await _resolve_user(req.match_info["ref"])
    if not uid:
        raise E(404, "user_not_found")
    limit, offset = _page(req)
    agg = await P().fetchrow("SELECT COALESCE(avg(rating),0)::float avg, count(*)::int n FROM user_reviews WHERE target_id=$1", uid)
    rows = await P().fetch(
        """SELECT r.id,r.rating,r.text,r.created_at,r.reviewer_id,u.username,u.first_name,u.last_name
           FROM user_reviews r LEFT JOIN users u ON u.user_id=r.reviewer_id WHERE r.target_id=$1
           ORDER BY r.created_at DESC, r.id DESC LIMIT $2 OFFSET $3""", uid, limit + 1, offset)
    items = [{"id": r["id"], "rating": r["rating"], "text": r["text"], "created_at": r["created_at"],
              "author": {"id": r["reviewer_id"], "username": r["username"],
                         "name": " ".join(x for x in [r["first_name"], r["last_name"]] if x) or r["username"]}} for r in rows[:limit]]
    return R({"average": round(agg["avg"], 2), "count": agg["n"], "reviews": items},
             {"limit": limit, "offset": offset, "has_more": len(rows) > limit})


@route("GET", "/check/{ref}", scope="read", tag="Пользователи и база",
       summary="Быстрая проверка: вердикт по пользователю (trusted / neutral / risk / scam / unknown)")
async def h_check(req):
    uid = await _resolve_user(req.match_info["ref"])
    c = (await bm.base_cards([uid])).get(uid) if uid else None
    if not c:
        return {"found": False, "verdict": "unknown", "id": None}
    verdict = "scam" if c["minus"] == "scam" else "risk" if c["minus"] else "trusted" if c["stars"] >= 4 else "neutral"
    return {"found": True, "id": c["id"], "username": c["username"], "verdict": verdict, "stars": c["stars"],
            "score": c["score"], "status": c["status"], "label": c["label"], "tier": c["tier"]}


@route("GET", "/base/search", scope="read", tag="Пользователи и база", summary="Поиск по базе (ник, имя, ID, прежний ник)",
       q=[("q", "string", "Запрос, от 2 символов")])
async def h_base_search(req):
    q = str(req.query.get("q") or "").strip()[:100]
    if len(q) < 2:
        return {"results": [], "exact": None}
    uid, via = await bm.base_find(q)
    ids = [uid] if uid else []
    for x in await bm.base_search_ids(q, 8):
        if x not in ids:
            ids.append(x)
    cards = await bm.base_cards(ids[:8])
    res = []
    for i in ids[:8]:
        if i in cards:
            c = _card_out(cards[i])
            if i == uid:
                c["via"] = via
            res.append(c)
    return {"results": res, "exact": uid}


@route("GET", "/base/list", scope="read", tag="Пользователи и база", summary="Списки базы: надёжные (top) или с жалобами/мошенники (minus)",
       q=[("kind", "string", "top | minus")])
async def h_base_list(req):
    kind = "minus" if req.query.get("kind") == "minus" else "top"
    d = await legacy(req, "api_base_list", query={"kind": kind})
    d["results"] = [_card_out(c) for c in d.get("results", [])]
    return d


@route("GET", "/base/{ref}", scope="read", tag="Пользователи и база", summary="Карточка пользователя в базе с жалобами")
async def h_base_profile(req):
    uid = await _resolve_user(req.match_info["ref"])
    c = (await bm.base_cards([uid])).get(uid) if uid else None
    if not c:
        raise E(404, "user_not_found")
    c = _card_out(c)
    try:
        c["complaints"] = await bm.base_complaints(uid, 15, False)
    except Exception:
        log.exception("base_complaints failed")
        c["complaints"] = []
    return {"user": c}


# ====================================================================== проекты
@route("GET", "/projects", scope="read", tag="Проекты", summary="Список проектов",
       q=[("owner_id", "integer", "Telegram ID владельца"), ("kind", "string", "bot | channel | group | site | service | other"),
          ("marked", "boolean", "true — только с галочкой"), ("limit", "integer", "1–100"), ("offset", "integer", "Смещение")])
async def h_projects(req):
    limit, offset = _page(req)
    owner = _int(req.query.get("owner_id"), "owner_id")
    kind = req.query.get("kind") or None
    marked = str(req.query.get("marked", "")).lower() in ("1", "true", "yes")
    rows = await P().fetch(
        bm.PROJECT_SELECT + " WHERE ($1::bigint IS NULL OR p.owner_id=$1) AND ($2::text IS NULL OR p.kind=$2) "
        "AND ($3::boolean = FALSE OR p.mark<>'') ORDER BY p.id DESC LIMIT $4 OFFSET $5", owner, kind, marked, limit + 1, offset)
    items = []
    for r in rows[:limit]:
        j = bm.project_json(r)
        j.pop("mark_req", None)
        items.append(j)
    return R({"projects": items}, {"limit": limit, "offset": offset, "has_more": len(rows) > limit})


@route("GET", "/projects/{id}", scope="read", tag="Проекты", summary="Проект и его активные объявления")
async def h_project(req):
    pid = _int(req.match_info["id"], "id")
    r = await P().fetchrow(bm.PROJECT_SELECT + " WHERE p.id=$1", pid)
    if not r:
        raise E(404, "project_not_found")
    rows = await P().fetch("SELECT * FROM posts WHERE project_id=$1 AND active AND expires_at>now() ORDER BY id DESC LIMIT 50", pid)
    j = bm.project_json(r)
    j.pop("mark_req", None)
    return {"project": j, "posts": await _posts(rows, req)}


# ====================================================================== сделки
def _kind(req):
    k = req.match_info["kind"]
    if k not in ("escrow", "direct"):
        raise E(400, "invalid_param", "kind должен быть escrow или direct")
    return k


@route("GET", "/deals", scope="deals:read", tag="Сделки", summary="Мои сделки (гарант и прямые)",
       q=[("kind", "string", "escrow | direct | all (по умолчанию all)"), ("status", "string", "Фильтр по статусу"),
          ("limit", "integer", "1–100"), ("offset", "integer", "Смещение")])
async def h_deals(req):
    uid = req["u"]["id"]
    limit, offset = _page(req)
    kind = req.query.get("kind") or "all"
    if kind not in ("all", "escrow", "direct"):
        raise E(400, "invalid_param", "kind должен быть escrow, direct или all")
    args, st = [uid], ""
    if req.query.get("status"):
        args.append(req.query["status"][:40])
        st = f" AND status=${len(args)}"
    parts = []
    if kind in ("all", "escrow"):
        parts.append("SELECT 'escrow'::text kind,id,buyer_id,seller_id,amount,fee,terms,status,created_at,updated_at "
                     f"FROM escrow_deals WHERE (buyer_id=$1 OR seller_id=$1){st}")
    if kind in ("all", "direct"):
        parts.append("SELECT 'direct'::text kind,id,buyer_id,seller_id,amount,0::bigint fee,terms,status,created_at,updated_at "
                     f"FROM direct_deals WHERE (buyer_id=$1 OR seller_id=$1){st}")
    args += [limit + 1, offset]
    rows = await P().fetch(" UNION ALL ".join(parts) + f" ORDER BY created_at DESC, id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}", *args)
    items = []
    for r in rows[:limit]:
        d = dict(r)
        d["role"] = "buyer" if r["buyer_id"] == uid else "seller"
        items.append(d)
    return R({"deals": items}, {"limit": limit, "offset": offset, "has_more": len(rows) > limit})


@route("GET", "/deals/{kind}/{id}", scope="deals:read", tag="Сделки", summary="Сделка с перепиской (kind: escrow | direct)")
async def h_deal(req):
    kind, did = _kind(req), _int(req.match_info["id"], "id")
    return await legacy(req, "api_escrow_detail" if kind == "escrow" else "api_direct_detail", query={"id": did})


@route("GET", "/deals/escrow/{id}/receipt.pdf", scope="deals:read", tag="Сделки", summary="PDF-чек сделки через гаранта")
async def h_receipt_pdf(req):
    _int(req.match_info["id"], "id")
    try:
        resp = await bm.api_escrow_receipt(Shim(req, match={"id": req.match_info["id"]}))
    except bm.ApiError as e:
        raise E(e.status, str(e.msg))
    return resp


@route("POST", "/deals/escrow", scope="deals:write", tag="Сделки", summary="Создать сделку через гаранта (владелец ключа — покупатель)",
       body=[("seller_id", "integer", "Telegram ID продавца", True), ("amount", "integer", "Сумма в ⭐ (1–1 000 000)", True),
             ("terms", "string", "Условия сделки (до 1500 символов)", True), ("post_id", "integer", "ID объявления продавца", False)])
async def h_deal_create_escrow(req):
    d = await _json(req)
    return R(await legacy(req, "api_escrow_create", data=d), status=201)


@route("POST", "/deals/direct", scope="deals:write", tag="Сделки", summary="Предложить прямую сделку без гаранта (владелец ключа — покупатель)",
       body=[("seller_id", "integer", "Telegram ID продавца", True), ("amount", "integer", "Сумма (0 — без суммы)", True),
             ("terms", "string", "Условия сделки (до 1500 символов)", True), ("post_id", "integer", "ID объявления продавца", False)])
async def h_deal_create_direct(req):
    d = await _json(req)
    return R(await legacy(req, "api_direct_create", data=d), status=201)


@route("POST", "/deals/{kind}/{id}/messages", scope="deals:write", tag="Сделки", summary="Сообщение в чат сделки",
       body=[("body", "string", "Текст, до 2000 символов", True)])
async def h_deal_message(req):
    kind, did = _kind(req), _int(req.match_info["id"], "id")
    d = await _json(req)
    return R(await legacy(req, "api_escrow_message" if kind == "escrow" else "api_direct_message",
                          data={"deal_id": did, "body": d.get("body")}), status=201)


@route("POST", "/deals/{kind}/{id}/actions", scope="deals:write", tag="Сделки",
       summary="Действие в сделке. escrow: buyer_done, seller_confirm, dispute. direct: accept, decline, confirm и др.",
       body=[("action", "string", "Название действия", True), ("reason", "string", "Причина (для dispute)", False)])
async def h_deal_action(req):
    kind, did = _kind(req), _int(req.match_info["id"], "id")
    d = await _json(req)
    action = str(d.get("action") or "")
    if kind == "escrow":
        fn = {"buyer_done": "api_escrow_buyer_done", "seller_confirm": "api_escrow_seller_confirm", "dispute": "api_escrow_dispute"}.get(action)
        if not fn:
            raise E(400, "invalid_action", "Для escrow доступны: buyer_done, seller_confirm, dispute")
        return await legacy(req, fn, data={"deal_id": did, "reason": d.get("reason")})
    return await legacy(req, "api_direct_action", data={"deal_id": did, "action": action, "reason": d.get("reason")})


@route("GET", "/receipts", scope="deals:read", tag="Сделки", summary="Чеки закрытых сделок")
async def h_receipts(req):
    return await legacy(req, "api_receipts")


@route("GET", "/receipts/{kind}/{id}", scope="deals:read", tag="Сделки", summary="Чек сделки")
async def h_receipt(req):
    return await legacy(req, "api_receipt_one", query={"kind": _kind(req), "id": _int(req.match_info["id"], "id")})


# ====================================================================== личные сообщения
@route("GET", "/chats", scope="messages:read", tag="Сообщения", summary="Список диалогов владельца ключа")
async def h_chats(req):
    return await legacy(req, "api_chats")


@route("GET", "/chats/{peer}/messages", scope="messages:read", tag="Сообщения", summary="Сообщения диалога (помечает прочитанными)",
       q=[("post_id", "integer", "Диалог по объявлению")])
async def h_chat_messages(req):
    return await legacy(req, "api_messages", query={"peer": _int(req.match_info["peer"], "peer"), "post_id": req.query.get("post_id")})


@route("POST", "/chats/{peer}/messages", scope="messages:write", tag="Сообщения", summary="Отправить личное сообщение",
       body=[("text", "string", "Текст, до 3000 символов", True), ("post_id", "integer", "Привязать к объявлению", False),
             ("reply_to", "integer", "ID сообщения, на которое отвечаем", False)])
async def h_chat_send(req):
    d = await _json(req)
    data = {"peer": _int(req.match_info["peer"], "peer"), "text": d.get("text"), "post_id": d.get("post_id"), "reply_to": d.get("reply_to")}
    return R(await legacy(req, "api_message_send", data=data), status=201)


# ====================================================================== вебхуки
async def check_url(url):
    """None, если адрес допустим; иначе текст ошибки. Защита от SSRF: внутренние адреса запрещены."""
    if len(url) > 500:
        return "URL длиннее 500 символов"
    try:
        u = urlparse(url)
        port = u.port
    except Exception:
        return "Некорректный URL"
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        return "Нужен адрес вида https://example.com/hook без логина и пароля"
    if u.scheme == "http" and os.getenv("API_WEBHOOK_ALLOW_HTTP") != "1":
        return "Разрешён только https://"
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return "Не удалось определить адрес хоста"
    if os.getenv("API_WEBHOOK_ALLOW_PRIVATE") == "1":
        return None
    for i in infos:
        ip = ipaddress.ip_address(i[4][0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return "Адреса во внутренних сетях запрещены"
    return None


def hook_json(w, secret=None):
    j = {"id": w["id"], "url": w["url"], "events": list(w["events"]), "description": w["description"], "active": w["active"],
         "failures": w["failures"], "last_status": w["last_status"], "last_delivery_at": w["last_at"], "created_at": w["created_at"]}
    if secret:
        j["secret"] = secret
    return j


def _norm_events(raw, key):
    if not raw or not isinstance(raw, list):
        raise E(400, "invalid_param", "events — непустой массив. Доступные: " + ", ".join(EVENTS) + " или *")
    if "*" in raw:
        return ["*"]
    bad = [e for e in raw if e not in EVENTS]
    if bad:
        raise E(400, "invalid_event", f"Неизвестные события: {', '.join(map(str, bad))}")
    for e in raw:
        need = EVENTS[e][0]
        if need and need not in (key["scopes"] or []):
            raise E(403, "insufficient_scope", f"Для события «{e}» у ключа должна быть область «{need}»", required_scope=need)
    return list(dict.fromkeys(raw))


async def _own_hook(req):
    w = await P().fetchrow("SELECT * FROM api_webhooks WHERE id=$1 AND key_id=$2", _int(req.match_info["id"], "id"), req["key"]["id"])
    if not w:
        raise E(404, "webhook_not_found", "Вебхук не найден")
    return w


@route("GET", "/webhooks/events", scope="webhooks", tag="Вебхуки", summary="Каталог событий")
async def h_hook_events(req):
    return {"events": [{"name": k, "required_scope": v[0], "description": v[1]} for k, v in EVENTS.items()]}


@route("GET", "/webhooks", scope="webhooks", tag="Вебхуки", summary="Вебхуки этого ключа")
async def h_hooks(req):
    rows = await P().fetch("SELECT * FROM api_webhooks WHERE key_id=$1 ORDER BY id", req["key"]["id"])
    return {"webhooks": [hook_json(w) for w in rows]}


@route("POST", "/webhooks", scope="webhooks", tag="Вебхуки", summary="Создать вебхук (секрет показывается один раз)",
       body=[("url", "string", "HTTPS-адрес получателя", True), ("events", "array", "Список событий или [\"*\"]", True),
             ("description", "string", "Заметка", False)])
async def h_hook_create(req):
    d = await _json(req)
    url = str(d.get("url") or "").strip()
    key = req["key"]
    events = _norm_events(d.get("events"), key)
    bad = await check_url(url)
    if bad:
        raise E(400, "invalid_url", bad)
    if await P().fetchval("SELECT count(*) FROM api_webhooks WHERE key_id=$1", key["id"]) >= MAX_HOOKS:
        raise E(409, "too_many_webhooks", f"Не больше {MAX_HOOKS} вебхуков на ключ")
    idle = not await P().fetchval("SELECT 1 FROM api_webhooks WHERE active LIMIT 1")
    secret = "whsec_" + secrets.token_urlsafe(24)
    w = await P().fetchrow(
        "INSERT INTO api_webhooks(key_id,user_id,url,secret,events,description) VALUES($1,$2,$3,$4,$5,$6) RETURNING *",
        key["id"], key["user_id"], url, secret, events, str(d.get("description") or "")[:200])
    if idle:
        await _sync_cursors()
    return R({"webhook": hook_json(w, secret)}, status=201)


@route("PATCH", "/webhooks/{id}", scope="webhooks", tag="Вебхуки", summary="Изменить вебхук (url, events, active, description)",
       body=[("url", "string", "Новый адрес", False), ("events", "array", "Новый список событий", False),
             ("active", "boolean", "Включить/выключить", False), ("description", "string", "Заметка", False)])
async def h_hook_update(req):
    w = await _own_hook(req)
    d = await _json(req)
    url = str(d["url"]).strip() if d.get("url") else w["url"]
    if url != w["url"]:
        bad = await check_url(url)
        if bad:
            raise E(400, "invalid_url", bad)
    events = _norm_events(d["events"], req["key"]) if "events" in d else list(w["events"])
    active = bool(d["active"]) if "active" in d else w["active"]
    desc = str(d["description"])[:200] if "description" in d else w["description"]
    idle = active and not w["active"] and not await P().fetchval("SELECT 1 FROM api_webhooks WHERE active LIMIT 1")
    w = await P().fetchrow(
        "UPDATE api_webhooks SET url=$2,events=$3,active=$4,description=$5,failures=CASE WHEN $4 AND NOT active THEN 0 ELSE failures END WHERE id=$1 RETURNING *",
        w["id"], url, events, active, desc)
    if idle:
        await _sync_cursors()
    return {"webhook": hook_json(w)}


@route("DELETE", "/webhooks/{id}", scope="webhooks", tag="Вебхуки", summary="Удалить вебхук")
async def h_hook_delete(req):
    w = await _own_hook(req)
    await P().execute("DELETE FROM api_webhooks WHERE id=$1", w["id"])
    return {"deleted": True}


@route("POST", "/webhooks/{id}/rotate_secret", scope="webhooks", tag="Вебхуки", summary="Выпустить новый секрет подписи")
async def h_hook_rotate(req):
    w = await _own_hook(req)
    secret = "whsec_" + secrets.token_urlsafe(24)
    w = await P().fetchrow("UPDATE api_webhooks SET secret=$2 WHERE id=$1 RETURNING *", w["id"], secret)
    return {"webhook": hook_json(w, secret)}


@route("POST", "/webhooks/{id}/test", scope="webhooks", tag="Вебхуки", summary="Отправить тестовое событие ping")
async def h_hook_test(req):
    w = await _own_hook(req)
    qid = await _enqueue(w["id"], "ping", {"message": "Тестовое событие Jins API", "webhook_id": w["id"]})
    return R({"queued": True, "delivery_id": qid}, status=202)


@route("GET", "/webhooks/{id}/deliveries", scope="webhooks", tag="Вебхуки", summary="Последние доставки и ошибки")
async def h_hook_deliveries(req):
    w = await _own_hook(req)
    rows = await P().fetch(
        "SELECT id,event,attempts,last_status,last_error,created_at,delivered_at,failed_at,next_at "
        "FROM api_webhook_queue WHERE webhook_id=$1 ORDER BY id DESC LIMIT 30", w["id"])
    return {"deliveries": [dict(r) for r in rows]}


async def _enqueue(webhook_id, etype, data):
    payload = {"id": "evt_" + uuid.uuid4().hex[:20], "type": etype, "created_at": datetime.now(timezone.utc).isoformat(),
               "data": json.loads(_dumps(data))}
    return await P().fetchval("INSERT INTO api_webhook_queue(webhook_id,event,payload) VALUES($1,$2,$3) RETURNING id",
                              webhook_id, etype, payload)


def sign(secret, body, ts):
    return hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


# ---- источники событий: читаем журналы БД по курсору (id), ничего не меняя в основном коде
def _iso(x):
    return x.isoformat() if x else None


def _ev_posts(r):
    return [("post.published", None, {"id": r["id"], "topic_id": r["topic_id"], "topic": r["topic_name"], "text": r["text"],
                                      "author_id": r["user_id"], "project_id": r["project_id"], "created_at": _iso(r["created_at"])})]


def _ev_escrow(r):
    t = "deal.created" if r["event"] == "created" else "deal.updated"
    return [(t, [r["buyer_id"], r["seller_id"]],
             {"kind": "escrow", "deal_id": r["deal_id"], "event": r["event"], "status": r["status"], "amount": r["amount"],
              "buyer_id": r["buyer_id"], "seller_id": r["seller_id"], "actor_id": r["actor_id"], "details": r["details"],
              "created_at": _iso(r["created_at"])})]


def _ev_direct_new(r):
    return [("deal.created", [r["buyer_id"], r["seller_id"]],
             {"kind": "direct", "deal_id": r["id"], "event": "created", "status": r["status"], "amount": r["amount"],
              "buyer_id": r["buyer_id"], "seller_id": r["seller_id"], "actor_id": r["buyer_id"], "created_at": _iso(r["created_at"])})]


def _ev_direct_msg(r):
    base = {"kind": "direct", "deal_id": r["deal_id"], "buyer_id": r["buyer_id"], "seller_id": r["seller_id"],
            "created_at": _iso(r["created_at"])}
    if r["system"]:
        return [("deal.updated", [r["buyer_id"], r["seller_id"]],
                 dict(base, event="system", status=r["status"], actor_id=r["sender_id"], text=r["body"]))]
    return [("deal.message", [r["buyer_id"], r["seller_id"]], dict(base, message_id=r["id"], sender_id=r["sender_id"], text=r["body"]))]


def _ev_escrow_msg(r):
    return [("deal.message", [r["buyer_id"], r["seller_id"]],
             {"kind": "escrow", "deal_id": r["deal_id"], "message_id": r["id"], "sender_id": r["sender_id"], "text": r["body"],
              "buyer_id": r["buyer_id"], "seller_id": r["seller_id"], "created_at": _iso(r["created_at"])})]


def _ev_receipt(r):
    return [("deal.closed", [r["buyer_id"], r["seller_id"]],
             {"kind": r["kind"], "deal_id": r["deal_id"], "outcome": r["outcome"], "amount": r["amount"], "fee": r["fee"],
              "buyer_id": r["buyer_id"], "seller_id": r["seller_id"], "created_at": _iso(r["created_at"])})]


def _ev_dm(r):
    return [("message.received", [r["recipient_id"]],
             {"message_id": r["id"], "sender_id": r["sender_id"], "recipient_id": r["recipient_id"], "text": r["body"],
              "post_id": r["post_id"], "deal_id": r["deal_id"], "created_at": _iso(r["created_at"])})]


SOURCES = [
    ("posts", "posts",
     "SELECT id,user_id,topic_id,topic_name,text,created_at,project_id FROM posts WHERE id>$1 AND active ORDER BY id LIMIT 100", _ev_posts),
    ("escrow_events", "escrow_events",
     "SELECT e.id,e.deal_id,e.actor_id,e.event,e.details,e.created_at,d.buyer_id,d.seller_id,d.status,d.amount "
     "FROM escrow_events e JOIN escrow_deals d ON d.id=e.deal_id WHERE e.id>$1 ORDER BY e.id LIMIT 200", _ev_escrow),
    ("direct_new", "direct_deals",
     "SELECT id,buyer_id,seller_id,amount,status,created_at FROM direct_deals WHERE id>$1 ORDER BY id LIMIT 200", _ev_direct_new),
    ("direct_msgs", "direct_deal_messages",
     "SELECT m.id,m.deal_id,m.sender_id,m.body,m.system,m.created_at,d.buyer_id,d.seller_id,d.status FROM direct_deal_messages m "
     "JOIN direct_deals d ON d.id=m.deal_id WHERE m.id>$1 "
     "AND NOT (m.system AND m.id=(SELECT min(x.id) FROM direct_deal_messages x WHERE x.deal_id=m.deal_id)) ORDER BY m.id LIMIT 200",
     _ev_direct_msg),
    ("escrow_msgs", "escrow_messages",
     "SELECT m.id,m.deal_id,m.sender_id,m.body,m.created_at,d.buyer_id,d.seller_id FROM escrow_messages m "
     "JOIN escrow_deals d ON d.id=m.deal_id WHERE m.id>$1 AND NOT m.system ORDER BY m.id LIMIT 200", _ev_escrow_msg),
    ("receipts", "deal_receipts",
     "SELECT id,kind,deal_id,outcome,buyer_id,seller_id,amount,fee,created_at FROM deal_receipts WHERE id>$1 ORDER BY id LIMIT 200", _ev_receipt),
    ("dm", "direct_messages",
     "SELECT id,sender_id,recipient_id,body,post_id,deal_id,created_at FROM direct_messages WHERE id>$1 ORDER BY id LIMIT 200", _ev_dm),
]


async def _sync_cursors():
    """Поставить курсоры на «сейчас», чтобы новые вебхуки не получали историю."""
    for name, table, _, _ in SOURCES:
        m = await P().fetchval(f"SELECT COALESCE(max(id),0) FROM {table}")
        await bm.set_setting(f"api_cur_{name}", m)


async def _dispatch_events():
    hooks = await P().fetch(
        """SELECT w.id,w.user_id,w.events,k.scopes FROM api_webhooks w JOIN api_keys k ON k.id=w.key_id
           WHERE w.active AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>now())""")
    if not hooks:
        return False
    for name, table, sql, build in SOURCES:
        cur = await bm.get_setting(f"api_cur_{name}")
        if cur is None:
            await bm.set_setting(f"api_cur_{name}", await P().fetchval(f"SELECT COALESCE(max(id),0) FROM {table}"))
            continue
        rows = await P().fetch(sql, int(cur))
        if not rows:
            continue
        for r in rows:
            for etype, users, data in build(r):
                need = EVENTS[etype][0]
                for w in hooks:
                    if users is not None and w["user_id"] not in users:
                        continue
                    if "*" not in w["events"] and etype not in w["events"]:
                        continue
                    if need and need not in (w["scopes"] or []):
                        continue
                    await _enqueue(w["id"], etype, data)
        await bm.set_setting(f"api_cur_{name}", rows[-1]["id"])
    return True


async def _events_loop():
    await asyncio.sleep(5)
    while True:
        try:
            busy = await _dispatch_events()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("webhook event dispatch failed")
            busy = False
        await asyncio.sleep(3 if busy else 10)


async def _deliver(session, row):
    body = json.dumps(row["payload"] if not isinstance(row["payload"], str) else json.loads(row["payload"]),
                      ensure_ascii=False, separators=(",", ":"))
    ts = int(time.time())
    headers = {"Content-Type": "application/json", "User-Agent": f"Jins-Webhook/{VERSION}", "X-Jins-Event": row["event"],
               "X-Jins-Delivery": str(row["id"]), "X-Jins-Timestamp": str(ts),
               "X-Jins-Signature": f"t={ts},v1={sign(row['secret'], body, ts)}"}
    status, err = None, None
    try:
        bad = await check_url(row["url"])
        if bad:
            raise RuntimeError(bad)
        async with session.post(row["url"], data=body.encode(), headers=headers, allow_redirects=False) as resp:
            status = resp.status
            await resp.content.read(1024)
        if not 200 <= status < 300:
            err = f"HTTP {status}"
    except asyncio.CancelledError:
        raise
    except Exception as e:
        err = (str(e) or e.__class__.__name__)[:300]
    if err is None:
        await P().execute("UPDATE api_webhook_queue SET delivered_at=now(), attempts=attempts+1, last_status=$2, last_error=NULL WHERE id=$1",
                          row["id"], status)
        await P().execute("UPDATE api_webhooks SET failures=0, last_status=$2, last_at=now() WHERE id=$1", row["webhook_id"], status)
        return
    attempts = row["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        await P().execute("UPDATE api_webhook_queue SET failed_at=now(), attempts=$2, last_status=$3, last_error=$4 WHERE id=$1",
                          row["id"], attempts, status, err)
        await P().execute(
            "UPDATE api_webhooks SET failures=failures+1, last_status=$2, last_at=now(), active=(failures+1<10) WHERE id=$1",
            row["webhook_id"], status)
    else:
        await P().execute(
            "UPDATE api_webhook_queue SET attempts=$2, last_status=$3, last_error=$4, next_at=now()+($5::text||' seconds')::interval WHERE id=$1",
            row["id"], attempts, status, err, RETRY_DELAYS[attempts - 1])
        await P().execute("UPDATE api_webhooks SET last_status=$2, last_at=now() WHERE id=$1", row["webhook_id"], status)


async def _delivery_loop():
    await asyncio.sleep(7)
    last_clean = 0.0
    async with ClientSession(timeout=ClientTimeout(total=10)) as session:
        while True:
            try:
                rows = await P().fetch(
                    """WITH due AS (SELECT id FROM api_webhook_queue WHERE delivered_at IS NULL AND failed_at IS NULL AND next_at<=now()
                                    ORDER BY id LIMIT 20 FOR UPDATE SKIP LOCKED)
                       UPDATE api_webhook_queue q SET next_at=now()+interval '2 minutes' FROM due, api_webhooks w
                       WHERE q.id=due.id AND w.id=q.webhook_id
                       RETURNING q.id,q.webhook_id,q.event,q.payload,q.attempts,w.url,w.secret""")
                if rows:
                    await asyncio.gather(*[_deliver(session, r) for r in rows], return_exceptions=True)
                if time.monotonic() - last_clean > 3600:
                    last_clean = time.monotonic()
                    await P().execute("DELETE FROM api_webhook_queue WHERE created_at < now() - interval '7 days'")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("webhook delivery loop failed")
            await asyncio.sleep(1 if rows else 2)


# ====================================================================== управление ключами из мини-аппа (initData)
async def api_keys_list(request):
    uid = request["u"]["id"]
    return bm.jr({"keys": await list_keys(uid), "scopes": [{"name": k, "description": v} for k, v in SCOPES.items()],
                  "docs": _base_url(request) + PREFIX + "/docs", "base": _base_url(request) + PREFIX,
                  "max_keys": 20 if uid == bm.OWNER_ID else MAX_KEYS})


async def api_keys_create(request):
    d = await bm.body(request)
    if request["u"]["id"] != bm.OWNER_ID:
        # ключи выдаются только по заявке с одобрением админов (бот: /api new Название)
        return bm.jr({"error": "approval_required",
                      "message": "Ключ выдаётся по заявке: отправьте боту /api new Название, опишите, зачем он нужен, "
                                 "и дождитесь решения админов."}, 403)
    try:
        row, token = await create_key(request["u"]["id"], d.get("name"), d.get("scopes"), d.get("rate_limit"),
                                      d.get("expires_days"), d.get("allowed_ips"))
    except E as e:
        return bm.jr({"error": e.code, "message": e.message}, e.status)
    return bm.jr({"key": key_json(row), "token": token})


async def api_keys_revoke(request):
    d = await bm.body(request)
    try:
        await revoke_key(request["u"]["id"], _int(d.get("id"), "id", 0), force=request["u"]["id"] == bm.OWNER_ID)
    except E as e:
        return bm.jr({"error": e.code, "message": e.message}, e.status)
    return bm.jr({"ok": True})


# ====================================================================== команда бота /api
if router is not None:
    @router.message(Command("api"))
    async def cmd_api(m: Message, state: FSMContext):
        if m.chat.type != "private":
            await m.answer("Команда /api работает только в личном чате с ботом.")
            return
        await state.clear()
        esc = bm.esc
        uid = m.from_user.id
        parts = (m.text or "").split(maxsplit=3)
        sub = parts[1].lower() if len(parts) > 1 else ""
        base = _base_url() + PREFIX
        try:
            if sub in ("new", "create", "создать"):
                name = parts[2] if len(parts) > 2 else "Мой ключ"
                if uid == bm.OWNER_ID:   # владельцу одобрение не нужно
                    row, token = await create_key(uid, name, parts[3] if len(parts) > 3 else "read")
                    await m.answer(_key_text(row, token, base))
                    return
                name = name.strip()[:40]
                scopes = _norm_scopes(parts[3] if len(parts) > 3 else "read")
                active = await P().fetchval("SELECT count(*) FROM api_keys WHERE user_id=$1 AND revoked_at IS NULL", uid)
                if active >= MAX_KEYS:
                    raise E(409, "too_many_keys", f"Достигнут лимит активных ключей ({MAX_KEYS}). Отзовите ненужные.")
                if await P().fetchval("SELECT 1 FROM api_key_requests WHERE user_id=$1 AND status='pending'", uid):
                    raise E(409, "request_pending", "Ваша заявка на ключ уже на рассмотрении у админов. Дождитесь решения.")
                await state.set_state(ApiFlow.purpose)
                await state.update_data(api_name=name, api_scopes=scopes)
                await m.answer(
                    f"📝 <b>Заявка на API-ключ «{esc(name)}»</b>\n"
                    f"Права: {esc(', '.join(scopes))}\n\n"
                    "Напишите одним сообщением, <b>зачем вам нужен ключ</b>: что за проект и как вы будете использовать API.\n"
                    "Заявку рассмотрят админы — вы получите ответ здесь.\n\n"
                    "Отмена: /cancel")
            elif sub == "all" and uid == bm.OWNER_ID:
                rows = await P().fetch(
                    "SELECT id,user_id,name,prefix,scopes,requests FROM api_keys WHERE revoked_at IS NULL ORDER BY id DESC LIMIT 50")
                await m.answer("🔑 <b>Все активные ключи</b>\n" + ("\n".join(
                    f"#{r['id']} user <code>{r['user_id']}</code> · {esc(r['name'])} · {esc(', '.join(r['scopes']))} · {r['requests']} запр."
                    for r in rows) or "Ключей нет.") + "\n\nОтозвать: <code>/api revoke ID</code>")
            elif sub in ("revoke", "del", "delete", "отозвать"):
                kid = _int(parts[2] if len(parts) > 2 else None, "id", 0)
                await revoke_key(uid, kid, force=uid == bm.OWNER_ID)
                await m.answer(f"Ключ #{kid} отозван. Его вебхуки отключены.")
            else:
                keys = [k for k in await list_keys(uid) if not k["revoked_at"]]
                lines = [f"#{k['id']} <b>{esc(k['name'])}</b> · <code>{esc(k['prefix'])}…</code> · {esc(', '.join(k['scopes']))}"
                         f"{' · до ' + k['expires_at'].strftime('%d.%m.%Y') if k['expires_at'] else ''}" for k in keys]
                await m.answer(
                    "🔌 <b>API для разработчиков</b>\n"
                    "Подключайте базу репутации, объявления, сделки и чаты к любому своему проекту.\n\n"
                    f"<b>Документация:</b> {esc(base)}/docs\n<b>OpenAPI:</b> {esc(base)}/openapi.json\n\n"
                    "<b>Команды</b>\n"
                    "<code>/api new Название</code> — заявка на ключ только для чтения\n"
                    "<code>/api new Название all</code> — все права\n"
                    "<code>/api new Название read,deals:read</code> — выбранные права\n"
                    "<code>/api revoke 3</code> — отозвать ключ №3\n\n"
                    "ℹ️ Ключ выдаётся после одобрения админами: бот спросит, зачем он нужен, и отправит заявку на проверку.\n\n"
                    f"<b>Права:</b> {esc(', '.join(SCOPES))}\n\n"
                    + ("<b>Ваши ключи</b>\n" + "\n".join(lines) if lines else "У вас пока нет ключей."))
        except E as e:
            await m.answer(f"⚠️ {esc(e.message)}")


def _key_text(row, token, base):
    esc = bm.esc
    return (f"🔑 <b>Ключ создан:</b> {esc(row['name'])}\n\n<code>{esc(token)}</code>\n\n"
            f"Области: {esc(', '.join(row['scopes']))}\nЛимит: {row['rate_limit']} запросов/мин\n\n"
            "⚠️ Ключ показан <b>один раз</b> — сохраните его сейчас и никому не передавайте. "
            "Удалите это сообщение после копирования.\n"
            f"Проверка: <code>curl -H \"Authorization: Bearer КЛЮЧ\" {esc(base)}/me</code>")


if router is not None:
    @router.message(ApiFlow.purpose, F.text, ~F.text.startswith("/"), F.chat.type == "private")
    async def api_purpose(m: Message, state: FSMContext, bot: Bot):
        esc = bm.esc
        purpose = (m.text or "").strip()
        if len(purpose) < 10:
            await m.answer("Опишите подробнее (минимум 10 символов): что за проект и зачем нужен доступ к API. Отмена: /cancel")
            return
        if len(purpose) > 600:
            await m.answer("Слишком длинно — уложитесь в 600 символов. Отмена: /cancel")
            return
        data = await state.get_data()
        name, scopes = data.get("api_name"), data.get("api_scopes")
        await state.clear()
        if not name or not scopes:
            await m.answer("Что-то пошло не так. Начните заново: /api new Название")
            return
        uid = m.from_user.id
        if await P().fetchval("SELECT 1 FROM api_key_requests WHERE user_id=$1 AND status='pending'", uid):
            await m.answer("Ваша заявка на ключ уже на рассмотрении у админов.")
            return
        rid = await P().fetchval(
            "INSERT INTO api_key_requests(user_id,name,scopes,purpose) VALUES($1,$2,$3,$4) RETURNING id",
            uid, name, list(scopes), purpose)
        who = f'<a href="tg://user?id={uid}">{esc(m.from_user.full_name)}</a>' + (f" (@{esc(m.from_user.username)})" if m.from_user.username else "")
        text = (f"🔑 <b>Заявка на API-ключ #{rid}</b>\n\n"
                f"👤 {who}\nID: <code>{uid}</code>\n"
                f"Название: <b>{esc(name)}</b>\nПрава: {esc(', '.join(scopes))}\n\n"
                f"<b>Зачем нужен:</b>\n{esc(purpose)}")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"apk:ok:{rid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"apk:no:{rid}")]])
        sent = False
        mod = await bm.get_setting("mod_chat")
        if mod:
            try:
                await bot.send_message(int(mod), text, reply_markup=kb)
                sent = True
            except Exception as e:
                log.warning("mod chat %s unavailable for api request: %s", mod, e)
        if not sent:   # админ-группа недоступна — шлём владельцу и главным админам в личку
            staff = {bm.OWNER_ID} | {r["user_id"] for r in await P().fetch("SELECT user_id FROM admins WHERE role='main'")}
            for cid in staff:
                try:
                    await bot.send_message(cid, text, reply_markup=kb)
                    sent = True
                except Exception as e:
                    log.warning("cannot DM staff %s about api request: %s", cid, e)
        if not sent:
            await P().execute("DELETE FROM api_key_requests WHERE id=$1", rid)
            await m.answer("⚠️ Не удалось отправить заявку админам. Попробуйте позже.")
            return
        await m.answer(f"✅ Заявка #{rid} отправлена админам. Когда её рассмотрят, я напишу вам здесь.")

    @router.callback_query(F.data.startswith("apk:"))
    async def cb_api_request(cb: CallbackQuery, bot: Bot):
        esc = bm.esc
        try:
            _, act, rid = cb.data.split(":")
            rid = int(rid)
        except ValueError:
            return await cb.answer()
        if await bm.role(cb.from_user.id) is None:
            return await cb.answer("Решать могут только админы", show_alert=True)
        req = await P().fetchrow("SELECT * FROM api_key_requests WHERE id=$1", rid)
        if not req:
            return await cb.answer("Заявка не найдена", show_alert=True)
        if req["user_id"] == cb.from_user.id and cb.from_user.id != bm.OWNER_ID:
            return await cb.answer("Свою заявку одобрить нельзя", show_alert=True)
        req = await P().fetchrow(
            "UPDATE api_key_requests SET status=$2, decided_by=$3, decided_at=now() WHERE id=$1 AND status='pending' RETURNING *",
            rid, "approved" if act == "ok" else "rejected", cb.from_user.id)
        if not req:
            return await cb.answer("Заявка уже обработана", show_alert=True)
        admin = esc(cb.from_user.full_name)
        uid = req["user_id"]
        verdict = None
        if act == "ok":
            try:
                row, token = await create_key(uid, req["name"], list(req["scopes"]))
                await bot.send_message(uid, "✅ <b>Заявка на API-ключ одобрена!</b>\n\n" + _key_text(row, token, _base_url() + PREFIX))
                verdict = f"✅ Одобрено: {admin}"
            except E as e:
                await P().execute("UPDATE api_key_requests SET status='failed' WHERE id=$1", rid)
                verdict = f"⚠️ Одобрено ({admin}), но ключ не создан: {esc(e.message)}"
                try:
                    await bot.send_message(uid, f"Заявка одобрена, но ключ не создан: {esc(e.message)}")
                except Exception:
                    pass
            except Exception:
                # не смогли доставить ключ пользователю — отзываем, чтобы он нигде не висел
                log.exception("api key delivery failed")
                await P().execute("UPDATE api_keys SET revoked_at=now() WHERE user_id=$1 AND name=$2 AND revoked_at IS NULL AND created_at > now() - interval '1 minute'", uid, req["name"])
                await P().execute("UPDATE api_key_requests SET status='failed' WHERE id=$1", rid)
                verdict = f"⚠️ Одобрено ({admin}), но ключ не доставлен (пользователь не открывал бота или заблокировал его). Ключ не выдан."
        else:
            verdict = f"❌ Отклонено: {admin}"
            try:
                await bot.send_message(uid, f"❌ Заявка #{rid} на API-ключ «{esc(req['name'])}» отклонена админами. Ключ не выдан.")
            except Exception:
                pass
        try:
            await cb.message.edit_text((cb.message.html_text or "") + "\n\n" + verdict, reply_markup=None)
        except Exception:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await cb.answer("Готово")


# ====================================================================== OpenAPI и документация
def _sample(t):
    return {"integer": 1, "boolean": True, "array": ["*"], "number": 1.5}.get(t, "…")


def build_openapi(base):
    paths = {}
    for s in ROUTES:
        op = {"tags": [s["tag"]], "summary": s["summary"], "operationId": s["id"], "parameters": [],
              "responses": {"200": {"description": "Успех",
                                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Envelope"}}}},
                            "default": {"description": "Ошибка",
                                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}}}
        for name in re.findall(r"{(\w+)}", s["path"]):
            op["parameters"].append({"name": name, "in": "path", "required": True, "schema": {"type": "string"}})
        for it in s["q"]:
            op["parameters"].append({"name": it[0], "in": "query", "required": False, "description": it[2], "schema": {"type": it[1]}})
        if s["body"]:
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": [b[0] for b in s["body"] if len(b) > 3 and b[3]],
                "properties": {b[0]: {"type": b[1], "description": b[2]} for b in s["body"]}}}}}
        if not s["public"]:
            op["security"] = [{"bearerAuth": []}]
            if s["scope"]:
                op["x-required-scope"] = s["scope"]
                op["description"] = f"Требуется область доступа: `{s['scope']}`."
        paths.setdefault(PREFIX + s["path"], {})[s["method"].lower()] = op
    return {
        "openapi": "3.0.3",
        "info": {"title": "Jins API", "version": VERSION,
                 "description": "Публичный API Jins: база репутации, объявления, проекты, сделки (гарант и прямые), личные сообщения, вебхуки. "
                                "Ответы всегда в формате {ok, data, meta} или {ok:false, error:{code,message,status}}."},
        "servers": [{"url": base}],
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "description": "Ключ вида jns_… (команда /api в боте)"}},
            "schemas": {
                "Envelope": {"type": "object", "properties": {"ok": {"type": "boolean", "example": True}, "data": {"type": "object"},
                                                               "meta": {"type": "object"}}},
                "Error": {"type": "object", "properties": {"ok": {"type": "boolean", "example": False}, "error": {
                    "type": "object", "properties": {"code": {"type": "string"}, "message": {"type": "string"}, "status": {"type": "integer"}}}}}}},
        "paths": paths}


_DOCS_CSS = """
:root{--bg:#fff;--fg:#1c1f26;--mut:#667085;--card:#f6f7f9;--bd:#e3e6eb;--ac:#6a5cff;--code:#eef0f4}
@media(prefers-color-scheme:dark){:root{--bg:#12141a;--fg:#e8eaf0;--mut:#9aa3b2;--card:#1a1d26;--bd:#2a2e3a;--ac:#9a8cff;--code:#222633}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:900px;margin:0 auto;padding:24px 16px 80px}h1{font-size:28px;margin:.2em 0}h2{margin-top:2em;border-bottom:1px solid var(--bd);padding-bottom:6px}
h3{margin:1.6em 0 .4em}code,pre{font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;background:var(--code);border-radius:6px}code{padding:1px 5px}
pre{padding:12px;overflow:auto}.mut{color:var(--mut)}.ep{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;margin:10px 0}
.m{display:inline-block;min-width:58px;text-align:center;font:700 12px ui-monospace,monospace;padding:2px 6px;border-radius:5px;color:#fff;margin-right:8px}
.GET{background:#2e9e5b}.POST{background:#3b7dd8}.PATCH{background:#c98a1b}.DELETE{background:#d44a4a}.sc{float:right;font-size:12px;color:var(--mut)}
table{border-collapse:collapse;width:100%;margin:6px 0}td,th{border-bottom:1px solid var(--bd);padding:4px 8px;text-align:left;font-size:13px;vertical-align:top}
a{color:var(--ac)}nav a{margin-right:12px}
"""


def build_docs(base):
    e = _html.escape
    api = base + PREFIX
    out = [f"<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Jins API v{VERSION}</title><style>{_DOCS_CSS}</style></head><body><main>",
           f"<h1>Jins API <span class=mut>v{VERSION}</span></h1>"
           f"<p class=mut>Подключите базу репутации, объявления, проекты, сделки и чаты к любому своему проекту: сайту, боту, CRM, приложению.</p>"
           f"<nav><a href='{e(api)}/openapi.json'>OpenAPI JSON</a><a href='#start'>Быстрый старт</a><a href='#hooks'>Вебхуки</a><a href='#ref'>Справочник</a></nav>",
           "<h2 id=start>Быстрый старт</h2><ol><li>Откройте бота и отправьте <code>/api new Мой сайт</code>, напишите, зачем нужен ключ, — после одобрения админами бот пришлёт ключ <code>jns_…</code> (показывается один раз).</li>"
           "<li>Передавайте его в заголовке <code>Authorization: Bearer jns_…</code>.</li></ol>",
           f"<pre>curl -H \"Authorization: Bearer $JINS_KEY\" {e(api)}/check/@username</pre>",
           "<p>Ответ всегда в одном формате:</p><pre>{ \"ok\": true, \"data\": { … }, \"meta\": { \"limit\": 20, \"offset\": 0, \"has_more\": true } }\n"
           "{ \"ok\": false, \"error\": { \"code\": \"insufficient_scope\", \"message\": \"…\", \"status\": 403 } }</pre>",
           "<h2>Основы</h2><table><tr><th>Тема<th>Как работает</tr>"
           "<tr><td>Авторизация<td><code>Authorization: Bearer jns_…</code> или <code>X-API-Key</code>. Ключ действует от имени его владельца.</tr>"
           "<tr><td>Области (scopes)<td>" + ", ".join(f"<code>{e(k)}</code>" for k in SCOPES) + ". Нужная область указана у каждого метода.</tr>"
           f"<tr><td>Лимиты<td>По умолчанию {DEFAULT_RATE} запросов/мин на ключ. Заголовки <code>X-RateLimit-*</code>; при превышении — 429 и <code>Retry-After</code>.</tr>"
           "<tr><td>Пагинация<td><code>limit</code> (1–100) и <code>offset</code>; в <code>meta</code> приходит <code>has_more</code>.</tr>"
           "<tr><td>Идемпотентность<td>Для POST передайте <code>Idempotency-Key</code> — повтор в течение 24 ч вернёт тот же ответ (заголовок <code>Idempotent-Replay: true</code>).</tr>"
           "<tr><td>CORS<td>Включён. Но не храните ключ в браузерном коде: вызывайте API с вашего сервера.</tr>"
           "<tr><td>Трассировка<td>Каждый ответ содержит <code>X-Request-Id</code> — называйте его в обращениях в поддержку.</tr></table>",
           "<h2>Коды ошибок</h2><table><tr><th>HTTP<th>code<th>Значение</tr>"
           "<tr><td>400<td>invalid_param, invalid_json, bad_input…<td>Неверные параметры</tr>"
           "<tr><td>401<td>missing_api_key, invalid_api_key, key_revoked, key_expired<td>Проблема с ключом</tr>"
           "<tr><td>403<td>insufficient_scope, ip_not_allowed, forbidden<td>Нет прав</tr>"
           "<tr><td>404<td>user_not_found, post_not_found, …<td>Не найдено</tr>"
           "<tr><td>409<td>bad_status, too_many_keys…<td>Конфликт состояния</tr>"
           "<tr><td>429<td>rate_limited<td>Превышен лимит</tr>"
           "<tr><td>5xx<td>server, escrow_offline<td>Сбой или гарант недоступен — повторите позже</tr></table>",
           "<h2 id=hooks>Вебхуки</h2><p>Создайте вебхук методом <code>POST /webhooks</code> (нужна область <code>webhooks</code>) — Jins будет присылать события POST-запросом с JSON:</p>"
           "<pre>{ \"id\": \"evt_…\", \"type\": \"deal.closed\", \"created_at\": \"2026-11-09T12:00:00+00:00\", \"data\": { … } }</pre>"
           "<table><tr><th>Событие<th>Область<th>Описание</tr>"
           + "".join(f"<tr><td><code>{e(k)}</code><td>{e(v[0] or '—')}<td>{e(v[1])}</tr>" for k, v in EVENTS.items()) + "</table>"
           "<p>Заголовки: <code>X-Jins-Event</code>, <code>X-Jins-Delivery</code>, <code>X-Jins-Timestamp</code>, <code>X-Jins-Signature: t=…,v1=…</code>. "
           "Подпись — HMAC-SHA256 от строки <code>{timestamp}.{тело}</code> секретом вебхука (<code>whsec_…</code>). Отклоняйте запросы старше 5 минут. "
           "Ответьте любым 2xx в течение 10 с; иначе будут повторы через 10 с, 1, 5, 30 мин, 2 и 6 ч, после чего доставка помечается неуспешной. "
           "После 10 неуспешных доставок подряд вебхук отключается (включить: <code>PATCH /webhooks/{id}</code> с <code>active:true</code>). Сначала проверьте приём методом <code>POST /webhooks/{id}/test</code>.</p>"
           "<pre># Python\nimport hmac, hashlib, time\ndef verify(secret, header, body: bytes, tolerance=300):\n"
           "    p = dict(x.split('=', 1) for x in header.split(','))\n"
           "    if abs(time.time() - int(p['t'])) > tolerance: return False\n"
           "    mac = hmac.new(secret.encode(), p['t'].encode() + b'.' + body, hashlib.sha256).hexdigest()\n"
           "    return hmac.compare_digest(mac, p['v1'])</pre>",
           "<h2 id=ref>Справочник методов</h2>"]
    tags = OrderedDict()
    for s in ROUTES:
        tags.setdefault(s["tag"], []).append(s)
    for tag, items in tags.items():
        out.append(f"<h3>{e(tag)}</h3>")
        for s in items:
            full = PREFIX + s["path"]
            sc = f"<span class=sc>область: {e(s['scope'])}</span>" if s["scope"] else ("<span class=sc>без ключа</span>" if s["public"] else "<span class=sc>любой ключ</span>")
            out.append(f"<div class=ep>{sc}<span class='m {s['method']}'>{s['method']}</span><code>{e(full)}</code><div>{e(s['summary'])}</div>")
            rows = [(n, "path", "string", "", "да") for n in re.findall(r"{(\w+)}", s["path"])]
            rows += [(q[0], "query", q[1], q[2], "") for q in s["q"]]
            rows += [(b[0], "body", b[1], b[2], "да" if len(b) > 3 and b[3] else "") for b in s["body"]]
            if rows:
                out.append("<table><tr><th>Параметр<th>Где<th>Тип<th>Описание<th>Обяз.</tr>" +
                           "".join(f"<tr><td><code>{e(r[0])}</code><td>{r[1]}<td>{r[2]}<td>{e(r[3])}<td>{r[4]}</tr>" for r in rows) + "</table>")
            path_ex = re.sub(r"{(\w+)}", lambda m: "123" if m.group(1) != "kind" else "escrow", full)
            qs = "?" + "&".join(f"{q[0]}={_sample(q[1])}" for q in s["q"][:1]) if s["q"] else ""
            cmd = f"curl -X {s['method']} " if s["method"] != "GET" else "curl "
            if not s["public"]:
                cmd += "-H \"Authorization: Bearer $JINS_KEY\" "
            if s["body"]:
                sample = json.dumps({b[0]: _sample(b[1]) for b in s["body"] if len(b) > 3 and b[3]}, ensure_ascii=False)
                cmd += f"-H \"Content-Type: application/json\" -d '{sample}' "
            out.append(f"<pre>{e(cmd + chr(34) + base + path_ex + qs + chr(34))}</pre></div>")
    out.append("<p class=mut>Админские методы в публичный API не входят. Вопросы и сбои: укажите <code>X-Request-Id</code> ответа.</p></main></body></html>")
    return "".join(out)


async def h_developers(request):
    raise web.HTTPFound(PREFIX + "/docs")


# ====================================================================== подключение к приложению
DDL = """
CREATE TABLE IF NOT EXISTS api_keys(
    id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, name TEXT NOT NULL, prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE, scopes TEXT[] NOT NULL, rate_limit INT NOT NULL DEFAULT 120,
    allowed_ips TEXT[], expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ, last_ip TEXT, revoked_at TIMESTAMPTZ, requests BIGINT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS api_keys_user_idx ON api_keys(user_id);
CREATE TABLE IF NOT EXISTS api_key_requests(
    id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, name TEXT NOT NULL, scopes TEXT[] NOT NULL, purpose TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', decided_by BIGINT, decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS api_key_requests_user_idx ON api_key_requests(user_id, status);
CREATE TABLE IF NOT EXISTS api_webhooks(
    id SERIAL PRIMARY KEY, key_id INT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE, user_id BIGINT NOT NULL,
    url TEXT NOT NULL, secret TEXT NOT NULL, events TEXT[] NOT NULL, description TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE, failures INT NOT NULL DEFAULT 0, last_status INT, last_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS api_webhooks_key_idx ON api_webhooks(key_id);
CREATE TABLE IF NOT EXISTS api_webhook_queue(
    id BIGSERIAL PRIMARY KEY, webhook_id INT NOT NULL REFERENCES api_webhooks(id) ON DELETE CASCADE,
    event TEXT NOT NULL, payload JSONB NOT NULL, attempts INT NOT NULL DEFAULT 0, last_status INT, last_error TEXT,
    next_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ, failed_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS api_webhook_queue_due_idx ON api_webhook_queue(next_at) WHERE delivered_at IS NULL AND failed_at IS NULL;
CREATE INDEX IF NOT EXISTS api_webhook_queue_hook_idx ON api_webhook_queue(webhook_id, id DESC);
"""


def install(app, bot_module):
    """Вызывается в build_web(): регистрирует middleware и маршруты."""
    global bm
    bm = bot_module
    app.middlewares.insert(0, mw)
    r = app.router
    for s in ROUTES:
        r.add_route(s["method"], PREFIX + s["path"], s["handler"])
    r.add_route("GET", PREFIX + "/", ROUTES[0]["handler"])
    r.add_get("/developers", h_developers)
    r.add_get("/api/apikeys", api_keys_list)
    r.add_post("/api/apikeys", api_keys_create)
    r.add_post("/api/apikeys/revoke", api_keys_revoke)


async def start(bot_module):
    """Вызывается в main() после init_db(): создаёт таблицы и запускает фоновые циклы вебхуков."""
    global bm
    bm = bot_module
    await bm.pool.execute(DDL)
    asyncio.create_task(_events_loop())
    asyncio.create_task(_delivery_loop())
    log.info("public api v%s ready: %d routes", VERSION, len(ROUTES))
