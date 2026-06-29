import json
import tdjson


class TelegramClient:
    def __init__(self):
        self.client_id = tdjson.td_create_client_id()

    def send(self, query: dict):
        tdjson.td_send(
            self.client_id,
            json.dumps(query).encode("utf-8")
        )

    def receive(self, timeout: float = 1.0):

        response = tdjson.td_receive(timeout)

        if not response:
            return None

        if isinstance(response, bytes):
            response = response.decode("utf-8")

        return json.loads(response)

    def execute(self, query: dict):

        response = tdjson.td_execute(
            json.dumps(query).encode("utf-8")
        )

        if not response:
            return None

        if isinstance(response, bytes):
            response = response.decode("utf-8")

        return json.loads(response)