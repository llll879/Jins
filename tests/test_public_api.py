# -*- coding: utf-8 -*-
"""
Тесты public_api без PostgreSQL и Telegram: БД и bot.py подменены заглушками.
Запуск:  python -m unittest discover -s tests -v
Если aiohttp/multidict не установлены, подставляются минимальные заглушки.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import aiohttp  # noqa: F401
    import multidict  # noqa: F401
except ImportError:  # минимальные заглушки, чтобы импортировать модуль
    am = types.ModuleType("aiohttp")
    web = types.ModuleType("aiohttp.web")

    class StreamResponse:
        pass

    class Response(StreamResponse):
        def __init__(self, text=None, body=None, status=200, content_type="text/plain", charset=None, headers=None):
            self.text, self.body, self.status, self.content_type = text, body, status, content_type
            self.headers = dict(headers or {})

    class HTTPException(Exception):
        status, reason = 500, "error"

    class HTTPNotFound(HTTPException):
        status, reason = 404, "Not Found"

    class HTTPFound(HTTPException):
        status, reason = 302, "Found"

        def __init__(self, loc):
            self.location = loc

    web.StreamResponse, web.Response, web.HTTPException = StreamResponse, Response, HTTPException
    web.HTTPNotFound, web.HTTPFound = HTTPNotFound, HTTPFound
    web.middleware = lambda f: f
    am.web, am.ClientSession, am.ClientTimeout = web, object, lambda **k: None
    sys.modules.update({"aiohttp": am, "aiohttp.web": web})
    md = types.ModuleType("multidict")
    md.MultiDict = dict
    sys.modules["multidict"] = md

import public_api as pa  # noqa: E402
from aiohttp import web  # noqa: E402


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


class ApiError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


class FakePool:
    """Отвечает на запросы по подстроке SQL; запоминает записи."""
    def __init__(self):
        self.keys, self.exec_log, self.queue, self.settings = {}, [], [], {}
        self.hooks, self.rows = [], {}

    async def fetchrow(self, sql, *a):
        if "FROM api_keys WHERE key_hash" in sql:
            return self.keys.get(a[0])
        if "FROM users WHERE user_id" in sql:
            return {"user_id": a[0], "username": "tester", "first_name": "T", "last_name": None, "photo_url": None}
        if "INSERT INTO api_keys" in sql:
            row = {"id": len(self.keys) + 1, "user_id": a[0], "name": a[1], "prefix": a[2], "key_hash": a[3], "scopes": a[4],
                   "rate_limit": a[5], "allowed_ips": a[6], "expires_at": a[7], "created_at": datetime.now(timezone.utc),
                   "last_used_at": None, "requests": 0, "revoked_at": None, "last_ip": None}
            self.keys[a[3]] = row
            return row
        return None

    async def fetch(self, sql, *a):
        if "FROM api_webhooks w JOIN api_keys k" in sql:
            return self.hooks
        for name, rows in self.rows.items():
            if name in sql:
                return [r for r in rows if r["id"] > a[0]]
        return []

    async def fetchval(self, sql, *a):
        if "count(*) FROM api_keys" in sql:
            return sum(1 for k in self.keys.values() if k["user_id"] == a[0] and not k["revoked_at"])
        if "INSERT INTO api_webhook_queue" in sql:
            self.queue.append((a[0], a[1], a[2]))
            return len(self.queue)
        return 0

    async def execute(self, sql, *a):
        self.exec_log.append((sql, a))


def make_bm(pool):
    bm = types.ModuleType("fake_bot")
    bm.pool, bm.ApiError, bm.OWNER_ID, bm.WEBAPP_URL = pool, ApiError, 1, "https://jins.example"
    bm.esc = lambda s: str(s)

    async def get_setting(k):
        return pool.settings.get(k)

    async def set_setting(k, v):
        pool.settings[k] = v
    bm.get_setting, bm.set_setting = get_setting, set_setting

    async def body(request):
        return await request.json()
    bm.body = body
    bm.jr = lambda d, s=200: web.Response(text=json.dumps(d), status=s, content_type="application/json")
    return bm


class Req:
    def __init__(self, path, method="GET", headers=None, query=None, data=None, match=None, remote="203.0.113.7"):
        self.path, self.method, self.headers = path, method, dict(headers or {})
        self.query, self.match_info, self._data, self.remote = query or {}, match or {}, data, remote
        self.can_read_body = data is not None
        self.app, self._s, self.scheme, self.host = {}, {}, "https", "jins.example"

    async def json(self):
        return self._data

    def __getitem__(self, k):
        return self._s[k]

    def __setitem__(self, k, v):
        self._s[k] = v

    def get(self, k, d=None):
        return self._s.get(k, d)


def add_key(pool, scopes=("read",), rate=120, **kw):
    tok = "jns_" + "A" * 43
    row = {"id": len(pool.keys) + 1, "user_id": 42, "name": "t", "prefix": tok[:12], "key_hash": pa._hash(tok),
           "scopes": list(scopes), "rate_limit": rate, "allowed_ips": None, "expires_at": None,
           "created_at": datetime.now(timezone.utc), "last_used_at": None, "requests": 0, "revoked_at": None, "last_ip": None}
    row.update(kw)
    pool.keys[row["hash"] if "hash" in row else row["key_hash"]] = row
    return tok


def spec_handler(scope=None, public=False, calls=None):
    async def h(req):
        if calls is not None:
            calls.append(1)
        return pa._ok({"hello": "world"})
    h.spec = {"scope": scope, "public": public}
    return h


class Base(unittest.TestCase):
    def setUp(self):
        self.pool = FakePool()
        self.bm = make_bm(self.pool)
        pa.bm = self.bm
        for d in (pa._kcache, pa._hits, pa._usage, pa._idem):
            d.clear()

    def call(self, req, handler=None):
        return run(pa.mw(req, handler or spec_handler()))

    @staticmethod
    def js(resp):
        return json.loads(resp.text)

    @staticmethod
    def auth(tok, **extra):
        return dict({"Authorization": f"Bearer {tok}"}, **extra)


class TestAuth(Base):
    def test_missing_key(self):
        r = self.call(Req("/api/v1/ping"))
        self.assertEqual(r.status, 401)
        j = self.js(r)
        self.assertFalse(j["ok"])
        self.assertEqual(j["error"]["code"], "missing_api_key")
        self.assertIn("X-Request-Id", r.headers)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")

    def test_non_api_path_untouched(self):
        called = []

        async def h(req):
            called.append(1)
            return "passthrough"
        self.assertEqual(run(pa.mw(Req("/api/me"), h)), "passthrough")
        self.assertEqual(called, [1])

    def test_bad_format_and_unknown(self):
        self.assertEqual(self.js(self.call(Req("/api/v1/ping", headers=self.auth("abc"))))["error"]["code"], "invalid_api_key")
        self.assertEqual(self.js(self.call(Req("/api/v1/ping", headers=self.auth("jns_" + "B" * 43))))["error"]["code"], "invalid_api_key")

    def test_ok_headers_and_x_api_key(self):
        tok = add_key(self.pool, rate=50)
        r = self.call(Req("/api/v1/ping", headers={"X-API-Key": tok}))
        self.assertEqual(r.status, 200)
        self.assertTrue(self.js(r)["ok"])
        self.assertEqual(r.headers["X-RateLimit-Limit"], "50")
        self.assertEqual(r.headers["X-RateLimit-Remaining"], "49")

    def test_revoked_expired_ip(self):
        tok = add_key(self.pool, revoked_at=datetime.now(timezone.utc))
        self.assertEqual(self.js(self.call(Req("/x", headers=self.auth(tok)) if False else Req("/api/v1/ping", headers=self.auth(tok))))["error"]["code"], "key_revoked")
        self.pool.keys.clear(), pa._kcache.clear()
        tok = add_key(self.pool, expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        self.assertEqual(self.js(self.call(Req("/api/v1/ping", headers=self.auth(tok))))["error"]["code"], "key_expired")
        self.pool.keys.clear(), pa._kcache.clear()
        tok = add_key(self.pool, allowed_ips=["10.0.0.0/8"])
        r = self.call(Req("/api/v1/ping", headers=self.auth(tok)))
        self.assertEqual((r.status, self.js(r)["error"]["code"]), (403, "ip_not_allowed"))
        r = self.call(Req("/api/v1/ping", headers=self.auth(tok, **{"X-Forwarded-For": "10.1.2.3, 1.1.1.1"})))
        self.assertEqual(r.status, 200)

    def test_scope(self):
        tok = add_key(self.pool, scopes=("read",))
        r = self.call(Req("/api/v1/deals", headers=self.auth(tok)), spec_handler(scope="deals:read"))
        j = self.js(r)
        self.assertEqual((r.status, j["error"]["code"], j["error"]["required_scope"]), (403, "insufficient_scope", "deals:read"))
        self.assertEqual(self.call(Req("/api/v1/posts", headers=self.auth(tok)), spec_handler(scope="read")).status, 200)

    def test_rate_limit(self):
        tok = add_key(self.pool, rate=3)
        for _ in range(3):
            self.assertEqual(self.call(Req("/api/v1/ping", headers=self.auth(tok))).status, 200)
        r = self.call(Req("/api/v1/ping", headers=self.auth(tok)))
        self.assertEqual((r.status, self.js(r)["error"]["code"]), (429, "rate_limited"))
        self.assertTrue(int(r.headers["Retry-After"]) >= 1)
        self.assertEqual(r.headers["X-RateLimit-Remaining"], "0")

    def test_public_route_needs_no_key(self):
        self.assertEqual(self.call(Req("/api/v1/docs"), spec_handler(public=True)).status, 200)

    def test_options_and_404(self):
        r = self.call(Req("/api/v1/anything", method="OPTIONS"))
        self.assertEqual(r.status, 204)
        self.assertIn("Authorization", r.headers["Access-Control-Allow-Headers"])

        async def nf(req):
            raise web.HTTPNotFound()
        r = run(pa.mw(Req("/api/v1/nope"), nf))
        self.assertEqual((r.status, self.js(r)["error"]["code"]), (404, "not_found"))

    def test_server_error_hidden(self):
        tok = add_key(self.pool)

        async def boom(req):
            raise RuntimeError("secret internals")
        boom.spec = {"scope": None, "public": False}
        r = run(pa.mw(Req("/api/v1/ping", headers=self.auth(tok)), boom))
        self.assertEqual(r.status, 500)
        self.assertNotIn("secret", r.text)

    def test_idempotency(self):
        tok = add_key(self.pool)
        calls = []
        h = spec_handler(calls=calls)
        h1 = self.call(Req("/api/v1/deals/direct", "POST", self.auth(tok, **{"Idempotency-Key": "abc"}), data={}), h)
        h2 = self.call(Req("/api/v1/deals/direct", "POST", self.auth(tok, **{"Idempotency-Key": "abc"}), data={}), h)
        h3 = self.call(Req("/api/v1/deals/direct", "POST", self.auth(tok, **{"Idempotency-Key": "other"}), data={}), h)
        self.assertEqual(len(calls), 2)
        self.assertEqual(h2.headers.get("Idempotent-Replay"), "true")
        self.assertEqual(h1.text, h2.text)
        self.assertNotIn("Idempotent-Replay", h3.headers)


class TestLegacyAdapter(Base):
    def test_success_and_error_mapping(self):
        async def api_direct_create(request):
            d = await self.bm.body(request)
            if d.get("amount") == -1:
                raise ApiError(400, "bad_input")
            assert request["u"]["id"] == 42 and request.app == {}
            return self.bm.jr({"ok": True, "id": 7, "seller": d["seller_id"]})
        self.bm.api_direct_create = api_direct_create
        req = Req("/api/v1/deals/direct", "POST", data={})
        req["u"] = {"id": 42}
        out = run(pa.legacy(req, "api_direct_create", data={"seller_id": 5, "amount": 10}))
        self.assertEqual(out, {"ok": True, "id": 7, "seller": 5})
        with self.assertRaises(pa.E) as cm:
            run(pa.legacy(req, "api_direct_create", data={"seller_id": 5, "amount": -1}))
        self.assertEqual((cm.exception.status, cm.exception.code), (400, "bad_input"))

    def test_json_error_status_mapping(self):
        async def h(request):
            return self.bm.jr({"error": "bad_status"}, 409)
        self.bm.h = h
        req = Req("/x", "POST", data={})
        req["u"] = {"id": 1}
        with self.assertRaises(pa.E) as cm:
            run(pa.legacy(req, "h"))
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "bad_status"))
        self.assertIn("статус", cm.exception.message)


class TestHelpers(Base):
    def test_scopes_ips_ints(self):
        self.assertEqual(pa._norm_scopes(None), ["read"])
        self.assertEqual(pa._norm_scopes("deals:read, read"), ["read", "deals:read"])
        self.assertEqual(pa._norm_scopes("all"), list(pa.SCOPES))
        with self.assertRaises(pa.E):
            pa._norm_scopes(["admin"])
        self.assertEqual(pa._norm_ips("1.2.3.4, 10.0.0.0/8"), ["1.2.3.4/32", "10.0.0.0/8"])
        with self.assertRaises(pa.E):
            pa._norm_ips("not-an-ip")
        self.assertEqual(pa._int("5", "x", None, 1, 10), 5)
        for bad in ("abc", "0", "11"):
            with self.assertRaises(pa.E):
                pa._int(bad, "x", None, 1, 10)

    def test_create_key(self):
        row, tok = run(pa.create_key(42, "Мой сайт", "read,deals:read", 60, 30, "1.2.3.4"))
        self.assertTrue(pa.KEY_RE.match(tok))
        self.assertEqual(row["key_hash"], hashlib.sha256(tok.encode()).hexdigest())
        self.assertNotIn(tok, json.dumps(pa.key_json(row), default=str))  # открытый ключ не утекает в JSON
        self.assertEqual((row["scopes"], row["rate_limit"], row["allowed_ips"]), (["read", "deals:read"], 60, ["1.2.3.4/32"]))
        for i in range(pa.MAX_KEYS - 1):
            run(pa.create_key(42, f"k{i}"))
        with self.assertRaises(pa.E) as cm:
            run(pa.create_key(42, "лишний"))
        self.assertEqual(cm.exception.code, "too_many_keys")
        with self.assertRaises(pa.E):
            run(pa.create_key(43, "  "))
        with self.assertRaises(pa.E):
            run(pa.create_key(43, "x", rate_limit=100000))

    def test_signature_and_docs_verifier(self):
        secret, body, ts = "whsec_test", '{"a":1}', 1700000000
        mac = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(pa.sign(secret, body, ts), mac)
        # проверяем именно код проверки из документации
        html = pa.build_docs("https://x")
        start = html.index("import hmac, hashlib, time")
        snippet = html[start:html.index("</pre>", start)].replace("&#x27;", "'").replace("&quot;", '"')
        ns = {}
        exec(snippet, ns)
        header = f"t={int(time.time())},v1={pa.sign(secret, body, int(time.time()))}"
        self.assertTrue(ns["verify"](secret, header, body.encode()))
        self.assertFalse(ns["verify"]("wrong", header, body.encode()))
        old = f"t=1,v1={pa.sign(secret, body, 1)}"
        self.assertFalse(ns["verify"](secret, old, body.encode()))

    def test_ssrf(self):
        bad = ["https://127.0.0.1/h", "https://10.0.0.5/h", "https://192.168.1.1/h", "https://169.254.169.254/latest",
               "https://[::1]/h", "https://[::ffff:127.0.0.1]/h", "http://8.8.8.8/h", "ftp://8.8.8.8/h",
               "https://user:pw@8.8.8.8/h", "https://0.0.0.0/h", "https://" + "a" * 600]
        for u in bad:
            self.assertIsNotNone(run(pa.check_url(u)), u)
        self.assertIsNone(run(pa.check_url("https://8.8.8.8/hook")))

    def test_abs_url(self):
        self.assertEqual(pa._abs("/img/p/1?t=a"), "https://jins.example/img/p/1?t=a")
        self.assertEqual(pa._abs("https://t.me/x"), "https://t.me/x")
        self.assertIsNone(pa._abs(None))


class TestOpenAPI(Base):
    def test_spec_consistency(self):
        spec = pa.build_openapi("https://jins.example")
        ops = [o for p in spec["paths"].values() for o in p.values()]
        self.assertEqual(len(ops), len(pa.ROUTES))
        ids = [o["operationId"] for o in ops]
        self.assertEqual(len(ids), len(set(ids)), "operationId должны быть уникальны")
        for s in pa.ROUTES:
            op = spec["paths"][pa.PREFIX + s["path"]][s["method"].lower()]
            self.assertEqual("security" in op, not s["public"])
            if s["scope"]:
                self.assertEqual(op["x-required-scope"], s["scope"])
                self.assertIn(s["scope"], pa.SCOPES)
        json.dumps(spec)
        for s in pa.ROUTES:
            if s["scope"]:
                self.assertIn(s["scope"], pa.SCOPES)
        for ev, (sc, _) in pa.EVENTS.items():
            self.assertTrue(sc is None or sc in pa.SCOPES, ev)

    def test_docs_mention_every_route(self):
        html = pa.build_docs("https://jins.example")
        for s in pa.ROUTES:
            self.assertIn(pa.PREFIX + s["path"].replace("{", "{"), html.replace("&#x27;", "'"))
        self.assertIn("jns_", html)

    def test_route_conflicts(self):
        """Статические пути должны быть объявлены раньше динамических с тем же числом сегментов и методом."""
        seen = []
        for s in pa.ROUTES:
            segs = s["path"].split("/")
            for prev in seen:
                if prev["method"] == s["method"] and len(prev["path"].split("/")) == len(segs):
                    ps = prev["path"].split("/")
                    if all(a == b or a.startswith("{") for a, b in zip(ps, segs)) and ps != segs:
                        self.fail(f"{prev['path']} перекрывает {s['path']}")
            seen.append(s)


class TestEvents(Base):
    def _row(self, i, **kw):
        d = {"id": i, "created_at": datetime(2026, 11, 9, tzinfo=timezone.utc)}
        d.update(kw)
        return d

    def test_builders(self):
        r = self._row(1, deal_id=5, actor_id=3, event="created", details={"x": 1}, buyer_id=3, seller_id=4, status="awaiting_fee", amount=10)
        t, users, data = pa._ev_escrow(r)[0]
        self.assertEqual((t, users, data["kind"], data["deal_id"]), ("deal.created", [3, 4], "escrow", 5))
        self.assertEqual(pa._ev_escrow(dict(r, event="buyer_confirmed"))[0][0], "deal.updated")
        m = self._row(2, deal_id=9, sender_id=3, body="hi", system=False, buyer_id=3, seller_id=4, status="in_progress")
        self.assertEqual(pa._ev_direct_msg(m)[0][0], "deal.message")
        self.assertEqual(pa._ev_direct_msg(dict(m, system=True))[0][0], "deal.updated")
        c = self._row(3, kind="direct", deal_id=9, outcome="completed", buyer_id=3, seller_id=4, amount=5, fee=0)
        self.assertEqual(pa._ev_receipt(c)[0][0], "deal.closed")
        d = self._row(4, sender_id=3, recipient_id=4, body="yo", post_id=None, deal_id=None)
        self.assertEqual(pa._ev_dm(d)[0][1], [4])
        json.dumps(pa._ev_dm(d)[0][2])

    def test_dispatch_routing_and_scopes(self):
        p = self.pool
        p.hooks = [{"id": 1, "user_id": 10, "events": ["deal.closed", "message.received"], "scopes": ["deals:read"]},
                   {"id": 2, "user_id": 11, "events": ["*"], "scopes": ["read", "deals:read", "messages:read"]}]
        for name, *_ in pa.SOURCES:
            p.settings[f"api_cur_{name}"] = 0
        p.rows["FROM deal_receipts"] = [self._row(1, kind="escrow", deal_id=1, outcome="completed", buyer_id=10, seller_id=99, amount=5, fee=1),
                                        self._row(2, kind="escrow", deal_id=2, outcome="cancelled", buyer_id=98, seller_id=99, amount=5, fee=1)]
        p.rows["FROM direct_messages"] = [self._row(1, sender_id=99, recipient_id=10, body="hey", post_id=None, deal_id=None)]
        self.assertTrue(run(pa._dispatch_events()))
        sent = [(w, e) for w, e, _ in p.queue]
        self.assertIn((1, "deal.closed"), sent)          # покупатель получил чек
        self.assertNotIn((1, "message.received"), sent)  # у ключа нет области messages:read
        self.assertNotIn((2, "deal.closed"), sent)       # вебхук пользователя 11 не причастен к этим сделкам
        self.assertEqual(sum(1 for w, _ in sent if w == 1), 1)
        self.assertEqual(p.settings["api_cur_receipts"], 2)  # курсор продвинут
        self.assertEqual(p.settings["api_cur_dm"], 1)
        payload = p.queue[0][2]
        self.assertTrue(payload["id"].startswith("evt_"))
        self.assertEqual(payload["type"], "deal.closed")

    def test_first_run_does_not_replay_history(self):
        p = self.pool
        p.hooks = [{"id": 1, "user_id": 10, "events": ["*"], "scopes": list(pa.SCOPES)}]
        p.rows["FROM deal_receipts"] = [self._row(1, kind="escrow", deal_id=1, outcome="completed", buyer_id=10, seller_id=9, amount=1, fee=0)]
        run(pa._dispatch_events())
        self.assertEqual(p.queue, [])
        self.assertIn("api_cur_receipts", p.settings)

    def test_no_hooks_is_idle(self):
        self.pool.hooks = []
        self.assertFalse(run(pa._dispatch_events()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
