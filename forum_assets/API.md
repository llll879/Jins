# Jins API v1

Подключайте базу репутации, объявления, проекты, сделки и чаты Jins к любому своему проекту: сайту, боту, CRM, мобильному приложению.

* Онлайн-документация со всеми методами и примерами `curl`: **`https://ВАШ-ХОСТ/api/v1/docs`** (короткий адрес — `/developers`)
* Спецификация OpenAPI 3: `https://ВАШ-ХОСТ/api/v1/openapi.json` (файл `openapi.json` лежит и в репозитории) — импортируется в Postman / Insomnia / Swagger и позволяет сгенерировать клиент на любом языке (`openapi-generator`)
* Готовые клиенты без зависимостей: `sdk/jins_client.py` (Python), `sdk/jins.js` + `sdk/jins.d.ts` (Node, браузер, Deno, Bun)

## Быстрый старт (3 минуты)

1. В боте отправьте **`/api new Мой сайт`** — бот пришлёт ключ вида `jns_…`. Он показывается **один раз**, сохраните его.
2. Проверьте связь:
   ```bash
   curl -H "Authorization: Bearer $JINS_KEY" https://ВАШ-ХОСТ/api/v1/me
   ```
3. Проверьте человека перед сделкой:
   ```bash
   curl -H "Authorization: Bearer $JINS_KEY" https://ВАШ-ХОСТ/api/v1/check/@username
   # {"ok":true,"data":{"found":true,"id":123,"username":"username","verdict":"trusted","stars":5,...}}
   ```

```python
from jins_client import Jins
jins = Jins("https://ВАШ-ХОСТ", "jns_...")
print(jins.check("@username")["verdict"])      # trusted | neutral | risk | scam | unknown
```

```js
const { Jins } = require('./sdk/jins');
const jins = new Jins('https://ВАШ-ХОСТ', process.env.JINS_KEY);
console.log((await jins.check('@username')).verdict);
```

## Ключи и права

Команды бота (только в личке):

| Команда | Что делает |
|---|---|
| `/api` | справка и список ваших ключей |
| `/api new Название` | ключ только для чтения (`read`) |
| `/api new Название all` | ключ со всеми правами |
| `/api new Название read,deals:read` | ключ с выбранными правами |
| `/api revoke 3` | отозвать ключ №3 (его вебхуки отключаются) |

Тот же функционал для мини-аппа: `GET/POST /api/apikeys`, `POST /api/apikeys/revoke` (авторизация через initData, как у остального мини-аппа). При создании через `POST /api/apikeys` можно задать `rate_limit` (10–600 в минуту), `expires_days` и `allowed_ips` (список IP/подсетей).

Ключ действует **от имени своего владельца**: «мои сделки» — это сделки владельца, сообщения уходят от его имени. Админские методы мини-аппа через API-ключи недоступны.

| Область | Даёт доступ |
|---|---|
| `read` | объявления, темы, проекты, профили, база репутации, проверка пользователей |
| `deals:read` | свои сделки (гарант и прямые) и чеки |
| `deals:write` | создание сделок, сообщения и действия в них |
| `messages:read` / `messages:write` | чтение / отправка личных сообщений |
| `posts:write` | подача объявлений на модерацию |
| `webhooks` | управление вебхуками ключа |

Выдавайте минимум нужных прав: ключ для сайта, который лишь проверяет репутацию, должен иметь только `read`.

## Формат ответов

Всегда JSON в одном виде:

```json
{ "ok": true,  "data": { ... }, "meta": { "limit": 20, "offset": 0, "has_more": true } }
{ "ok": false, "error": { "code": "insufficient_scope", "message": "…", "status": 403, "required_scope": "deals:read" } }
```

Ориентируйтесь на `error.code` (машинный, стабильный), а не на текст `message`. Каждый ответ содержит `X-Request-Id` — называйте его при обращении в поддержку.

| HTTP | Коды | Значение |
|---|---|---|
| 400 | `invalid_param`, `invalid_json`, `bad_input`, `invalid_scope`… | неверные параметры |
| 401 | `missing_api_key`, `invalid_api_key`, `key_revoked`, `key_expired` | проблема с ключом |
| 403 | `insufficient_scope`, `ip_not_allowed`, `forbidden`, `subscribe` | нет прав |
| 404 | `user_not_found`, `post_not_found`, `deal_not_found`… | не найдено |
| 409 | `bad_status`, `too_many_keys`… | конфликт состояния |
| 429 | `rate_limited` | превышен лимит (смотрите `Retry-After`) |
| 5xx | `server`, `escrow_offline` | сбой / гарант недоступен — повторите позже |

## Лимиты, пагинация, идемпотентность

* **Лимит:** по умолчанию 120 запросов/мин на ключ. Заголовки `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`; при превышении — `429` и `Retry-After`. SDK сами ждут и повторяют.
* **Пагинация:** `limit` (1–100) и `offset`; в `meta.has_more` — есть ли продолжение. В SDK есть `iter_posts` / `iterPosts` — обход всей ленты.
* **Идемпотентность:** для `POST` передайте заголовок `Idempotency-Key` (любая уникальная строка). Повтор того же запроса в течение 24 часов вернёт тот же ответ и заголовок `Idempotent-Replay: true` — сделка не создастся дважды при сетевом сбое.
* **CORS** включён (`API_CORS_ORIGINS`, по умолчанию `*`), но **не храните ключ в браузерном коде** — вызывайте API со своего сервера.

