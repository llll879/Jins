# -*- coding: utf-8 -*-
"""
Клиент Jins API v1 — один файл, только стандартная библиотека (Python 3.8+).

    from jins_client import Jins
    jins = Jins("https://your-host", "jns_...")          # ключ: команда /api в боте
    print(jins.check("@username")["verdict"])            # trusted | neutral | risk | scam | unknown

Асинхронный вариант (не блокирует event loop):

    from jins_client import AsyncJins
    jins = AsyncJins("https://your-host", "jns_...")
    data = await jins.check("@username")

Проверка вебхуков:

    from jins_client import verify_webhook
    if not verify_webhook(secret, request.headers["X-Jins-Signature"], raw_body):
        abort(400)
"""
import asyncio
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["Jins", "AsyncJins", "JinsError", "verify_webhook"]


class JinsError(Exception):
    """Ошибка API. Поля: status (HTTP), code (машинный код), message, request_id, details."""

    def __init__(self, status, code, message, request_id=None, details=None):
        super().__init__(f"[{status} {code}] {message}" + (f" (request {request_id})" if request_id else ""))
        self.status, self.code, self.message = status, code, message
        self.request_id, self.details = request_id, details or {}


def verify_webhook(secret, signature_header, body, tolerance=300):
    """Проверка подписи вебхука. body — СЫРЫЕ байты запроса (не пересобранный JSON)."""
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        ts = int(parts["t"])
    except Exception:
        return False
    if tolerance and abs(time.time() - ts) > tolerance:
        return False
    if isinstance(body, str):
        body = body.encode()
    mac = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, parts.get("v1", ""))


