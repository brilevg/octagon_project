# Telegram Collector

Инструмент для сбора и хранения переписки из Telegram-чатов с возможностью выгрузки данных через Telegram-бота.

## Что делает

- Подключается к Telegram через TDLib (tdlib/tdjson) под реальным аккаунтом
- Скачивает историю всех чатов и слушает новые сообщения в реальном времени
- Сохраняет сообщения, вложения и реакции в PostgreSQL с полной историей правок и удалений
- Параллельно запускает Telegram-бот для администраторов, через который можно выгрузить переписку в Excel

---

## Архитектура

```
main.py
├── telegram_collector/       — TDLib-клиент
│   ├── client.py             — обёртка над tdjson (send/receive/execute)
│   ├── auth.py               — машина состояний авторизации TDLib
│   ├── collector.py          — разбор update'ов, запросы истории и реакций
│   └── connection.py         — мониторинг состояния соединения и прокси
│
├── telegram_admin/           — Telegram-бот для администраторов
│   ├── bot.py                — сборка Application, фоновый поток polling'а
│   ├── commands.py           — хендлеры команд (/start, /chats, /export)
│   └── keyboards.py          — inline-клавиатуры
│
├── database/
│   ├── models.py             — SQLAlchemy ORM (User, Chat, Message, ...)
│   ├── repository.py         — вся работа с БД (DatabaseRepository)
│   └── init_db.py            — create_engine / sessionmaker / create_all
│
└── core/
    ├── config.py             — загрузка переменных окружения в датаклассы
    └── logger.py             — настройка logging (файл + консоль)
```

Коллектор и бот работают в одном процессе: `main.py` крутит TDLib-цикл в главном потоке, а бот — в отдельном фоновом потоке со своим event loop'ом.

---

## База данных

| Таблица | Назначение |
|---|---|
| `users` | Авторы сообщений |
| `chats` | Чаты |
| `messages` | Актуальное состояние каждого сообщения |
| `message_revisions` | Лог изменений: `created` / `edited` / `deleted` |
| `attachments` | Вложения (фото, документы, видео, голосовые) |
| `reactions` | Реакции с полным списком авторов |

Удалённые сообщения помечаются флагом `is_deleted`, но не удаляются из базы. История правок хранится в `message_revisions` — одна строка на событие, ничего не перезаписывается.

---

## Требования

- Python 3.11+
- PostgreSQL
- Библиотека tdjson (TDLib 1.8.x, собранная под вашу платформу)
- Telegram API credentials (api_id / api_hash) — получить на [my.telegram.org](https://my.telegram.org)
- Telegram Bot Token — создать через [@BotFather](https://t.me/BotFather)

---

## Установка

```bash
git clone https://github.com/brilevg/octagon_project
cd octagon_project
pip install -r requirements.txt
```

`requirements.txt`:
```
python-dotenv==1.2.2
tdjson==1.8.65
sqlalchemy==2.0.51
psycopg2==2.9.12
python-telegram-bot==22.8
openpyxl==3.1.5
```

---

## Конфигурация

Создайте файл `.env` в корне проекта:

```dotenv
# Telegram API (my.telegram.org)
API_ID=123456
API_HASH=abcdef1234567890abcdef1234567890
PHONE_NUMBER=+79991234567
PASSWORD=your_2fa_password        # если включена двухфакторная аутентификация

# MTProxy (опционально)
PROXY_SERVER=proxy.example.com
PROXY_PORT=443
PROXY_SECRET=ee...

# Telegram-бот
BOT_TOKEN=123456789:AAF...
ADMIN_IDS="111111111,222222222"      # telegram user_id администраторов, через запятую

# PostgreSQL
DB_USER=postgres
DB_PASSWORD=secret
DB_HOST=localhost
DB_PORT=5432
DB_NAME=collector
```

Если прокси не нужен — оставьте `PROXY_SERVER`, `PROXY_PORT`, `PROXY_SECRET` пустыми.

---

## Запуск

```bash
python main.py
```

При первом запуске TDLib потребует ввести код подтверждения из Telegram (в консоль). После авторизации сессия сохраняется в `tdlib/<md5_от_номера>/` и повторного ввода кода не потребуется.

База данных создаётся автоматически при каждом старте (`create_all` — безопасно, не трогает существующие таблицы).

---

## Telegram-бот (для администраторов)

Доступ имеют только пользователи из `ADMIN_IDS`.

| Команда | Описание |
|---|---|
| `/start` | Информация о боте и статус доступа |
| `/chats` | Список чатов в базе |
| `/export` | Выбрать чат и период, получить Excel-файл |
| `/cancel` | Отменить текущую операцию |

Выгрузка (`/export`) формирует `.xlsx` с тремя листами: **Сообщения**, **Вложения**, **Реакции**. В выгрузку попадают в том числе удалённые сообщения (с пометкой в колонке «Удалено»).

---

## Структура файлов

```
.
├── main.py
├── requirements.txt
├── .env                     # не коммитить
├── core/
│   ├── config.py
│   └── logger.py
├── database/
│   ├── models.py
│   ├── repository.py
│   └── init_db.py
├── telegram_collector/
│   ├── client.py
│   ├── auth.py
│   ├── collector.py
│   └── connection.py
├── telegram_admin/
│   ├── bot.py
│   ├── commands.py
│   └── keyboards.py
├── logs/                    # создаётся автоматически
│   ├── collector.log
│   └── tdlib_native.log
└── tdlib/                   # создаётся TDLib автоматически
    └── <md5_номера>/
```

---

## Логирование

Все события пишутся одновременно в консоль и в `logs/collector.log` (ротация по 10 МБ, 5 файлов). Нативные логи TDLib — в `logs/tdlib_native.log` (также 10 МБ).

Ключевые префиксы в логах:

| Префикс | Что означает |
|---|---|
| `AUTH:` | Шаги авторизации TDLib |
| `CONNECTION:` | Смена состояния соединения |
| `PROXY:` | Проблемы с прокси |
| `ADMIN BOT:` | События Telegram-бота |

---

## Важные особенности

**Сессии TDLib** хранятся отдельно для каждого номера телефона (путь включает MD5 от номера) — смена `PHONE_NUMBER` в `.env` не подхватит чужую сессию автоматически.

**Реакции** запрашиваются постранично через `getMessageAddedReactions`. Для одного сообщения в любой момент времени идёт не более одного цикла запросов, чтобы исключить гонку при параллельных обновлениях.

**Сессии SQLAlchemy** не расшариваются между потоками: каждый вызов к БД из бота открывает и закрывает собственную короткоживущую Session.

**Схема БД** создаётся автоматически при старте. При изменении существующих таблиц `create_all` не применяет ALTER — для этого нужен Alembic.