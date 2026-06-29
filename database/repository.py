import logging
from datetime import datetime
import traceback
from sqlalchemy.orm import selectinload
from .models import (
    Attachment,
    Chat,
    Message,
    MessageRevision,
    Reaction,
    User,
)
logger = logging.getLogger(__name__)

def parse_reaction_type(reaction_type):
    """
    ReactionType в TDLib бывает трёх видов: reactionTypeEmoji,
    reactionTypeCustomEmoji, reactionTypePaid.
    Возвращает (kind, emoji, custom_emoji_id).
    """
    t = (reaction_type or {}).get("@type")

    if t == "reactionTypeEmoji":
        return "emoji", reaction_type.get("emoji"), None

    if t == "reactionTypeCustomEmoji":
        return "custom_emoji", None, reaction_type.get("custom_emoji_id")

    if t == "reactionTypePaid":
        return "paid", None, None

    return "unknown", None, None


class DatabaseRepository:

    def __init__(self, session_factory):
        self.db = session_factory()

        # Буфер для постраничного ответа getMessageAddedReactions (см.
        # save_added_reactions). collector.py запрашивает реакции
        # постранично через offset/next_offset — копим страницы здесь и
        # сверяем с БД только когда дошли до последней (next_offset пуст),
        # иначе по промежуточной странице можно по ошибке погасить ещё не
        # увиденные, но всё ещё активные реакции с других страниц.
        #
        # Буфер хранит сырые telegram-идентификаторы отправителей, а не
        # внутренние DB id (user.id / chat.id). Это важно: getUser-ответ
        # может прийти ПОСЛЕ addedReactions (сетевые запросы возвращаются
        # не в порядке отправки), и если резолвить get_user() сразу при
        # получении страницы реакций — для ещё-не-сохранённых пользователей
        # get_user() вернёт None, и реакция уйдёт в БД без автора.
        # Резолвим telegram_id → DB id только в момент финальной записи
        # (last page), когда вся нужная информация уже, скорее всего, есть.
        self._added_reactions_buffer = {}

        # Буфер interaction_info для чатов где can_get=None (basicGroup,
        # private). Для них getMessageAddedReactions возвращает пустой
        # список — это не "реакций нет", а "API недоступен для этого типа
        # чата". Сохраняем interaction_info при первом вызове
        # _apply_interaction_info, чтобы save_added_reactions мог
        # использовать recent_sender_ids как fallback при получении пустого
        # ответа вместо того, чтобы гасить уже записанные реакции.
        self._interaction_info_buffer = {}

        self.handlers = {
            "user": self.save_user,
            "chat": self.save_chat,
            "updateNewChat": lambda u: self.save_chat(u["chat"]),
            "messages": self.save_messages,
            "message": self.save_message_update,
            "updateNewMessage": self.save_new_message,
            "updateMessageContent": self.update_message_content,
            "updateMessageEdited": self.update_message_edited,
            "updateDeleteMessages": self.delete_messages,
            "updateMessageInteractionInfo": self.update_interaction_info,
            "updateFile": self.update_file,
            # Ответ на getMessageAddedReactions — основной (а не запасной)
            # путь получения реакций для чатов, где can_get_added_reactions
            # = True, то есть для большинства чатов. Раньше для этого типа
            # update'а здесь не было обработчика вообще: collector.py
            # получал такой ответ только чтобы вести пагинацию
            # (offset/next_offset), а сами реакции — кто, чем и когда
            # отреагировал — никуда не записывались, в БД они не попадали.
            "addedReactions": self.save_added_reactions,
        }

    def process(self, update):

        handler = self.handlers.get(update.get("@type"))

        if handler is None:
            return None

        try:
            result = handler(update)
            self.db.commit()
            return result

        except Exception:
            self.db.rollback()
            logger.exception("Ошибка обработки update %s", update.get("@type"))
            return None

    def close(self):
        self.db.close()

    # ---------- пользователи / чаты ----------

    def get_user(self, telegram_id):
        return (
            self.db.query(User)
            .filter(User.telegram_id == telegram_id)
            .first()
        )

    def save_user(self, update):

        telegram_id = update["id"]

        usernames = update.get("usernames", {})
        active = usernames.get("active_usernames", [])

        username = active[0] if active else None

        user = self.get_user(telegram_id)

        if user is None:

            user = User(
                telegram_id=telegram_id,
                username=username,
                first_name=update.get("first_name"),
                last_name=update.get("last_name"),
                phone=update.get("phone_number"),
                is_bot=update.get("type", {}).get("@type") == "userTypeBot"
            )

            self.db.add(user)

        else:

            user.username = username
            user.first_name = update.get("first_name")
            user.last_name = update.get("last_name")
            user.phone = update.get("phone_number")
            user.is_bot = update.get("type", {}).get("@type") == "userTypeBot"

        return None
    
    def list_chats(self):
        """Для админ-интерфейса: список чатов, доступных для выгрузки."""
        return self.db.query(Chat).order_by(Chat.title).all()
    
    def get_chat(self, telegram_chat_id):
        return (
            self.db.query(Chat)
            .filter(Chat.telegram_chat_id == telegram_chat_id)
            .first()
        )

    def save_chat(self, update):

        chat = self.get_chat(update["id"])

        if chat is None:

            chat = Chat(
                telegram_chat_id=update["id"],
                title=update.get("title"),
                chat_type=update["type"]["@type"]
            )

            self.db.add(chat)

        else:

            chat.title = update.get("title")
            chat.chat_type = update["type"]["@type"]

        return None

    # ---------- сообщения ----------
    def get_messages_for_period(self, telegram_chat_id, date_from, date_to, include_deleted=False):
        """
        Сообщения чата за период [date_from, date_to] (datetime, UTC —
        в той же зоне, что и tg_date). Используется административным
        модулем при выгрузке переписки по команде.
        """
        chat = self.get_chat(telegram_chat_id)
        if chat is None:
            return []

        query = self.db.query(Message).filter(
            Message.chat_id == chat.id,
            Message.tg_date >= date_from,
            Message.tg_date <= date_to,
        )

        if not include_deleted:
            query = query.filter(Message.is_deleted.is_(False))

        return query.order_by(Message.tg_date.asc()).all()


    def get_message_full(self, message_id):
        """
        Сообщение со всеми связанными данными (вложения, реакции, ревизии) —
        для детальной выгрузки одного сообщения.
        """
        return self.db.query(Message).filter(Message.id == message_id).first()
    @staticmethod
    def _user_display_name(user) -> str:
        if user.username:
            return f"@{user.username}"
        full_name = " ".join(filter(None, [user.first_name, user.last_name]))
        return full_name or str(user.telegram_id)

    def _build_sender_name_maps(self, user_ids, chat_ids):
        """
        sender_chat_id (посты от имени канала / анонимных админов) и
        sender_id у Reaction — это просто FK-колонки в models.py, без
        ORM-relationship (в отличие от Message.sender). Поэтому имена
        отправителей здесь резолвятся явными batch-запросами по набору
        id, а не через selectinload — иначе будет AttributeError на
        несуществующий атрибут.
        """
        user_names = {}
        if user_ids:
            for user in self.db.query(User).filter(User.id.in_(user_ids)).all():
                user_names[user.id] = self._user_display_name(user)

        chat_names = {}
        if chat_ids:
            for chat in self.db.query(Chat).filter(Chat.id.in_(chat_ids)).all():
                chat_names[chat.id] = chat.title or str(chat.telegram_chat_id)

        return user_names, chat_names

    def build_export_rows(self, telegram_chat_id, date_from, date_to, include_deleted=False):
        """
        Готовит данные для экспорта (commands._build_export_workbook)
        сразу в виде простых dict — все нужные связи (attachments,
        reactions, имена отправителей) разворачиваются здесь, пока
        Session ещё открыта. Наружу из этого метода не уходит ни одного
        ORM-объекта, поэтому вызывающий код (commands._run_db, см.
        соответствующий комментарий там) может спокойно работать с
        результатом уже после того, как эта Session закрыта.

        Возвращает None, если чат с таким telegram_chat_id не найден.
        """
        chat = self.get_chat(telegram_chat_id)
        if chat is None:
            return None

        query = (
            self.db.query(Message)
            .options(
                selectinload(Message.attachments),
                selectinload(Message.reactions),
            )
            .filter(
                Message.chat_id == chat.id,
                Message.tg_date >= date_from,
                Message.tg_date <= date_to,
            )
        )

        if not include_deleted:
            query = query.filter(Message.is_deleted.is_(False))

        messages = query.order_by(Message.tg_date.asc()).all()

        user_ids = set()
        chat_ids = set()
        for msg in messages:
            if msg.sender_id:
                user_ids.add(msg.sender_id)
            if msg.sender_chat_id:
                chat_ids.add(msg.sender_chat_id)
            for reaction in msg.reactions:
                if reaction.sender_id:
                    user_ids.add(reaction.sender_id)
                if reaction.sender_chat_id:
                    chat_ids.add(reaction.sender_chat_id)

        user_names, chat_names = self._build_sender_name_maps(user_ids, chat_ids)
    
        def sender_label(sender_id, sender_chat_id):
            if sender_id:
                return user_names.get(sender_id, f"user#{sender_id}")
            if sender_chat_id:
                return chat_names.get(sender_chat_id, f"chat#{sender_chat_id}")
            return ""

        message_rows = []
        attachment_rows = []
        reaction_rows = []

        for msg in messages:
            message_rows.append({
                "telegram_message_id": msg.telegram_message_id,
                "tg_date": msg.tg_date,
                "sender": sender_label(msg.sender_id, msg.sender_chat_id),
                "text": msg.text,
                "is_deleted": msg.is_deleted,
                "version": msg.version,
                "reply_to": msg.reply_to,
            })

            for attachment in msg.attachments:
                attachment_rows.append({
                    "telegram_message_id": msg.telegram_message_id,
                    "tg_date": msg.tg_date,
                    "file_type": attachment.file_type,
                    "file_name": attachment.file_name,
                    "mime_type": attachment.mime_type,
                    "size": attachment.size,
                    "local_path": attachment.local_path,
                })

            for reaction in msg.reactions:
                # _apply_interaction_info теперь идемпотентен (см.
                # комментарий там), поэтому is_active=False — это уже не
                # дубль от старого бага, а реально снятая реакция.
                # Показываем все реакции, текущие и снятые; колонка
                # "Активна" отличает одно от другого.
                reaction_rows.append({
                    "telegram_message_id": msg.telegram_message_id,
                    "tg_date": msg.tg_date,
                    "emoji": reaction.emoji,
                    "custom_emoji_id": reaction.custom_emoji_id,
                    "sender": sender_label(reaction.sender_id, reaction.sender_chat_id),
                    "reaction_date": reaction.tg_date,
                    "event": reaction.event_type,
                })

        return {
            "chat_title": chat.title or str(chat.telegram_chat_id),
            "messages": message_rows,
            "attachments": attachment_rows,
            "reactions": reaction_rows,
        }
    
    def get_message(self, chat_id, telegram_message_id):
        """
        chat_id — внутренний id чата (Chat.id), не telegram_chat_id.
        telegram_message_id не уникален глобально (уникален только
        в рамках одного чата), поэтому ищем по паре.
        """
        return (
            self.db.query(Message)
            .filter(
                Message.chat_id == chat_id,
                Message.telegram_message_id == telegram_message_id
            )
            .first()
        )

    def save_new_message(self, update):
        return self.save_message(update["message"])

    def save_message_update(self, message):
        return self.save_message(message)

    def save_messages(self, update):
        for message in update["messages"]:
            self.save_message(message)
        return None

    @staticmethod
    def _extract_text(message):
        content = message.get("content")
        if not content:
            return ""

        content_type = content["@type"]

        if content_type == "messageText":
            return content["text"]["text"]

        if content_type == "messageAnimatedEmoji":
            # одно эмодзи без текста — отдельный тип контента в TDLib,
            # сам текст лежит в content["emoji"]
            return content.get("emoji", "")

        if content_type in ("messagePhoto", "messageDocument", "messageVideo"):
            caption = content.get("caption") or {}
            return caption.get("text", "")

        if content_type == "messageVoiceNote":
            caption = content.get("caption") or {}
            text = caption.get("text", "")
            # если подписи нет — пишем плейсхолдер, чтобы строка в messages
            # не была пустой даже без скачанного файла
            return text or "[голосовое сообщение]"

        return ""

    def save_message(self, message):
        """
        Создаёт сообщение, если его ещё нет, либо — если оно уже
        существует и текст/edit_date изменились — обновляет текущую
        запись и добавляет ревизию "edited". Раньше при повторном
        приходе того же message["id"] функция просто молча выходила
        и правки/повторная обработка терялись.
        """

        chat = self.get_chat(message["chat_id"])
        if chat is None:
            return None

        sender_user = None
        sender_chat = None

        sender = message.get("sender_id", {})
        if sender.get("@type") == "messageSenderUser":
            sender_user = self.get_user(sender["user_id"])
        elif sender.get("@type") == "messageSenderChat":
            sender_chat = self.get_chat(sender["chat_id"])

        text = self._extract_text(message)

        # ВАЖНО: TDLib отдаёт date/edit_date как unix-timestamp в UTC.
        # datetime.fromtimestamp() без явной таймзоны конвертирует его в
        # ЛОКАЛЬНОЕ время машины, на которой работает бот — а фильтрация
        # периода в commands.py (choose_period/custom_range_received)
        # сравнивает tg_date с datetime.utcnow(). Из-за этого расхождения
        # только что отправленное сообщение получало tg_date "в будущем"
        # относительно UTC-now на величину локального смещения (например,
        # +3 часа для Москвы) и не проходило фильтр "tg_date <= date_to"
        # при выгрузке — то есть пропадало из Excel, хотя факт удаления
        # того же сообщения (deleted_at = datetime.utcnow()) проставлялся
        # на уже существующую строку и этому расхождению не подвержен.
        # Поэтому используем utcfromtimestamp — naive-время, но именно в
        # UTC, как и везде остальное в проекте.
        tg_date = datetime.utcfromtimestamp(message["date"])
        tg_edit_date = (
            datetime.utcfromtimestamp(message["edit_date"])
            if message.get("edit_date")
            else None
        )

        existing = self.get_message(chat.id, message["id"])

        if existing is None:

            msg = Message(
                telegram_message_id=message["id"],
                chat_id=chat.id,
                sender_id=sender_user.id if sender_user else None,
                sender_chat_id=sender_chat.id if sender_chat else None,
                text=text,
                tg_date=tg_date,
                tg_edit_date=tg_edit_date,
                reply_to=message.get("reply_to_message_id"),
                is_outgoing=message["is_outgoing"],
                version=1
            )
            self.db.add(msg)
            self.db.flush()

            self.db.add(
                MessageRevision(
                    message_id=msg.id,
                    revision_type="created",
                    text=text,
                    tg_date=tg_date
                )
            )

        else:

            msg = existing

            content_changed = (
                msg.text != text or msg.tg_edit_date != tg_edit_date
            )

            if content_changed:
                msg.version += 1
                msg.text = text
                msg.tg_edit_date = tg_edit_date

                self.db.add(
                    MessageRevision(
                        message_id=msg.id,
                        revision_type="edited",
                        text=text,
                        tg_date=tg_edit_date or tg_date
                    )
                )

        self._save_attachments(msg, message)
        self._apply_interaction_info(msg, message.get("interaction_info"))
        return None

    def _save_attachments(self, msg, message):
        """
        Вложения по сути не меняются после отправки, поэтому просто
        добавляем то, чего ещё нет (без дублей при повторной обработке).
        """

        content = message.get("content")
        if not content:
            return

        content_type = content["@type"]

        if content_type == "messagePhoto":
            file = content["photo"]["sizes"][-1]["photo"]
            file_type, file_name, mime_type = "photo", None, None

        elif content_type == "messageDocument":
            document = content["document"]
            file = document["document"]
            file_type = "document"
            file_name = document["file_name"]
            mime_type = document["mime_type"]

        elif content_type == "messageVideo":
            video = content["video"]
            file = video["video"]
            file_type = "video"
            file_name = video["file_name"]
            mime_type = video["mime_type"]

        elif content_type == "messageVoiceNote":
            voice = content["voice_note"]
            file = voice["voice"]
            file_type, file_name = "voice", None
            mime_type = voice["mime_type"]

        else:
            return

        already_saved = (
            self.db.query(Attachment)
            .filter(
                Attachment.message_id == msg.id,
                Attachment.telegram_file_id == file["id"]
            )
            .first()
        )
        if already_saved:
            return

        self.db.add(
            Attachment(
                message_id=msg.id,
                telegram_file_id=file["id"],
                file_type=file_type,
                file_name=file_name,
                mime_type=mime_type,
                size=file["size"],
                local_path=file.get("local", {}).get("path")
            )
        )
    def update_file(self, update):
        file = update.get("file") or {}
        local = file.get("local") or {}
        if not local.get("is_downloading_completed"):
            return None
        path = local.get("path")
        if not path:
            return None
        self.db.query(Attachment).filter(
            Attachment.telegram_file_id == file["id"]
        ).update({"local_path": path})
        return None

    def update_message_content(self, update):
        """
        updateMessageContent: chat_id, message_id, new_content.
        Приходит отдельно от updateMessageEdited (там только edit_date,
        без текста), поэтому именно здесь фиксируем ревизию с новым
        текстом — а время правки (tg_date) дозаполнит
        update_message_edited, если он придёт позже.
        """

        chat = self.get_chat(update["chat_id"])
        if chat is None:
            return None

        msg = self.get_message(chat.id, update["message_id"])
        if msg is None:
            return None

        new_content = update.get("new_content") or {}

        if new_content.get("@type") == "messageText":
            text = new_content["text"]["text"]
        else:
            text = msg.text

        if text == msg.text:
            return None

        msg.version += 1
        msg.text = text

        self.db.add(
            MessageRevision(
                message_id=msg.id,
                revision_type="edited",
                text=text,
                tg_date=msg.tg_edit_date
            )
        )

        return None

    def update_message_edited(self, update):
        """
        updateMessageEdited: только chat_id, message_id, edit_date,
        reply_markup — без текста. Обновляем edit_date на сообщении
        и, если перед этим updateMessageContent уже создал ревизию
        без даты, дозаполняем её.
        """

        chat = self.get_chat(update["chat_id"])
        if chat is None:
            return None

        msg = self.get_message(chat.id, update["message_id"])
        if msg is None:
            return None

        # См. комментарий в save_message — здесь та же причина: нужно UTC,
        # а не локальное время машины.
        edit_date = datetime.utcfromtimestamp(update["edit_date"])
        msg.tg_edit_date = edit_date

        pending_revision = (
            self.db.query(MessageRevision)
            .filter(
                MessageRevision.message_id == msg.id,
                MessageRevision.revision_type == "edited",
                MessageRevision.tg_date.is_(None)
            )
            .order_by(MessageRevision.id.desc())
            .first()
        )
        if pending_revision:
            pending_revision.tg_date = edit_date

        return None

    def delete_messages(self, update):
        """
        updateDeleteMessages: chat_id, message_ids[], is_permanent,
        from_cache. Telegram не присылает момент удаления — фиксируем
        локальное время получения.
        """
        if not update.get("is_permanent"):
        # сообщение исчезло из локального кэша / стало недоступно,
        # но это не значит, что его реально удалили — пропускаем
            return None
        chat = self.get_chat(update["chat_id"])
        if chat is None:
            return None

        for telegram_message_id in update.get("message_ids", []):

            msg = self.get_message(chat.id, telegram_message_id)
            if msg is None or msg.is_deleted:
                continue

            msg.is_deleted = True
            msg.deleted_at = datetime.utcnow()

            self.db.add(
                MessageRevision(
                    message_id=msg.id,
                    revision_type="deleted",
                    text=None,
                    tg_date=None
                )
            )

        return None

    # ---------- реакции ----------


    def update_interaction_info(self, update):
        chat = self.get_chat(update["chat_id"])
        if chat is None:
            return None

        message = self.get_message(chat.id, update["message_id"])
        if message is None:
            return None

        self._apply_interaction_info(message, update.get("interaction_info"))
        return None


    def _apply_interaction_info(self, message, interaction_info):
        """
        Общая логика для двух источников: живой апдейт updateMessageInteractionInfo
        и interaction_info, вложенный прямо в message при загрузке истории.

        Три случая по значению can_get_added_reactions:

        • False  — постраничный список авторов закрыт (анонимные реакции,
                   некоторые каналы). Единственный источник данных —
                   recent_sender_ids. Синхронизируем здесь же.

        • True   — список доступен. Не трогаем реакции: collector.py сам
                   запустит getMessageAddedReactions, и save_added_reactions
                   сделает полную синхронизацию со всеми авторами и датами.

        • отсутствует (None/ключа нет) — встречается в chatTypeBasicGroup и
                   chatTypePrivate. Collector всё равно пошлёт запрос
                   getMessageAddedReactions, но TDLib вернёт пустой список
                   (API не поддерживается для этого типа чата). Сохраняем
                   interaction_info в буфер — save_added_reactions возьмёт
                   его оттуда как fallback и применит recent_sender_ids.
                   Здесь же ничего не пишем, чтобы не создавать дублей:
                   если бы мы писали через recent_sender_ids сейчас, а потом
                   save_added_reactions получил бы непустой ответ (например,
                   после обновления TDLib) — реакции задвоились бы с разными
                   ключами (без tg_date vs с tg_date).
        """
        reactions = (interaction_info or {}).get("reactions") or {}
        reaction_list = reactions.get("reactions") or []

        if not reaction_list:
            # Реакций нет совсем. Гасим только если can_get не None —
            # при can_get=None "пусто в reaction_list" может быть просто
            # неполным состоянием до прихода ответа от TDLib.
            can_get = reactions.get("can_get_added_reactions")
            if can_get is not None:
                updated = self.db.query(Reaction).filter(
                    Reaction.message_id == message.id,
                    Reaction.is_active.is_(True)
                ).update({"is_active": False})
                if updated:
                    logger.info(
                        "_apply_interaction_info: реакций больше нет, сняли активность с %s строк",
                        updated
                    )
            return

        can_get = reactions.get("can_get_added_reactions")

        if can_get is True:
            # Полный список будет получен через getMessageAddedReactions →
            # save_added_reactions. Здесь ничего не делаем.
            return

        if can_get is None:
            # Collector пошлёт getMessageAddedReactions, но получит пустой
            # ответ (API не поддерживается). Сохраняем interaction_info в
            # буфер — save_added_reactions применит recent_sender_ids оттуда.
            key = (message.chat_id, message.telegram_message_id)
            self._interaction_info_buffer[key] = interaction_info
            return

        # can_get is False — постраничный список закрыт. Используем
        # recent_sender_ids как единственный доступный источник данных.
        desired_items = []
        for item in reaction_list:
            kind, emoji, custom_emoji_id = parse_reaction_type(item.get("type"))

            for sender in item.get("recent_sender_ids", []):
                user = None
                sender_chat = None
                if sender.get("@type") == "messageSenderUser":
                    user = self.get_user(sender["user_id"])
                elif sender.get("@type") == "messageSenderChat":
                    sender_chat = self.get_chat(sender["chat_id"])

                desired_items.append((
                    user.id if user else None,
                    sender_chat.id if sender_chat else None,
                    kind,
                    emoji,
                    custom_emoji_id,
                    None,  # tg_date недоступна через recent_sender_ids
                ))

        self._save_reaction_events(message, desired_items)

    def _sync_reactions(self, message, desired_items):
        """
        desired_items — полный целевой набор АКТИВНЫХ реакций на message:
        список (sender_id, sender_chat_id, kind, emoji, custom_emoji_id,
        tg_date). Сравнивает его с уже сохранёнными активными строками в
        БД и трогает только разницу: новые реакции добавляет, пропавшие
        гасит (is_active=False), совпадающие не трогает вообще.

        Общая логика для двух источников реакций: ограниченного
        recent_sender_ids (см. _apply_recent_sender_ids, запасной путь для
        basicGroup/private) и полного getMessageAddedReactions (см.
        save_added_reactions — основной путь для супергрупп и каналов).

        Дедупликация анонимных записей: если в БД уже есть реакция с
        sender_id=None (записана раньше из-за race condition — getUser ещё
        не пришёл), а в desired_items та же реакция появляется с реальным
        sender_id — обновляем существующую запись вместо создания дубля.
        """
        existing_active = (
            self.db.query(Reaction)
            .filter(
                Reaction.message_id == message.id,
                Reaction.is_active.is_(True),
            )
            .all()
        )
        existing_by_key = {
            (
                row.sender_id,
                row.sender_chat_id,
                row.reaction_kind,
                row.emoji,
                row.custom_emoji_id,
            ): row
            for row in existing_active
        }

        # Анонимные записи (sender_id=None, sender_chat_id=None) — кандидаты
        # на обновление если придёт та же реакция с реальным отправителем.
        # Ключ: (kind, emoji, custom_emoji_id) → строка в БД.
        anonymous_by_type = {
            (row.reaction_kind, row.emoji, row.custom_emoji_id): row
            for row in existing_active
            if row.sender_id is None and row.sender_chat_id is None
        }

        desired_by_key = {}
        for sender_id, sender_chat_id, kind, emoji, custom_emoji_id, tg_date in desired_items:
            key = (sender_id, sender_chat_id, kind, emoji, custom_emoji_id)
            # если в desired_items несколько записей с одним и тем же
            # ключом (дубль на стороне TDLib) — оставляем последнюю дату
            desired_by_key[key] = tg_date

        # Набор ключей desired, которые "покрывают" анонимную запись —
        # чтобы не гасить анонимную запись, которую мы сейчас апгрейдим.
        upgraded_anonymous_rows = set()

        removed = 0
        added = 0

        for key, tg_date in desired_by_key.items():
            if key in existing_by_key:
                # уже есть точное совпадение — ничего не делаем
                continue

            sender_id, sender_chat_id, kind, emoji, custom_emoji_id = key
            type_key = (kind, emoji, custom_emoji_id)

            if (
                sender_id is not None or sender_chat_id is not None
            ) and type_key in anonymous_by_type:
                # В БД есть анонимная запись той же реакции — это старая
                # запись без автора. Проставляем реального отправителя
                # и дату вместо создания дубля.
                anon_row = anonymous_by_type.pop(type_key)
                anon_row.sender_id = sender_id
                anon_row.sender_chat_id = sender_chat_id
                if tg_date is not None:
                    anon_row.tg_date = tg_date
                upgraded_anonymous_rows.add(id(anon_row))
                logger.debug(
                    "_sync_reactions: обновили анонимную реакцию message_id=%s "
                    "emoji=%s → sender_id=%s sender_chat_id=%s",
                    message.id, emoji, sender_id, sender_chat_id,
                )
            else:
                self.db.add(
                    Reaction(
                        message_id=message.id,
                        sender_id=sender_id,
                        sender_chat_id=sender_chat_id,
                        reaction_kind=kind,
                        emoji=emoji,
                        custom_emoji_id=custom_emoji_id,
                        tg_date=tg_date,
                        is_active=True,
                    )
                )
                added += 1

        for key, row in existing_by_key.items():
            if id(row) in upgraded_anonymous_rows:
                # уже обновлена выше — не трогаем
                continue
            if key not in desired_by_key:
                row.is_active = False
                removed += 1

        if added or removed or upgraded_anonymous_rows:
            logger.info(
                "_sync_reactions: message_id=%s, добавлено=%s, обновлено_анонимных=%s, погашено=%s",
                message.id, added, len(upgraded_anonymous_rows), removed,
            )

    def save_added_reactions(self, update):
        """
        Ответ на getMessageAddedReactions — основной путь получения
        реакций для чатов, где can_get_added_reactions=True (то есть для
        большинства чатов; см. _apply_interaction_info). collector.py
        запрашивает их постранично через offset/next_offset, и пока
        страницы не закончились — мы только копим присланные реакции в
        self._added_reactions_buffer по ключу (chat_id, message_id), НЕ
        трогая БД: промежуточная страница не содержит всего набора, и
        свериться с БД прямо по ней — значит по ошибке погасить ещё не
        увиденные, но всё ещё активные реакции с других страниц.

        Сверяем накопленное с БД и пишем разницу одним махом только когда
        next_offset пуст — то есть когда получены вообще все страницы.

        Буфер хранит сырые telegram_user_id / telegram_chat_id, а не
        внутренние DB id. getUser-ответ может прийти ПОСЛЕ addedReactions
        (порядок сетевых ответов TDLib не гарантирован), поэтому
        резолвим telegram_id → DB id только при записи последней страницы,
        когда нужные строки в users/chats уже, скорее всего, существуют.
        Если пользователь всё-таки ещё не сохранён — пишем реакцию без
        sender_id (как делали раньше), но не теряем саму реакцию.
        """
        extra = update.get("@extra") or {}
        telegram_chat_id = extra.get("chat_id")
        telegram_message_id = extra.get("message_id")
        # can_get прокидывается collector-ом из interaction_info (True/None)
        can_get = extra.get("can_get")

        if telegram_chat_id is None or telegram_message_id is None:
            return None

        key = (telegram_chat_id, telegram_message_id)
        buffer = self._added_reactions_buffer.setdefault(key, [])

        for item in update.get("reactions") or []:
            kind, emoji, custom_emoji_id = parse_reaction_type(item.get("type"))

            sender = item.get("sender_id") or {}
            tg_date = (
                datetime.utcfromtimestamp(item["date"])
                if item.get("date")
                else None
            )

            # Храним сырые telegram-id; резолвим в DB id при финальной записи.
            if sender.get("@type") == "messageSenderUser":
                buffer.append(("user", sender["user_id"], kind, emoji, custom_emoji_id, tg_date))
            elif sender.get("@type") == "messageSenderChat":
                buffer.append(("chat", sender["chat_id"], kind, emoji, custom_emoji_id, tg_date))
            else:
                buffer.append((None, None, kind, emoji, custom_emoji_id, tg_date))

        if update.get("next_offset"):
            # ещё не последняя страница — ждём остальные, в БД пока не пишем
            return None

        # Последняя страница получена — резолвим telegram-id → DB id пачкой.
        raw_items = self._added_reactions_buffer.pop(key, [])

        if not raw_items:
            chat = self.get_chat(telegram_chat_id)
            if chat is None:
                return None

            message = self.get_message(chat.id, telegram_message_id)
            if message is None:
                return None

            if can_get is True:
                # Полный список реакций пуст — все реакции сняты.
                self._save_reaction_events(message, [])

            else:
                interaction_info = self._interaction_info_buffer.pop(key, None)

                if interaction_info is not None:
                    self._apply_recent_sender_ids(message, interaction_info)
                else:
                    logger.debug(
                        "save_added_reactions: пустой ответ и нет interaction_info "
                        "для chat_id=%s message_id=%s",
                        telegram_chat_id,
                        telegram_message_id,
                    )

            return None

        # Есть реакции — очищаем interaction_info_buffer (он больше не нужен)
        self._interaction_info_buffer.pop(key, None)

        chat = self.get_chat(telegram_chat_id)
        if chat is None:
            return None

        message = self.get_message(chat.id, telegram_message_id)
        if message is None:
            return None

        desired_items = []
        for sender_type, tg_sender_id, kind, emoji, custom_emoji_id, tg_date in raw_items:
            db_user_id = None
            db_chat_id = None
            if sender_type == "user":
                user = self.get_user(tg_sender_id)
                if user is None:
                    logger.debug(
                        "save_added_reactions: пользователь telegram_id=%s ещё не в БД, "
                        "реакция будет сохранена без sender_id",
                        tg_sender_id,
                    )
                else:
                    db_user_id = user.id
            elif sender_type == "chat":
                sender_chat = self.get_chat(tg_sender_id)
                if sender_chat is not None:
                    db_chat_id = sender_chat.id

            desired_items.append((db_user_id, db_chat_id, kind, emoji, custom_emoji_id, tg_date))

        self._save_reaction_events(message, desired_items)
        return None

    def _save_reaction_events(self, message, desired_items):
        """
        desired_items — текущее состояние TDLib.
        В БД пишутся только новые события.
        """

        last_events = {}
        
        
        rows = (
            self.db.query(Reaction)
            .filter(Reaction.message_id == message.id)
            .order_by(Reaction.id)
            .all()
        )

        for row in rows:
            key = (
                row.sender_id,
                row.sender_chat_id,
                row.reaction_kind,
                row.emoji,
                row.custom_emoji_id,
            )
            last_events[key] = row.event_type

        current = {
            (
                sender_id,
                sender_chat_id,
                kind,
                emoji,
                custom_emoji_id,
            ): tg_date
            for sender_id, sender_chat_id, kind, emoji, custom_emoji_id, tg_date
            in desired_items
        }
        logger.warning("CURRENT=%s", current)
        logger.warning("LAST=%s", last_events)
        #
        # появились новые реакции
        #
        for key, tg_date in current.items():

            if last_events.get(key) == "added":
                continue

            sender_id, sender_chat_id, kind, emoji, custom_emoji_id = key

            self.db.add(
                Reaction(
                    message_id=message.id,
                    sender_id=sender_id,
                    sender_chat_id=sender_chat_id,
                    reaction_kind=kind,
                    emoji=emoji,
                    custom_emoji_id=custom_emoji_id,
                    tg_date=tg_date,
                    event_type="added",
                )
            )

        #
        # исчезли реакции
        #
        for key, last in last_events.items():

            if last != "added":
                continue

            if key in current:
                continue

            sender_id, sender_chat_id, kind, emoji, custom_emoji_id = key

            self.db.add(
                Reaction(
                    message_id=message.id,
                    sender_id=sender_id,
                    sender_chat_id=sender_chat_id,
                    reaction_kind=kind,
                    emoji=emoji,
                    custom_emoji_id=custom_emoji_id,
                    tg_date=None,
                    event_type="removed",
                )
            )

    def _apply_recent_sender_ids(self, message, interaction_info):
        """
        Fallback для чатов, где getMessageAddedReactions не поддерживается
        (can_get=None). Синхронизирует реакции через recent_sender_ids из
        interaction_info. Вызывается из save_added_reactions при пустом
        ответе на getMessageAddedReactions.
        """
        reactions = (interaction_info or {}).get("reactions") or {}
        reaction_list = reactions.get("reactions") or []

        desired_items = []
        for item in reaction_list:
            kind, emoji, custom_emoji_id = parse_reaction_type(item.get("type"))

            for sender in item.get("recent_sender_ids", []):
                user = None
                sender_chat = None
                if sender.get("@type") == "messageSenderUser":
                    user = self.get_user(sender["user_id"])
                elif sender.get("@type") == "messageSenderChat":
                    sender_chat = self.get_chat(sender["chat_id"])

                desired_items.append((
                    user.id if user else None,
                    sender_chat.id if sender_chat else None,
                    kind,
                    emoji,
                    custom_emoji_id,
                    None,  # tg_date недоступна через recent_sender_ids
                ))

        self._save_reaction_events(message, desired_items)