class Jins:
    def __init__(self, base_url, api_key, timeout=20, retries=3, user_agent="jins-python/1.0"):
        self.base = base_url.rstrip("/") + ("" if base_url.rstrip("/").endswith("/api/v1") else "/api/v1")
        self.key, self.timeout, self.retries, self.ua = api_key, timeout, retries, user_agent
        self.last_meta = None        # meta последнего ответа (пагинация)
        self.last_rate_limit = None  # {"limit","remaining","reset"} из заголовков

    # ---------------------------------------------------------------- ядро
    def request(self, method, path, params=None, json_body=None, idempotency_key=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: ("true" if v is True else "false" if v is False else v)
                                                 for k, v in params.items() if v is not None})
        data = json.dumps(json_body, ensure_ascii=False).encode() if json_body is not None else None
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json", "User-Agent": self.ua}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        attempt = 0
        while True:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return self._parse(r.status, r.headers, r.read())
            except urllib.error.HTTPError as e:
                status, hdrs, raw = e.code, e.headers, e.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt >= self.retries or method not in ("GET", "DELETE") and not idempotency_key:
                    raise JinsError(0, "network_error", str(e))
                time.sleep(min(2 ** attempt, 10))
                attempt += 1
                continue
            retryable = status == 429 or status >= 500 and (method == "GET" or idempotency_key)
            if retryable and attempt < self.retries:
                time.sleep(min(float(hdrs.get("Retry-After") or 2 ** attempt), 30))
                attempt += 1
                continue
            return self._parse(status, hdrs, raw)

    def _parse(self, status, headers, raw):
        rl = headers.get("X-RateLimit-Limit")
        if rl:
            self.last_rate_limit = {"limit": int(rl), "remaining": int(headers.get("X-RateLimit-Remaining", 0)),
                                    "reset": int(headers.get("X-RateLimit-Reset", 0))}
        ctype = headers.get("Content-Type", "")
        if "json" not in ctype:
            if status >= 400:
                raise JinsError(status, "http_error", raw[:200].decode("utf-8", "replace"), headers.get("X-Request-Id"))
            return raw  # например, PDF-чек
        j = json.loads(raw.decode("utf-8"))
        if not j.get("ok"):
            e = j.get("error", {})
            extra = {k: v for k, v in e.items() if k not in ("code", "message", "status")}
            raise JinsError(status, e.get("code", "error"), e.get("message", ""), headers.get("X-Request-Id"), extra)
        self.last_meta = j.get("meta")
        return j.get("data")

    def get(self, path, **params):
        return self.request("GET", path, params)

    def _post(self, path, body=None, idempotency_key=None):
        return self.request("POST", path, json_body=body or {}, idempotency_key=idempotency_key)

    # ---------------------------------------------------------------- служебные
    def ping(self):
        return self.get("/ping")

    def me(self):
        return self.get("/me")

    def stats(self):
        return self.get("/stats")

    # ---------------------------------------------------------------- репутация и пользователи
    def check(self, ref):
        """Быстрый вердикт по @username или Telegram ID."""
        return self.get(f"/check/{urllib.parse.quote(str(ref), safe='@')}")

    def user(self, ref):
        return self.get(f"/users/{urllib.parse.quote(str(ref), safe='@')}")["user"]

    def reviews(self, ref, limit=20, offset=0):
        return self.get(f"/users/{urllib.parse.quote(str(ref), safe='@')}/reviews", limit=limit, offset=offset)

    def base_search(self, q):
        return self.get("/base/search", q=q)

    def base_list(self, kind="top"):
        return self.get("/base/list", kind=kind)

    def base_profile(self, ref):
        return self.get(f"/base/{urllib.parse.quote(str(ref), safe='@')}")["user"]

    # ---------------------------------------------------------------- объявления и проекты
    def topics(self):
        return self.get("/topics")["topics"]

    def posts(self, topic=None, q=None, project_id=None, author_id=None, limit=20, offset=0):
        return self.get("/posts", topic=topic, q=q, project_id=project_id, author_id=author_id, limit=limit, offset=offset)["posts"]

    def iter_posts(self, page_size=50, **filters):
        """Генератор по всей ленте с автоматической пагинацией."""
        offset = 0
        while True:
            items = self.posts(limit=page_size, offset=offset, **filters)
            yield from items
            if not (self.last_meta or {}).get("has_more") or not items:
                return
            offset += len(items)

    def post(self, post_id):
        return self.get(f"/posts/{int(post_id)}")["post"]

    def submit_post(self, text, topic_id, project_id=None):
        return self._post("/posts", {"text": text, "topic_id": topic_id, "project_id": project_id})

    def projects(self, **filters):
        return self.get("/projects", **filters)["projects"]

    def project(self, project_id):
        return self.get(f"/projects/{int(project_id)}")

    # ---------------------------------------------------------------- сделки
    def deals(self, kind="all", status=None, limit=20, offset=0):
        return self.get("/deals", kind=kind, status=status, limit=limit, offset=offset)["deals"]

    def deal(self, kind, deal_id):
        return self.get(f"/deals/{kind}/{int(deal_id)}")

    def create_escrow_deal(self, seller_id, amount, terms, post_id=None, idempotency_key=None):
        return self._post("/deals/escrow", {"seller_id": seller_id, "amount": amount, "terms": terms, "post_id": post_id},
                         idempotency_key)

    def create_direct_deal(self, seller_id, amount, terms, post_id=None, idempotency_key=None):
        return self._post("/deals/direct", {"seller_id": seller_id, "amount": amount, "terms": terms, "post_id": post_id},
                         idempotency_key)

    def deal_message(self, kind, deal_id, body):
        return self._post(f"/deals/{kind}/{int(deal_id)}/messages", {"body": body})

    def deal_action(self, kind, deal_id, action, reason=None):
        return self._post(f"/deals/{kind}/{int(deal_id)}/actions", {"action": action, "reason": reason})

    def receipts(self):
        return self.get("/receipts")["receipts"]

    def receipt_pdf(self, deal_id):
        return self.get(f"/deals/escrow/{int(deal_id)}/receipt.pdf")

    # ---------------------------------------------------------------- чаты
    def chats(self):
        return self.get("/chats")

    def messages(self, peer, post_id=None):
        return self.get(f"/chats/{int(peer)}/messages", post_id=post_id)

    def send_message(self, peer, text, post_id=None, reply_to=None):
        return self._post(f"/chats/{int(peer)}/messages", {"text": text, "post_id": post_id, "reply_to": reply_to})

    # ---------------------------------------------------------------- вебхуки
    def webhooks(self):
        return self.get("/webhooks")["webhooks"]

    def create_webhook(self, url, events=("*",), description=""):
        """Возвращает вебхук вместе с secret — он показывается один раз."""
        return self._post("/webhooks", {"url": url, "events": list(events), "description": description})["webhook"]

    def test_webhook(self, webhook_id):
        return self._post(f"/webhooks/{int(webhook_id)}/test")

    def delete_webhook(self, webhook_id):
        return self.request("DELETE", f"/webhooks/{int(webhook_id)}")


class AsyncJins:
    """Те же методы, но awaitable: вызовы выполняются в потоке, event loop не блокируется."""

    def __init__(self, *args, **kwargs):
        self._c = Jins(*args, **kwargs)

    def __getattr__(self, name):
        attr = getattr(self._c, name)
        if not callable(attr) or name == "iter_posts":
            return attr

        async def call(*a, **kw):
            return await asyncio.get_running_loop().run_in_executor(None, lambda: attr(*a, **kw))
        return call
