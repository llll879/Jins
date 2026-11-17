# jins-bot — бот + мини-апп (сервис на Render)

Первая половина проекта Jins. Здесь: Telegram-бот (`bot.py`), мини-апп (`index.html`), ИИ-поддержка, публичный API,
юзербот-воркер, сбор объявлений в группах. **Форум вынесен в репозиторий `jins-forum`** — он работает с той же базой данных.

Что осталось от форума в этом репозитории: `forum_login.py` — подтверждение входа в форум (`/start forum_КОД`) и таблицы
`forum_login` / `forum_chat_prefs`. Без него кнопка «Войти через Telegram» в форуме не работала бы.

## Переменные окружения
Смотрите `.env.example`. Обязательные: `BOT_TOKEN`, `DATABASE_URL`, `OWNER_ID`. Остальное — как раньше (см. `README.txt`).
`DATABASE_URL` должен быть **таким же**, как у `jins-forum`.

## Запуск на Render
1. New → **Web Service** → репозиторий `jins-bot`.
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `python bot.py`
4. Environment: переменные из `.env.example` (минимум `BOT_TOKEN`, `DATABASE_URL`, `OWNER_ID`).
5. Health Check Path: `/health`.

«Будилка» работает как и раньше: бот сам пингует `RENDER_EXTERNAL_URL/health` раз в 10 минут, чтобы Render не усыплял сервис.

**Запускать этот сервис нужно первым**: именно он создаёт в БД таблицы (`users`, `direct_messages`, …). Форум на пустой базе подождёт его.

## Локальный запуск
```bash
pip install -r requirements.txt
cp .env.example .env   # заполните и экспортируйте переменные (export $(grep -v '^#' .env | xargs))
python bot.py
```
Тесты: `python -m unittest discover -s tests -v`.

## Важно
- Бот — единственный, кто делает polling (`getUpdates`). Форум токен использует только для отправки сообщений.
- `README.txt` — журнал версий; ИИ-поддержка использует его как базу знаний (копия лежит и в `jins-forum`).
