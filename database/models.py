from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


class User(Base):

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True
    )

    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))

    phone: Mapped[str | None] = mapped_column(String(30))

    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    messages = relationship(
        "Message",
        back_populates="sender",
        foreign_keys="Message.sender_id"
    )


class Chat(Base):

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(primary_key=True)

    telegram_chat_id: Mapped[int] = mapped_column(
        BigInteger,
        unique=True,
        index=True
    )

    title: Mapped[str | None] = mapped_column(String(255))

    chat_type: Mapped[str] = mapped_column(String(50))

    created_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    messages = relationship(
        "Message",
        back_populates="chat",
        foreign_keys="Message.chat_id"
    )


class Message(Base):
    """
    Текущее (актуальное) состояние сообщения.

    Это "проекция" — то, что сообщение представляет собой прямо сейчас.
    Полная история правок и удаления лежит в MessageRevision, а не здесь:
    эта таблица никогда не перезатирает данные молча, всё проходит через
    запись в message_revisions.

    id — стабильный внутренний идентификатор строки, на него ссылаются
    attachments и reactions. telegram_message_id может повторяться
    в разных чатах, поэтому уникальность — по паре (chat_id, telegram_message_id).
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)

    telegram_message_id: Mapped[int] = mapped_column(BigInteger)

    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id"))

    sender_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True
    )

    # для постов от имени канала / анонимных админов (sender_id типа messageSenderChat)
    sender_chat_id: Mapped[int | None] = mapped_column(
        ForeignKey("chats.id"),
        nullable=True
    )

    text: Mapped[str | None] = mapped_column(Text)

    # время, которое прислал сам Telegram
    tg_date: Mapped[datetime] = mapped_column()
    tg_edit_date: Mapped[datetime | None] = mapped_column()

    reply_to: Mapped[int | None] = mapped_column(BigInteger)

    is_outgoing: Mapped[bool] = mapped_column(Boolean)

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Telegram не присылает момент удаления — фиксируем локальное время
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # инкрементируется при каждом изменении текста
    version: Mapped[int] = mapped_column(Integer, default=1)

    # когда мы локально записали текущую версию строки
    received_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    sender = relationship(
        "User",
        back_populates="messages",
        foreign_keys=[sender_id]
    )

    chat = relationship(
        "Chat",
        back_populates="messages",
        foreign_keys=[chat_id]
    )

    attachments = relationship("Attachment", back_populates="message")
    reactions = relationship("Reaction", back_populates="message")
    revisions = relationship(
        "MessageRevision",
        back_populates="message",
        order_by="MessageRevision.id"
    )

    __table_args__ = (
        UniqueConstraint(
            "chat_id", "telegram_message_id",
            name="uq_messages_chat_telegram_id"
        ),
    )


class MessageRevision(Base):
    """
    Лог изменений сообщения: создание / правка / удаление.

    Одна строка = один зафиксированный факт. Ничего не перезаписывается
    и не удаляется — отвечает на запрос "хочу видеть запись при
    изменениях сообщения и при удалении".
    """

    __tablename__ = "message_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)

    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))

    # "created" | "edited" | "deleted"
    revision_type: Mapped[str] = mapped_column(String(20))

    # содержимое сообщения на момент этой ревизии, NULL для "deleted"
    text: Mapped[str | None] = mapped_column(Text)

    # date/edit_date от Telegram для этой ревизии, если применимо
    # (для "deleted" остаётся NULL — Telegram не даёт такую метку)
    tg_date: Mapped[datetime | None] = mapped_column()

    # локальное время, когда мы зафиксировали ревизию
    received_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    message = relationship("Message", back_populates="revisions")


class Reaction(Base):
    """
    Одна конкретная реакция одного конкретного отправителя на сообщение —
    "сообщение А, реакция Б, пользователь В", как в логах.

    Получаем через getMessageAddedReactions (агрегированные данные из
    interaction_info содержат только total_count и ограниченный список
    recent_sender_ids, полного списка пользователей там нет).

    is_active=False — реакция была снята; строка не удаляется, чтобы
    сохранить историю того, кто и когда реагировал.
    """

    __tablename__ = "reactions"

    id: Mapped[int] = mapped_column(primary_key=True)

    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))

    sender_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True
    )
    sender_chat_id: Mapped[int | None] = mapped_column(
        ForeignKey("chats.id"),
        nullable=True
    )

    # "emoji" | "custom_emoji" | "paid"
    reaction_kind: Mapped[str] = mapped_column(String(20))
    emoji: Mapped[str | None] = mapped_column(String(32))
    custom_emoji_id: Mapped[int | None] = mapped_column(BigInteger)

    # date из addedReaction (момент, когда поставлена реакция, по Telegram)
    tg_date: Mapped[datetime | None] = mapped_column()

    event_type: Mapped[str] = mapped_column(String(16))

    received_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    message = relationship("Message", back_populates="reactions")


class Attachment(Base):

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)

    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))

    telegram_file_id: Mapped[int] = mapped_column(BigInteger)

    file_type: Mapped[str] = mapped_column(String(32))

    file_name: Mapped[str | None] = mapped_column(String(255))

    mime_type: Mapped[str | None] = mapped_column(String(128))

    size: Mapped[int | None]

    local_path: Mapped[str | None] = mapped_column(Text)

    received_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    message = relationship("Message", back_populates="attachments")