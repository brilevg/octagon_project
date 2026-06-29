from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# callback_data кодируется как "chat:<telegram_chat_id>" и "period:<код>"
PERIOD_TODAY = "today"
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
PERIOD_CUSTOM = "custom"

PERIOD_LABELS = {
    PERIOD_TODAY: "Сегодня",
    PERIOD_WEEK: "Последние 7 дней",
    PERIOD_MONTH: "Последние 30 дней",
    PERIOD_CUSTOM: "Свой период",
}


def chats_keyboard(chats):
    """
    chats — список объектов Chat (database.models.Chat), как возвращает
    DatabaseRepository.list_chats(). В callback_data кладём
    telegram_chat_id, а не внутренний Chat.id — он же используется
    методами репозитория, принимающими параметры из TDLib.
    """
    buttons = [
        [
            InlineKeyboardButton(
                chat.title or str(chat.telegram_chat_id),
                callback_data=f"chat:{chat.telegram_chat_id}",
            )
        ]
        for chat in chats
    ]
    return InlineKeyboardMarkup(buttons)


def periods_keyboard():
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"period:{code}")]
        for code, label in PERIOD_LABELS.items()
    ]
    return InlineKeyboardMarkup(buttons)