## Рецепты

**Бейдж «репутация» на сайте / проверка контрагента**

```python
r = jins.check("@seller")
if r["verdict"] in ("scam", "risk"): block_checkout()
```

**Создать сделку через гаранта** (ключ с `deals:write`; ключ — покупатель)

```python
deal = jins.create_escrow_deal(seller_id=123, amount=500, terms="Подарок X", idempotency_key=order_id)
```

**Узнавать о закрытии сделок без опроса** — вебхук `deal.closed` (ниже).

**Выгрузить все объявления темы**

```python
for post in jins.iter_posts(topic=3): save(post)
```

## Вебхуки

Создание (ключ с областью `webhooks`):

```python
hook = jins.create_webhook("https://your.site/jins-hook", events=["deal.closed", "message.received"])
print(hook["secret"])    # whsec_… — показывается один раз; новый: POST /webhooks/{id}/rotate_secret
jins.test_webhook(hook["id"])   # пришлёт событие ping
```

| Событие | Нужная область | Когда |
|---|---|---|
| `post.published` | `read` | опубликовано новое объявление |
| `deal.created` | `deals:read` | создана сделка с участием владельца ключа |
| `deal.updated` | `deals:read` | изменился статус / ход сделки |
| `deal.message` | `deals:read` | новое сообщение в чате сделки |
| `deal.closed` | `deals:read` | сделка закрыта (завершена / отменена / возврат), чек готов |
| `message.received` | `messages:read` | владельцу ключа написали в личные сообщения |
| `ping` | — | тест |

Событийные уведомления приходят `POST`-запросом:

```json
{ "id": "evt_8f2c…", "type": "deal.closed", "created_at": "2026-11-09T12:00:00+00:00",
  "data": { "kind": "escrow", "deal_id": 17, "outcome": "completed", "amount": 500, "fee": 25, "buyer_id": 1, "seller_id": 2 } }
```

* **Подпись.** Заголовок `X-Jins-Signature: t=<unix>,v1=<hex>`; `v1` = HMAC-SHA256 секретом вебхука от строки `"{t}.{сырое тело}"`. Проверяйте сырое тело и отвергайте запросы старше 5 минут. Готовые функции: `verify_webhook` (Python), `verifyWebhook` (Node); рабочие приёмники — `examples/webhook_flask.py`, `examples/webhook_express.js`.
* **Доставка — «минимум один раз».** Отвечайте любым `2xx` в течение 10 с. Иначе повторы через 10 с, 1 мин, 5 мин, 30 мин, 2 ч, 6 ч; после этого доставка помечается неуспешной (`GET /webhooks/{id}/deliveries`). После 10 неуспешных доставок подряд вебхук отключается (`PATCH /webhooks/{id}` с `{"active": true}` включит снова). Событие могут прислать повторно — дедуплицируйте по `id`.
* **Безопасность адреса.** Нужен `https://`. Адреса во внутренних сетях (localhost, 10.x, 192.168.x, 169.254.x и т. п.) запрещены; редиректы не выполняются.
* События отдаются только тем, кого они касаются: `deal.*` и `message.received` — владельцу ключа (участнику сделки / получателю), `post.published` — любому вебхуку с областью `read`.
* Старые события при создании первого вебхука не «проигрываются» — приходят только новые.

## Справочник методов

Подробные параметры и примеры `curl` — на странице `/api/v1/docs`.

<!-- ENDPOINTS:START -->
<!-- ENDPOINTS:END -->

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `API_CORS_ORIGINS` | `*` | значение `Access-Control-Allow-Origin` |
| `API_TRUST_PROXY` | `1` | брать IP клиента из `X-Forwarded-For` (нужно за Render/nginx; `0` — отключить) |
| `API_WEBHOOK_ALLOW_HTTP` | `0` | `1` — разрешить `http://` для вебхуков (только для отладки) |
| `API_WEBHOOK_ALLOW_PRIVATE` | `0` | `1` — разрешить внутренние адреса вебхуков (только для отладки) |

## Для владельца сервиса

* Подключение к проекту: файл `public_api.py` рядом с `bot.py`; в `bot.py` — импорт, три вызова (`install`, `start`, роутер команды `/api`) и обход `initData` для путей `/api/v1/*`. Новые таблицы (`api_keys`, `api_webhooks`, `api_webhook_queue`) создаются сами при запуске.
* Ключи хранятся только в виде SHA-256-хэша; потерянный ключ восстановить нельзя — отзовите и выпустите новый. Отозвать любой ключ может владелец сервиса (`OWNER_ID`) командой `/api revoke <id>`.
* Существующие правила (проверка подписки, блокировки, комиссии, уведомления) не дублируются: методы сделок, чатов и объявлений вызывают прежние обработчики мини-аппа. Поэтому владелец ключа, не подписанный на обязательные каналы, получит `403 subscribe` на действиях от своего имени — как и в мини-аппе.
* Лимиты запросов и кэш идемпотентности хранятся в памяти процесса. При нескольких экземплярах сервиса лимит применяется на каждый экземпляр отдельно.
* Проверка: `python -m unittest discover -s tests -v` (БД и Telegram подменяются заглушками). Пересобрать `openapi.json` и таблицу выше: `python tools/gen_api_docs.py https://ваш-хост`.
