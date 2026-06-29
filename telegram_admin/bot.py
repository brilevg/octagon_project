# telegram_admin/bot.py
import asyncio
import logging
import threading
import time

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import commands
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    # Регистрируем список команд в Telegram, чтобы они показывались в
    # меню "/" рядом с полем ввода — пользователю не нужно гадать,
    # что умеет бот.
    await application.bot.set_my_commands([
        ("start", "Информация о боте и доступе"),
        ("chats", "Список отслеживаемых чатов"),
        ("export", "Выгрузить переписку за период"),
        ("cancel", "Отменить текущую операцию"),
    ])

from telegram.error import NetworkError, TimedOut


async def _on_error(update, context):
    error = context.error

    # TimedOut/NetworkError — это кратковременные обрывы связи с Telegram
    # (см. комментарий выше про connect_timeout/read_timeout), а не баг в
    # коде. Полный traceback для них только зашумляет лог — он одинаковый
    # при каждом таком обрыве и не несёт новой информации. Пишем короткое
    # предупреждение без трейса, а для всего остального (реальные баги в
    # хендлерах) — как и раньше, logger.exception с полным traceback.
    if isinstance(error, (TimedOut, NetworkError)):
        logger.warning(
            "ADMIN BOT: временная сетевая ошибка при обращении к Telegram "
            "(%s: %s) — update пропущен, при необходимости повторите действие",
            type(error).__name__, error,
        )
        return

    logger.exception("ADMIN BOT: необработанная ошибка", exc_info=error)
def build_application(bot_token: str, admin_ids, session_factory) -> Application:
    
    # ВАЖНО: у getUpdates (long polling) и у обычных вызовов API (answer,
    # edit_message_text, send_document...) должны быть РАЗНЫЕ HTTPXRequest
    # с разными пулами соединений. Если использовать один и тот же объект
    # (как было раньше), long-poll запрос занимает единственное свободное
    # соединение в пуле на всё время ожидания, и любой обычный вызов API —
    # например query.answer() сразу после нажатия кнопки — блокируется в
    # ожидании свободного соединения и тихо падает по pool_timeout.
    # Пользователь в этом случае видит "крутящуюся" кнопку, которая сама
    # гаснет по таймауту на стороне Telegram, и никакого видимого эффекта.
    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=30.0,
        pool_timeout=5.0,
        connection_pool_size=8,
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=40.0,
        pool_timeout=5.0,
        connection_pool_size=1,
    )
    application = (
        Application.builder()
        .token(bot_token)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(_post_init)
        .build()
    )
    
    # Раньше здесь создавался один-единственный DatabaseRepository (и
    # внутри него — одна Session) на всё время жизни бота, и дальше все
    # обработчики команд брали его из bot_data и дёргали через
    # asyncio.to_thread. Проблема: эта Session создавалась в потоке
    # main.py, использовалась из потока admin-bot (см. AdminBot._run), а
    # дальше ещё и из пул-потоков asyncio.to_thread — то есть у одной
    # Session/соединения к БД получалось несколько разных "хозяев"-потоков
    # за время её жизни, плюс при обращении к отношениям ORM
    # (msg.sender, msg.attachments и т.д.) уже ПОСЛЕ возврата из
    # to_thread это лезло в БД синхронно прямо из event loop бота, блокируя
    # его. Поэтому экспорт периодически "зависал"/не доезжал до
    # пользователя. Теперь сохраняем сам session_factory, а каждый вызов
    # к БД (см. commands._run_db) создаёт и закрывает свою Session —
    # коротко и в одном потоке.
    application.bot_data["session_factory"] = session_factory
    application.bot_data["admin_ids"] = set(admin_ids)
    application.add_error_handler(_on_error)
    application.add_handler(CommandHandler("start", commands.start))
    application.add_handler(CommandHandler("chats", commands.list_chats))

    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("export", commands.export_start)],
            states={
                commands.CHOOSING_PERIOD: [
                    CallbackQueryHandler(commands.choose_chat, pattern=r"^chat:"),
                    CallbackQueryHandler(commands.choose_period, pattern=r"^period:"),
                ],
                commands.AWAITING_CUSTOM_RANGE: [
                    MessageHandler(
                        # ВАЖНО: filters.TEXT матчится не только на новое
                        # сообщение (update.message), но и на ПРАВКУ уже
                        # отправленного сообщения (update.edited_message) —
                        # filters проверяют update.effective_message, а не
                        # update.message напрямую. Без filters.UpdateType.
                        # MESSAGE хендлер вызывался и на edited_message, где
                        # update.message всегда None, и падал с AttributeError
                        # в custom_range_received.
                        filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE,
                        commands.custom_range_received,
                    
                    ),
                ],
            },
            fallbacks=[CommandHandler("cancel", commands.cancel)],
            allow_reentry=True,   # повторный /export сбрасывает состояние без /cancel
            per_message=False,    # явно — убирает PTBUserWarning, фиксирует поведение
        )
    )

    return application


