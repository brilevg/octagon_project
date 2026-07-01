import logging
from pathlib import Path
import hashlib
logger = logging.getLogger(__name__)


class Authorization:
    
    def __init__(self, client, api_id, api_hash, phone_number, password):
        self.client = client
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone_number = phone_number
        self.password = password

        self.is_authorized = False
        self._tdlib_params_sent = False

    def handle(self, update):

        if not update:
            return

        if update["@type"] == "updateAuthorizationState":
            state = update["authorization_state"]["@type"]
        elif update["@type"].startswith("authorizationState"):
            state = update["@type"]
        else:
            return

        logger.info("AUTH: %s", state)

        if state == "authorizationStateWaitTdlibParameters":
            if self._tdlib_params_sent:
                return
            self._tdlib_params_sent = True
            session = hashlib.md5(self.phone_number.encode("utf-8")).hexdigest()

            database_dir = Path("tdlib") / session
            files_dir = database_dir / "files"

            database_dir.mkdir(parents=True, exist_ok=True)
            files_dir.mkdir(parents=True, exist_ok=True)
            self.client.send({
                "@type": "setTdlibParameters",

                # своя database_directory на каждый аккаунт — иначе при
                # смене PHONE_NUMBER в .env TDLib тихо подхватывает уже
                # лежащую на диске сессию, и PHONE_NUMBER из .env не
                # используется вообще 
                "database_directory": str(database_dir),
                "files_directory": str(files_dir),

                "use_test_dc": False,

                "use_file_database": True,
                "use_chat_info_database": True,
                "use_message_database": True,
                "use_secret_chats": False,

                "api_id": self.api_id,
                "api_hash": self.api_hash,

                "system_language_code": "en",
                "device_model": "Desktop",
                "system_version": "Windows",
                "application_version": "1.0",

                "enable_storage_optimizer": True,
                "ignore_file_names": False
            })

        elif state == "authorizationStateWaitEncryptionKey":
            self.client.send({
                "@type": "checkDatabaseEncryptionKey",
                "encryption_key": ""
            })

        elif state == "authorizationStateWaitPhoneNumber":
            self.client.send({
                "@type": "setAuthenticationPhoneNumber",
                "phone_number": self.phone_number
            })

        elif state == "authorizationStateWaitCode":
            code = input("INPUT CODE FROM YOUR ACCOUNT: ")
            self.client.send({
                "@type": "checkAuthenticationCode",
                "code": code
            })

        elif state == "authorizationStateWaitPassword":
            self.client.send({
                "@type": "checkAuthenticationPassword",
                "password": self.password
            })

        elif state == "authorizationStateReady":
            logger.info("AUTHORIZED")
            self.is_authorized = True

        elif state == "authorizationStateClosed":
            logger.info("TDLib closed")