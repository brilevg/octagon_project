# core/logging_config.py
import logging
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(log_file: str = "logs/collector.log", level: int = logging.INFO) -> None:
    """
    Единая точка настройки логирования. Вызывается один раз в main.py,
    до создания клиента/коллектора/БД — дальше все модули просто берут
    logging.getLogger(__name__) и пишут в общий лог.
    """
    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(console_handler)
    
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)