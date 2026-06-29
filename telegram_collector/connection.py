import logging
import time

logger = logging.getLogger(__name__)


class ConnectionMonitor:
    """
    Следит за updateConnectionState. Если TDLib застревает в состоянии
    connectionStateConnectingToProxy дольше PROXY_STUCK_TIMEOUT секунд —
    это признак того, что прокси не отвечает / неверные параметры
    (PROXY_SERVER/PROXY_PORT/PROXY_SECRET), и мы пишем об этом в лог
    явной строкой, которую легко искать ("PROXY:").

    Также логирует "error"-апдевты от TDLib — раньше они проходили через
    общий цикл main.py молча, ни один из обработчиков на них не реагировал.
    """

    PROXY_STUCK_TIMEOUT = 10 # секунд

    def __init__(self):
        self._state = None
        self._connecting_to_proxy_since = None
        self._warned_stuck = False

    def handle(self, update):
        if not update:
            return

        update_type = update.get("@type")

        if update_type == "updateConnectionState":
            self._handle_state(update["state"]["@type"])

        elif update_type == "error":
            self._handle_error(update)

    def _handle_state(self, state):
        if state != self._state:
            logger.info("CONNECTION: %s -> %s", self._state, state)

        was_stuck = self._warned_stuck
        self._state = state

        if state == "connectionStateConnectingToProxy":
            if self._connecting_to_proxy_since is None:
                self._connecting_to_proxy_since = time.monotonic()
        else:
            self._connecting_to_proxy_since = None
            self._warned_stuck = False

        if state == "connectionStateReady" and was_stuck:
            logger.info("PROXY: соединение восстановлено")

    def check_stuck(self):
        """Вызывать на каждой итерации основного цикла, не только при
        получении нового update — иначе зависание без новых апдейтов
        от TDLib останется незамеченным."""

        if self._state != "connectionStateConnectingToProxy":
            return
        if self._connecting_to_proxy_since is None or self._warned_stuck:
            return

        elapsed = time.monotonic() - self._connecting_to_proxy_since
        if elapsed >= self.PROXY_STUCK_TIMEOUT:
            logger.error(
                "PROXY: не удаётся подключиться через прокси уже %.0f сек. "
                "Проверьте PROXY_SERVER/PROXY_PORT/PROXY_SECRET и доступность прокси-сервера.",
                elapsed
            )
            self._warned_stuck = True

    def _handle_error(self, update):
        message = update.get("message", "")
        code = update.get("code")

        if "proxy" in message.lower():
            logger.error("PROXY ERROR (code=%s): %s", code, message)
        else:
            logger.warning("TDLib error (code=%s): %s", code, message)