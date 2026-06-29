import logging
class TelegramCollector:
    HISTORY_PAGE_LIMIT = 100
    def __init__(self, client):
        self.client = client
        self.started = False
        # чтобы не слать getUser повторно на каждое сообщение одного автора
        self._requested_user_ids = set()
        self._history_sync_started = set()
        # на одно сообщение в любой момент времени допускается не больше
        # одного запущенного цикла getMessageAddedReactions. Это ключевое
        # условие, благодаря которому ответы безопасно применять как есть,
        # без риска гонки (ответы на сетевые запросы могут вернуться не в
        # том порядке, в котором были отправлены, — а для голосований
        # через реакции потеря/перезатирание данных недопустимы).
        self._reactions_in_flight = set()   # {(chat_id, message_id), ...}
        # если апдейт о реакциях пришёл, пока цикл уже идёт — не запускаем
        # параллельный, а просто помечаем "обновить ещё раз сразу после"
        self._reactions_pending = set()
        logger = logging.getLogger(__name__)
        self._logger = logger
    def _request_chat_history(self, chat_id, from_message_id):
        self.client.send({
            "@type": "getChatHistory",
            "chat_id": chat_id,
            "from_message_id": from_message_id,
            "offset": 0,
            "limit": self.HISTORY_PAGE_LIMIT,
            "only_local": False,
            "@extra": {
                "request": "history_sync",
                "chat_id": chat_id
            }
        })
    def _start_history_sync(self, chat_id):
        if chat_id in self._history_sync_started:
            return
        self._history_sync_started.add(chat_id)
        self._request_chat_history(chat_id, from_message_id=0)
    def process_message(self, message):
        sender = message.get("sender_id", {})

        if sender.get("@type") == "messageSenderUser":
            user_id = sender["user_id"]

            if user_id not in self._requested_user_ids:
                self._requested_user_ids.add(user_id)
                self.client.send({
                    "@type": "getUser",
                    "user_id": user_id
                })

        content = message.get("content", {})
        content_type = content.get("@type")

        file = None

        if content_type == "messagePhoto":
            file = content["photo"]["sizes"][-1]["photo"]

        elif content_type == "messageDocument":
            file = content["document"]["document"]

        elif content_type == "messageVideo":
            file = content["video"]["video"]

        elif content_type == "messageVoiceNote":
            file = content["voice_note"]["voice"]

        if file:
            self.client.send({
                "@type": "downloadFile",
                "file_id": file["id"],
                "priority": 1,
                "synchronous": False
            })

        # У старых сообщений (из getChatHistory) реакции уже могут быть
        # проставлены, но updateMessageInteractionInfo по ним не придёт —
        # этот апдейт сигнализирует только об ИЗМЕНЕНИИ реакций, а не о
        # самом факте их наличия. Поэтому проверяем interaction_info,
        # который уже лежит в самом объекте message, и запрашиваем полный
        # список реакций сразу, не дожидаясь живого события.
        self.request_added_reactions(
            message["chat_id"],
            message["id"],
            message.get("interaction_info")
        )

    def request_added_reactions(self, chat_id, message_id, interaction_info):

        if not interaction_info:
            return

        reactions = interaction_info.get("reactions") or {}
        reaction_list = reactions.get("reactions") or []


        # can_get_added_reactions может отсутствовать в старых версиях
        # TDLib — тогда просто пробуем; явный False означает, что список
        # отправителей закрыт (например, анонимные реакции в этом чате),
        # и доступны только агрегированные total_count из interaction_info.
        can_get = reactions.get("can_get_added_reactions")
        if can_get is False and not reaction_list:
            return

        key = (chat_id, message_id)

        if key in self._reactions_in_flight:
            # цикл уже идёт — не запускаем второй параллельно, попросим
            # обновить ещё раз сразу после того, как текущий завершится
            self._reactions_pending.add(key)
            return

        self._reactions_in_flight.add(key)
        self._send_get_added_reactions(chat_id, message_id, offset="", page=0, can_get=can_get)

    def _send_get_added_reactions(self, chat_id, message_id, offset, page, can_get=None):
        # chat_id/message_id у ответа addedReactions своих нет — TDLib
        # возвращает "@extra" из запроса как есть, поэтому прокидываем их
        # через @extra, чтобы database-модуль знал, к какому сообщению
        # относится ответ, и в т.ч. знал, что это первая страница (page=0,
        # когда нужно снять активность со старого набора реакций).
        #
        # can_get прокидывается из interaction_info["reactions"][
        # "can_get_added_reactions"]. Значение важно для repository:
        # при can_get=True пустой ответ означает "реакций нет" (нужно
        # погасить), при can_get=None пустой ответ означает "API не
        # поддерживается для этого типа чата" (используем recent_sender_ids).
        self.client.send({
            "@type": "getMessageAddedReactions",
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction_type": None,
            "offset": offset,
            "limit": 100,
            "@extra": {
                "chat_id": chat_id,
                "message_id": message_id,
                "page": page,
                "can_get": can_get,
            }
        })

    def _handle_added_reactions_response(self, update):
        extra = update.get("@extra") or {}
        chat_id = extra.get("chat_id")
        message_id = extra.get("message_id")
        page = extra.get("page", 0)
        can_get = extra.get("can_get")  # прокидывается из исходного запроса

        if chat_id is None or message_id is None:
            return

        key = (chat_id, message_id)
        next_offset = update.get("next_offset", "")

        if next_offset:
            # ещё есть страницы — продолжаем тот же цикл опроса
            self._send_get_added_reactions(chat_id, message_id, next_offset, page + 1, can_get=can_get)
            return

        # цикл для этого сообщения завершён
        self._reactions_in_flight.discard(key)

        if key in self._reactions_pending:
            # реакции могли поменяться ещё раз, пока мы ждали ответ —
            # запускаем новый цикл с чистого листа, чтобы захватить
            # самое актуальное состояние
            self._reactions_pending.discard(key)
            self._reactions_in_flight.add(key)
            self._send_get_added_reactions(chat_id, message_id, offset="", page=0, can_get=can_get)

    def handle(self, update):
        if not self.started:
            self.started = True

            self.client.send({
                "@type": "getChats",
                "chat_list": {
                    "@type": "chatListMain"
                },
                "limit": 100
            })
        if update.get("@type") == "chats":
            chat_ids = update["chat_ids"]
            for chat_id in chat_ids:
                self.client.send({"@type": "getChat", "chat_id": chat_id})
            if len(chat_ids) >= 100:
            # возможно есть ещё чаты — просим TDLib подгрузить дальше
                self.client.send({
                    "@type": "loadChats",
                    "chat_list": {"@type": "chatListMain"},
                    "limit": 100
                })


        if update.get("@type") == "chat":
            chat_id = update["id"]            
            self._logger.info("chat: %s", update["title"])
            self._start_history_sync(chat_id)

            # self.client.send({
            #     "@type": "getChatHistory",
            #     "chat_id": update["id"],
            #     "from_message_id": 0,
            #     "offset": 0,
            #     "limit": 100,
            #     "only_local": False
            # })
        
        if update.get("@type") == "updateNewChat":
            chat = update["chat"]
            self.client.send({"@type": "getChat", "chat_id": chat["id"]})
        
        if update.get("@type") == "messages":
            messages = update.get("messages") or []
            for message in messages:
                self.process_message(message)
            extra = update.get("@extra") or {}
            if extra.get("request") == "history_sync":
                if messages:
                    oldest_id = messages[-1]["id"]
                    self._request_chat_history(extra["chat_id"], oldest_id)
                else:
                    self._logger.info(
                        "История чата %s полностью синхронизирована",
                        extra.get("chat_id")
                    )


        elif update.get("@type") == "updateNewMessage":
            self.process_message(update["message"])

        if update.get("@type") == "updateMessageInteractionInfo":
            # interaction_info здесь даёт только total_count и куцый
            # recent_sender_ids (несколько последних). Полный список
            # "кто поставил какую реакцию" получаем отдельным запросом.
            self.request_added_reactions(
                update["chat_id"],
                update["message_id"],
                update.get("interaction_info")
            )
        elif update.get("@type") == "addedReactions":
            self._handle_added_reactions_response(update)

        elif update.get("@type") == "message":
            self.process_message(update)