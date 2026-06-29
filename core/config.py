import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    phone_number: str
    password: str
    proxy_server: str | None
    proxy_port: int | None
    proxy_secret: str | None


@dataclass(frozen=True)
class BotConfig:
    bot_token: str
    admin_ids: frozenset[int]


@dataclass(frozen=True)
class DatabaseConfig:
    url: str


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    bot: BotConfig
    database: DatabaseConfig


def load_config(env_file: str | None = None) -> AppConfig:
    """
    Единственное место, где читаются переменные окружения (.env).
    Вызывается один раз в main.py — дальше все остальные модули получают
    нужные им значения через параметры конструктора, а не через прямой
    import core.config. Так модули остаются независимыми друг от друга
    и от способа хранения конфигурации (.env / Vault / CLI — без разницы),
    и их легко тестировать отдельно, подставляя конфиг вручную.
    """
    load_dotenv(env_file)

    proxy_port = os.getenv("PROXY_PORT")

    telegram = TelegramConfig(
        api_id=int(os.getenv("API_ID")),
        api_hash=os.getenv("API_HASH"),
        phone_number=os.getenv("PHONE_NUMBER"),
        password=os.getenv("PASSWORD"),
        proxy_server=os.getenv("PROXY_SERVER"),
        proxy_port=int(proxy_port) if proxy_port else None,
        proxy_secret=os.getenv("PROXY_SECRET"),
    )

    bot = BotConfig(
        bot_token=os.getenv("BOT_TOKEN"),
        admin_ids=frozenset(
            int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
        ),
    )

    database = DatabaseConfig(
        url=(
            f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
            f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
        )
    )

    return AppConfig(telegram=telegram, bot=bot, database=database)