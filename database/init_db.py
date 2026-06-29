from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base


def build_engine(database_url: str):
    return create_engine(database_url)


def build_session_factory(engine):
    """
    sessionmaker, привязанный к переданному engine. DatabaseRepository
    получает его через конструктор — модуль database больше не хранит
    собственный глобальный SessionLocal, привязанный к .env на момент
    импорта.
    """
    return sessionmaker(bind=engine)


def create_database(engine) -> None:
    """
    create_all() создаёт только отсутствующие таблицы и НЕ делает ALTER
    на уже существующих — при изменении схемы существующей таблицы нужна
    отдельная миграция (Alembic), повторный вызов этой функции её не
    подхватит. Но создавать недостающие таблицы безопасно вызывать на
    каждом старте — поэтому main.py делает это сам, и отдельный шаг
    `python -m database.init_db` перед запуском больше не нужен.
    """
    Base.metadata.create_all(engine)