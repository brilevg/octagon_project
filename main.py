import logging
import sys
import atexit
import os

from core.config import load_config
from core.logger import setup_logging

from database.init_db import build_engine, build_session_factory, create_database
from database.repository import DatabaseRepository

from telegram_collector.connection import ConnectionMonitor
from telegram_collector.client import TelegramClient
from telegram_collector.auth import Authorization
from telegram_collector.collector import TelegramCollector

from telegram_admin.bot import start_admin_bot

from yandex.worker import QualityWorker


setup_logging()
logger = logging.getLogger(__name__)


def _log_unhandled_exception(exc_type, exc_value, exc_traceback):
    """
    Последний рубеж: ловит вообще всё, что долетело до самого верха без
    единого except'а на своём пути (ошибка при чтении конфига, при
    построении engine, любая опечатка в коде main.py верхнего уровня и
    т.п.). Без этого Python печатает такой traceback напрямую в stderr
    через свой стандартный механизм — то есть мимо logging и мимо
    файла лога — и именно такие traceback'и не попадали в лог.
    KeyboardInterrupt пропускаем как обычно (Ctrl-C — не ошибка).
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical(
        "Необработанное исключение верхнего уровня",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


sys.excepthook = _log_unhandled_exception





config = load_config()

engine = build_engine(config.database.url)
create_database(engine)  # create_all — безопасно на каждом старте,
                          # отдельный `python -m database.init_db` не нужен
session_factory = build_session_factory(engine)

client = TelegramClient()
db = DatabaseRepository(session_factory)
auth = Authorization(
    client,
    api_id=config.telegram.api_id,
    api_hash=config.telegram.api_hash,
    phone_number=config.telegram.phone_number,
    password=config.telegram.password,
)
connection_monitor = ConnectionMonitor()
collector = TelegramCollector(client)

# У админ-бота своя сессия (из того же session_factory, но отдельный
# объект Session — сессии SQLAlchemy нельзя шарить между потоками) и
# собственный фоновый поток (см. telegram_admin/bot.py). Дальше main.py
# просто продолжает заниматься TDLib-циклом, ничего больше не блокирует.
admin_bot = start_admin_bot(
    config.bot.bot_token,
    config.bot.admin_ids,
    session_factory,
)

# Worker получает собственный репозиторий и отдельную сессию БД.
quality_worker = None

if os.getenv("YANDEX_API_KEY") and os.getenv("YANDEX_FOLDER_ID"):
    try:
        quality_worker = QualityWorker(
            repository_factory=lambda: DatabaseRepository(session_factory),
            poll_interval=5,
            batch_size=5,
            pipeline_version="v1",
            max_attempts=5,
            retry_delay_seconds=60,
        )

        quality_worker.start()
        logger.info("LLM quality worker запущен")

    except Exception:
        quality_worker = None
        logger.exception(
            "Не удалось запустить LLM worker. "
            "Сбор Telegram продолжит работу."
        )
else:
    logger.warning(
        "LLM worker не запущен: проверь YANDEX_API_KEY "
        "и YANDEX_FOLDER_ID."
    )

# proxy_sent = False
# if not proxy_sent and auth._tdlib_params_sent:
    # proxy_sent = True
client.send({
    "@type": "addProxy",
    "proxy": {
        "@type": "proxy",
        "server": config.telegram.proxy_server,
        "port": config.telegram.proxy_port,
        "type": {
            "@type": "proxyTypeMtproto",
            "secret": config.telegram.proxy_secret
        }
    },
    "enable": True
})

client.send({
    "@type": "setLogVerbosityLevel",
    "new_verbosity_level": 3
})
client.execute({
    "@type": "setLogStream",
    "log_stream": {
        "@type": "logStreamFile",
        "path": "logs/tdlib_native.log",
        "max_file_size": 10 * 1024 * 1024,
        "redirect_stderr": True
    }
})

# Запрашиваем состояние авторизации
client.send({
    "@type": "getAuthorizationState"
})

logger.info("Telegram Collector started...")

try:
    while True:

        try:
            update = client.receive(1)

            auth.handle(update)
            connection_monitor.handle(update)
            connection_monitor.check_stuck()

            if not update:
                continue

            logger.debug(update)

            if auth.is_authorized:
                collector.handle(update)

            result = db.process(update)

            if isinstance(result, list):
                for request in result:
                    client.send(request)
            elif result:
                client.send(result)

        except KeyboardInterrupt:
            raise

        except Exception:
            # Раньше любое исключение из auth.handle/connection_monitor/
            # collector.handle (db.process себя уже сам ловит и логирует)
            # ничем не перехватывалось внутри цикла и улетало наверх как
            # необработанное — то есть traceback печатался в консоль, а не
            # в лог-файл, и цикл обработки update'ов после этого полностью
            # останавливался. Теперь такая ошибка просто логируется, а
            # цикл продолжает обрабатывать следующие update'ы.
            logger.exception(
                "Необработанная ошибка при обработке update, цикл продолжает работу"
            )

except KeyboardInterrupt:
    logger.info("Остановка по Ctrl-C")

finally:
    if quality_worker is not None:
        quality_worker.stop()
    admin_bot.stop()