class AdminBot:
    """
    Обёртка над Application + потоком polling'а. Держит ссылку на
    event loop, который реально крутит run_polling() — она нужна,
    чтобы корректно остановить бота из другого (главного) потока:
    asyncio-объекты не thread-safe "из коробки", и stop_running()
    обязан выполняться в том же потоке/loop'е, где работает сам бот.
    """

    def __init__(self, application: Application):
        self.application = application
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._crashed_with: BaseException | None = None

    def start(self) -> "AdminBot":
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            try:
                # ВАЖНО: allowed_updates не передаётся ретроактивно и, если
                # не указан явно, Telegram использует значение, выставленное
                # последним вызовом getUpdates/setWebhook для этого токена —
                # когда бы и чем бы он ни был сделан. Если в какой-то момент
                # (тестовый скрипт, другая библиотека, более ранний запуск)
                # этот список не включал "callback_query", Telegram продолжит
                # тихо отфильтровывать нажатия inline-кнопок на своей стороне
                # навсегда — без единой ошибки на нашей стороне, при этом
                # обычные текстовые команды будут приходить как ни в чём не
                # бывало. Передаём Update.ALL_TYPES явно при каждом запуске,
                # чтобы раз и навсегда исключить эту причину.
                # stop_signals=None: SIGINT/SIGTERM можно перехватывать
                # только в главном потоке процесса, а этот поток — не главный
                self.application.run_polling(
                    stop_signals=None,
                    allowed_updates=Update.ALL_TYPES,
                )
            except BaseException as exc:
                # КРИТИЧНО: без этого try/except любое исключение, которое
                # вылетело из run_polling() САМОГО (а не из конкретного
                # хендлера — те уже ловит add_error_handler в bot_data),
                # тихо убивает этот поток. Поллинг просто останавливается,
                # новые update'ы (в т.ч. нажатия inline-кнопок) больше
                # никогда не доходят ни до одного хендлера, а в файл логов
                # ничего не попадает — Python печатает такие исключения
                # только в stderr через стандартный threading.excepthook,
                # который легко не заметить, если смотреть только лог-файл.
                self._crashed_with = exc
                logger.exception(
                    "ADMIN BOT: поток polling'а аварийно завершился"
                )

        self._thread = threading.Thread(target=_run, daemon=True, name="admin-bot")
        self._thread.start()
        self._loop_ready.wait(timeout=10)

        # Раньше тут сразу логировалось "polling запущен" — но это
        # происходило ДО того, как run_polling() успевал хоть что-то
        # сделать (loop_ready.set() стоит до вызова run_polling()).
        # Поэтому даже если поток падал в первую секунду, лог всё равно
        # говорил "запущен". Даём потоку реальный шанс либо начать
        # polling, либо упасть — и проверяем фактическое состояние.
        time.sleep(2)
        if not self._thread.is_alive():
            logger.error(
                "ADMIN BOT: поток polling'а умер сразу после старта, причина: %r",
                self._crashed_with,
            )
        else:
            logger.info("ADMIN BOT: polling запущен")

        return self

    def stop(self) -> None:
        """
        call_soon_threadsafe кладёт stop_running() в очередь именно
        того loop'а, который крутит run_polling() — выполнится она там,
        внутри уже работающего loop'а, поэтому get_running_loop() внутри
        stop_running() найдёт его и не упадёт. Прямой вызов
        application.stop_running() из главного потока (как было раньше)
        этого гарантировать не может — отсюда и RuntimeError.
        """
        if self._thread is None or self._loop is None:
            return

        self._loop.call_soon_threadsafe(self.application.stop_running)
        self._thread.join(timeout=10)

        if self._thread.is_alive():
            logger.warning("ADMIN BOT: поток polling'а не завершился за 10с")


def start_admin_bot(bot_token: str, admin_ids, session_factory) -> AdminBot:
    application = build_application(bot_token, admin_ids, session_factory)
    return AdminBot(application).start()